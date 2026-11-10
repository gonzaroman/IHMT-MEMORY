"""Capture Claude Code conversations into their own IHMT store.

Claude Code keeps every session as a JSON-lines transcript under
``~/.claude/projects/<project>/<session>.jsonl``. Almost all of it is tool
output, images and model reasoning; what the user and the assistant actually
*said* is well under 1 % of the bytes. This module keeps that part — the text of
both sides plus one line per action the assistant took — and stores it in a
separate tree (``$IHMT_HOME/ihmt_conversations``), so thousands of chat
fragments never compete with the curated notes of the long-term memory.

Capture is incremental: a state file remembers how far into each transcript the
last run read, so running it again (from a session hook, or as a sweep over all
transcripts) only ingests what is new and never duplicates.

Standard library only, like the rest of the core.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .api import IHMT
from .models import DataType
from .textutils import estimate_tokens, slugify

#: Directory (inside ``IHMT_HOME``) holding the conversations store.
CONVERSATIONS_DIRNAME = "ihmt_conversations"
#: Prefix of every conversation leaf's ``source``; the project name follows it.
SOURCE_PREFIX = "claude-code://"
#: Size of one stored fragment. Small on purpose: a search answers with a whole
#: fragment, so this is also what a hit costs in context.
FRAGMENT_TOKENS = 600
#: Longest action line kept for a tool call.
ACTION_CHARS = 140
#: A lock older than this belonged to a run that died.
LOCK_STALE_SECONDS = 15 * 60

#: Tools that only look at things. Listing them would bury the actions that
#: changed something.
_READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "ToolSearch", "TodoWrite", "LS", "ListAgents"})
#: Input fields that best describe a tool call, most telling first.
_ACTION_FIELDS = ("description", "file_path", "command", "path", "url", "query", "prompt", "skill", "content")

_SECRET_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)
#: ``password = hunter2``-style assignments: the name is kept, the value is not.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:password|passwd|contrase[ñn]a|secret|token|api[_-]?key|access[_-]?key)"
    r"(?:[_-][a-z0-9]+)*)(\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;]{6,})"
)
REDACTED = "[REDACTED]"

#: Blocks the harness injects into user turns; the user never wrote them.
_INJECTED_BLOCKS = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr)>.*?</\1>",
    re.DOTALL,
)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_NOISE = re.compile(r"<(command-message|command-args)>.*?</\1>", re.DOTALL)
#: Cheap test run on the raw bytes before paying for ``json.loads``: transcripts
#: hold multi-megabyte lines that are of no interest here.
_WANTED_LINE = re.compile(rb'"type":\s?"(?:user|assistant|ai-title)"')


def redact(text: str) -> str:
    """Replace anything that looks like a credential with ``[REDACTED]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def clean_user_text(text: str) -> str:
    """Drop what the harness injected into a user turn; keep what the user typed."""
    text = _INJECTED_BLOCKS.sub("", text)
    text = _COMMAND_NOISE.sub("", text)
    text = _COMMAND_NAME.sub(lambda m: m.group(1).strip(), text)
    return text.strip()


def describe_action(name: str, tool_input: Any) -> Optional[str]:
    """One line for a tool call, or ``None`` for calls that only read."""
    if not name or name in _READ_ONLY_TOOLS:
        return None
    detail = ""
    if isinstance(tool_input, dict):
        for key in _ACTION_FIELDS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = " ".join(value.split())
                break
    if len(detail) > ACTION_CHARS:
        detail = detail[: ACTION_CHARS - 1] + "…"
    short = name.rsplit("__", 1)[-1]
    return f"{short}: {detail}" if detail else short


@dataclass
class Turn:
    """One side speaking: its text and, for the assistant, what it did."""

    role: str  # "user" or "assistant"
    timestamp: str = ""
    text: str = ""
    actions: List[str] = field(default_factory=list)

    def render(self, user_label: str, assistant_label: str) -> str:
        label = user_label if self.role == "user" else assistant_label
        lines = [f"{label}: {self.text}" if self.text else f"{label}:"]
        lines += [f"  · {action}" for action in self.actions]
        return "\n".join(lines)


@dataclass
class TranscriptSlice:
    """What a transcript held between two byte offsets."""

    turns: List[Turn] = field(default_factory=list)
    end_offset: int = 0
    session_id: str = ""
    cwd: str = ""
    title: str = ""


def _turn_from(record: Dict[str, Any]) -> Optional[Turn]:
    """Build the turn a transcript line holds, or ``None`` if it holds nothing said or done."""
    role = record.get("type")
    message = record.get("message")
    if role not in ("user", "assistant") or not isinstance(message, dict):
        return None
    if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    content = message.get("content")
    texts: List[str] = []
    actions: List[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "tool_use" and role == "assistant":
                action = describe_action(str(block.get("name", "")), block.get("input"))
                if action:
                    actions.append(redact(action))
    text = "\n".join(t for t in texts if t and t.strip())
    text = clean_user_text(text) if role == "user" else text.strip()
    if not text and not actions:
        return None
    return Turn(role=role, timestamp=str(record.get("timestamp") or ""), text=redact(text), actions=actions)


def read_transcript(path: Path, offset: int = 0) -> TranscriptSlice:
    """Read the turns a transcript holds from ``offset`` on.

    Only complete lines are consumed, so a transcript still being written is
    picked up where it was left on the next call. Consecutive records of the
    same side (Claude Code writes one per content block) become one turn.
    """
    result = TranscriptSlice(end_offset=offset)
    with open(path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break  # still being written
            result.end_offset += len(raw)
            if not _WANTED_LINE.search(raw):
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "ai-title":
                result.title = str(record.get("aiTitle") or result.title)
                continue
            turn = _turn_from(record)
            if turn is None:
                continue
            result.session_id = result.session_id or str(record.get("sessionId") or "")
            result.cwd = result.cwd or str(record.get("cwd") or "")
            last = result.turns[-1] if result.turns else None
            if last is not None and last.role == turn.role == "assistant":
                last.text = "\n".join(t for t in (last.text, turn.text) if t)
                last.actions.extend(turn.actions)
            else:
                result.turns.append(turn)
    return result


def render_fragments(
    turns: Iterable[Turn],
    *,
    project: str,
    session: str,
    title: str = "",
    user_label: str = "User",
    assistant_label: str = "Claude",
    fragment_tokens: int = FRAGMENT_TOKENS,
) -> List[Tuple[str, str]]:
    """Group turns into dated, self-describing fragments: ``[(timestamp, text), ...]``.

    A fragment starts at a user turn whenever the budget is spent, so a question
    stays with its answer. Each opens with a header line naming the date, the
    project and the session — a fragment found months later explains itself.
    """
    fragments: List[Tuple[str, str]] = []
    current: List[str] = []
    current_tokens = 0
    started = ""
    last_seen = ""

    def close() -> None:
        nonlocal current, current_tokens, started
        if current:
            header = " · ".join(p for p in (started[:10] or "undated", project, f"session {session}", title) if p)
            fragments.append((started, header + "\n\n" + "\n\n".join(current)))
        current, current_tokens, started = [], 0, ""

    for turn in turns:
        last_seen = turn.timestamp or last_seen
        if turn.role == "user" and current_tokens >= fragment_tokens:
            close()
        if not current:
            started = turn.timestamp or last_seen
        rendered = turn.render(user_label, assistant_label)
        current.append(rendered)
        current_tokens += estimate_tokens(rendered)
    close()
    return fragments


def project_of(cwd: str, transcript: Path) -> str:
    """A short project name: the working directory's name, else the transcript folder's."""
    name = Path(cwd).name if cwd else ""
    if not name:
        name = transcript.parent.name.rsplit("-", 1)[-1]
    return slugify(name, fallback="home", max_length=40)


@dataclass
class CaptureReport:
    """What one transcript contributed in one run."""

    transcript: str
    project: str = ""
    turns: int = 0
    fragments: int = 0
    tokens: int = 0
    skipped: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class _Lock:
    """Cross-process lock: capture runs from hooks of sessions that overlap."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def __enter__(self) -> "_Lock":
        for _ in range(2):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                break
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS
                except OSError:
                    continue  # released meanwhile: try again
                if not stale:
                    break
                try:
                    self.path.unlink()
                except OSError:
                    break
        return self

    def __exit__(self, *exc: object) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


class ConversationCapture:
    """Incremental capture of Claude Code transcripts into the conversations store."""

    def __init__(self, home: Path | str, *, user_label: str = "User", assistant_label: str = "Claude") -> None:
        """
        Args:
            home: ``IHMT_HOME`` — the conversations store is created inside it.
            user_label: Name written before the user's turns.
            assistant_label: Name written before the assistant's turns.
        """
        self.base = Path(home).expanduser() / CONVERSATIONS_DIRNAME
        self.state_path = self.base / "capture.json"
        self.user_label = user_label
        self.assistant_label = assistant_label
        self._memory: Optional[IHMT] = None

    # ------------------------------------------------------------------ store
    @property
    def memory(self) -> IHMT:
        """The conversations tree, created on first use."""
        if self._memory is None:
            self.base.mkdir(parents=True, exist_ok=True)
            if (self.base / "ihmt_memory" / "root.json").exists():
                memory = IHMT(self.base, auto_consolidate=False)
            else:
                IHMT.initialize(self.base, target_tokens=FRAGMENT_TOKENS * 2, max_tokens=FRAGMENT_TOKENS * 6)
                memory = IHMT(self.base, auto_consolidate=False)
            # Chat is full of "now I use X" that was never meant as a dated fact;
            # extracting them would stamp OUTDATED notices on unrelated fragments.
            memory.ingestor.conflict_resolver = None
            self._memory = memory
        return self._memory

    def _load_state(self) -> Dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.state_path)

    # ---------------------------------------------------------------- capture
    def capture(self, transcripts: Iterable[Path | str], *, dry_run: bool = False) -> List[CaptureReport]:
        """Ingest what is new in each transcript.

        Args:
            transcripts: Paths of ``.jsonl`` transcripts.
            dry_run: Report what would be stored without writing anything.

        Returns:
            One report per transcript that had something new (or was skipped
            for a stated reason). Transcripts with nothing new are omitted.
        """
        paths = [Path(p).expanduser() for p in transcripts]
        if dry_run:
            state = self._load_state()
            return [r for r in (self._capture_one(p, state, None) for p in paths) if r is not None]

        self.base.mkdir(parents=True, exist_ok=True)
        with _Lock(self.base / "capture.lock") as lock:
            if not lock.held:
                return [CaptureReport(transcript="", skipped="another capture is running; the next one catches up")]
            state = self._load_state()
            reports: List[CaptureReport] = []
            memory = self.memory
            with memory.store.batch():
                for path in paths:
                    report = self._capture_one(path, state, memory)
                    if report is not None:
                        reports.append(report)
                        # Saved per transcript: an interrupted backfill resumes
                        # where it stopped, and leaf ids make a replay harmless.
                        self._save_state(state)
            if any(r.fragments for r in reports):
                # Not flush(): partial groups stay pending (the trunk references
                # them), so later sessions fill them instead of leaving stubs.
                memory.consolidate()
            return reports

    def capture_all(self, projects_dir: Path | str, *, dry_run: bool = False) -> List[CaptureReport]:
        """Sweep every session transcript under a Claude Code ``projects`` directory."""
        root = Path(projects_dir).expanduser()
        return self.capture(sorted(root.glob("*/*.jsonl")), dry_run=dry_run)

    def _capture_one(self, path: Path, state: Dict[str, Any], memory: Optional[IHMT]) -> Optional[CaptureReport]:
        """Capture one transcript; ``memory`` is ``None`` on a dry run."""
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return CaptureReport(transcript=key, skipped="no such transcript")
        entry = state.get(key) if isinstance(state.get(key), dict) else {}
        offset = int(entry.get("offset", 0))
        if offset > size:
            offset = 0  # rewritten from scratch; leaf ids keep a replay from duplicating
        if offset == size:
            return None

        piece = read_transcript(path, offset)
        title = piece.title or str(entry.get("title", ""))
        session = (piece.session_id or str(entry.get("session", "")) or path.stem)[:8]
        project = project_of(piece.cwd or str(entry.get("cwd", "")), path)
        report = CaptureReport(transcript=key, project=project, turns=len(piece.turns))

        fragments = render_fragments(
            piece.turns,
            project=project,
            session=session,
            title=title,
            user_label=self.user_label,
            assistant_label=self.assistant_label,
        )
        for timestamp, text in fragments:
            report.fragments += 1
            report.tokens += estimate_tokens(text)
            if memory is not None:
                memory.ingest_text(
                    text,
                    source=f"{SOURCE_PREFIX}{project}/{session}",
                    domain=f"conversations.{project}",
                    data_type=DataType.GENERIC,
                    tags=[f"project:{project}", f"session:{session}"],
                    timestamp=timestamp or None,
                )
        state[key] = {
            "offset": piece.end_offset,
            "title": title,
            "session": piece.session_id or str(entry.get("session", "")),
            "cwd": piece.cwd or str(entry.get("cwd", "")),
        }
        if not report.turns and piece.end_offset == offset:
            return None
        return report


# ------------------------------------------------------------------------ cli
def _detach() -> bool:
    """Continue in a background process; True in the process that should do the work.

    Session hooks are given very little time (and their output may land in the
    model's context), so the hook returns at once and the capture runs detached.
    Where ``fork`` does not exist (Windows) the work simply runs inline.
    """
    if not hasattr(os, "fork"):
        return True
    if os.fork():
        return False
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(devnull, descriptor)
    return True


def add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    """The options of ``capture``, shared by ``main.py`` and the ``ihmt-memory`` command."""
    parser.add_argument("transcripts", nargs="*", help="transcript .jsonl files")
    parser.add_argument("--all", action="store_true", help="sweep every transcript under --claude-dir")
    parser.add_argument("--claude-dir", default="~/.claude/projects", help="Claude Code projects directory")
    parser.add_argument("--hook", action="store_true", help="read a Claude Code hook payload from stdin; silent")
    parser.add_argument("--detach", action="store_true", help="run in the background and return at once")
    parser.add_argument("--dry-run", action="store_true", help="report what would be stored, write nothing")
    parser.add_argument("--user-name", default="User", help="label written before the user's turns")


def run_capture(args: argparse.Namespace, home: Path | str) -> int:
    """Run ``capture`` with parsed options against the store under ``home``."""
    transcripts = list(args.transcripts)
    if args.hook:
        # A hook must never disturb the session: no output, exit code 0 whatever happens.
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            if isinstance(payload, dict) and payload.get("transcript_path"):
                transcripts.append(str(payload["transcript_path"]))
        except ValueError:
            pass
    quiet = args.hook or args.detach
    forked = quiet and not args.dry_run and hasattr(os, "fork")
    if forked and not _detach():
        return 0
    try:
        capture = ConversationCapture(home, user_label=args.user_name)
        reports = capture.capture(transcripts, dry_run=args.dry_run) if transcripts else []
        if args.all:
            reports += capture.capture_all(args.claude_dir, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001 - a traceback must not reach a hook
        if not quiet:
            raise
        if forked:
            os._exit(0)
        return 0
    if forked:
        os._exit(0)
    if quiet:
        return 0

    if getattr(args, "json", False):
        print(json.dumps([r.to_dict() for r in reports], indent=2, ensure_ascii=False))
        return 0
    for report in reports:
        if report.skipped:
            print(f"skipped {report.transcript}: {report.skipped}".replace("  ", " "))
        elif report.fragments:
            print(f"{report.project}: {report.turns} turns → {report.fragments} fragments, ~{report.tokens} tokens")
    verb = "would store" if args.dry_run else "stored"
    total = sum(r.tokens for r in reports)
    print(f"{verb} {sum(r.fragments for r in reports)} fragments (~{total} tokens) in {capture.base}")
    return 0


def cli(argv: List[str], default_home: Path | str) -> int:
    """``ihmt-memory capture ...``: the same command for an install without ``main.py``."""
    parser = argparse.ArgumentParser(prog="ihmt-memory capture", description=__doc__.splitlines()[0])
    add_capture_arguments(parser)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    return run_capture(args, os.environ.get("IHMT_HOME") or default_home)
