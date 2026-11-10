"""Capture coding-agent conversations into their own IHMT store.

Agents keep every session on disk: Claude Code as JSON lines under
``~/.claude/projects/<project>/<session>.jsonl``, Codex under
``~/.codex/sessions/<date>/rollout-*.jsonl``, opencode in a SQLite database
(``~/.local/share/opencode/opencode.db``). All three are read here, so the
memory of what was said survives a change of agent — and of model: what matters
is the agent application, not whose model it runs. Supporting another agent
means adding its location to ``SESSION_SOURCES`` and a reader for its records. Almost all of a transcript
is tool output, images and model reasoning; what the user and the assistant actually
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
import sqlite3
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .api import IHMT
from .models import DataType
from .textutils import estimate_tokens, slugify

#: Directory (inside ``IHMT_HOME``) holding the conversations store.
CONVERSATIONS_DIRNAME = "ihmt_conversations"
#: Prefix of every conversation leaf's ``source``; the project name follows it,
#: whatever agent the session ran in — a project filter spans agents.
SOURCE_PREFIX = "session://"
#: Where each agent keeps its transcripts: (environment override, default
#: directory, glob). The key is the agent's name as stored with each fragment.
SESSION_SOURCES: Dict[str, Tuple[str, str, str]] = {
    "claude": ("CLAUDE_CONFIG_DIR", "~/.claude", "projects/*/*.jsonl"),
    "codex": ("CODEX_HOME", "~/.codex", "sessions/**/rollout-*.jsonl"),
    "opencode": ("OPENCODE_DATA_DIR", "~/.local/share/opencode", "opencode.db"),
}
#: Name written before each agent's turns.
AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "opencode": "opencode"}
#: Size of one stored fragment. Small on purpose: a search answers with a whole
#: fragment, so this is also what a hit costs in context.
FRAGMENT_TOKENS = 600
#: Longest action line kept for a tool call.
ACTION_CHARS = 140
#: A lock older than this belonged to a run that died.
LOCK_STALE_SECONDS = 15 * 60

#: Tools that only look at things. Listing them would bury the actions that
#: changed something.
_READ_ONLY_TOOLS = frozenset(
    {"read", "grep", "glob", "toolsearch", "todowrite", "todoread", "ls", "list", "listagents", "wait",
     "update_plan", "view_image"}
)
#: Input fields that best describe a tool call, most telling first.
_ACTION_FIELDS = ("description", "file_path", "filePath", "command", "cmd", "path", "url", "query", "prompt", "skill", "content")

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
_WANTED_LINE = re.compile(
    rb'"type":\s?"(?:user|assistant|ai-title|session_meta|user_message|agent_message'
    rb'|function_call|custom_tool_call|local_shell_call)"'
)


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
    if not name or name.lower() in _READ_ONLY_TOOLS:
        return None
    detail = ""
    if isinstance(tool_input, dict):
        for key in _ACTION_FIELDS:
            value = tool_input.get(key)
            if isinstance(value, list):  # a command given as argv
                value = " ".join(str(part) for part in value)
            if isinstance(value, str) and value.strip():
                detail = " ".join(value.split())
                break
    elif isinstance(tool_input, str):
        detail = " ".join(tool_input.split())
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
    agent: str = "claude"


def _codex_turn(record: Dict[str, Any]) -> Optional[Turn]:
    """The turn a Codex rollout line holds, if any.

    Codex writes each message twice: as a ``response_item`` (the raw model
    exchange, with injected instructions) and as an ``event_msg`` (what was
    shown). The events are read for text; the items only for tool calls.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    timestamp = str(record.get("timestamp") or "")
    if record.get("type") == "event_msg" and kind in ("user_message", "agent_message"):
        text = payload.get("message")
        if not isinstance(text, str) or not text.strip():
            return None
        if kind == "user_message":
            text = clean_user_text(text)
            return Turn(role="user", timestamp=timestamp, text=redact(text)) if text else None
        return Turn(role="assistant", timestamp=timestamp, text=redact(text.strip()))
    if record.get("type") == "response_item" and kind in ("function_call", "custom_tool_call", "local_shell_call"):
        tool_input: Any = payload.get("arguments", payload.get("input", payload.get("action")))
        if isinstance(tool_input, str) and tool_input.lstrip().startswith("{"):
            try:
                tool_input = json.loads(tool_input)
            except ValueError:
                pass
        action = describe_action(str(payload.get("name") or "shell"), tool_input)
        return Turn(role="assistant", timestamp=timestamp, actions=[redact(action)]) if action else None
    return None


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
            if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
                meta = record["payload"]
                result.agent = "codex"
                result.session_id = str(meta.get("id") or result.session_id)
                result.cwd = str(meta.get("cwd") or result.cwd)
                continue
            if "payload" in record:
                result.agent = "codex"
                turn = _codex_turn(record)
            else:
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
    agent: str = "",
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
            parts = (started[:10] or "undated", project, f"{agent} session {session}".strip(), title)
            header = " · ".join(p for p in parts if p)
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
    if not name and transcript.parent.name.startswith("-"):  # Claude Code: the path with dashes
        name = transcript.parent.name.rsplit("-", 1)[-1]
    return slugify(name, fallback="home", max_length=40)


def _iso(milliseconds: Any) -> str:
    """An epoch in milliseconds as the ISO timestamp the store uses."""
    try:
        moment = datetime.fromtimestamp(int(milliseconds) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(text: Any) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_opencode(database: Path, seen: Dict[str, int]) -> List[Tuple[TranscriptSlice, int]]:
    """Read what is new in an opencode database: ``[(slice, newest message time), ...]``.

    Args:
        database: Path of ``opencode.db``. Opened read-only.
        seen: Session id → creation time of the last message already captured.

    Sub-agent sessions are skipped, like Claude Code's side chains, and so is an
    assistant message still being produced — the next run takes it whole.

    Raises:
        sqlite3.Error: If the database cannot be read or has another layout.
    """
    slices: List[Tuple[TranscriptSlice, int]] = []
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        sessions = connection.execute(
            "SELECT id, directory, title FROM session WHERE parent_id IS NULL ORDER BY time_created"
        ).fetchall()
        for session_id, directory, title in sessions:
            last = int(seen.get(session_id, 0))
            piece = TranscriptSlice(session_id=session_id, cwd=directory or "", title=title or "", agent="opencode")
            newest = last
            messages = connection.execute(
                "SELECT id, time_created, data FROM message WHERE session_id = ? AND time_created > ? "
                "ORDER BY time_created, id",
                (session_id, last),
            ).fetchall()
            for message_id, created, data in messages:
                message = _json(data)
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                times = message.get("time") if isinstance(message.get("time"), dict) else {}
                if role == "assistant" and not times.get("completed") and not message.get("error"):
                    break  # still being written
                texts: List[str] = []
                actions: List[str] = []
                parts = connection.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id", (message_id,)
                )
                for (part_data,) in parts:
                    part = _json(part_data)
                    if part.get("type") == "text" and isinstance(part.get("text"), str) and not part.get("synthetic"):
                        texts.append(part["text"])
                    elif part.get("type") == "tool" and role == "assistant":
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        action = describe_action(str(part.get("tool") or ""), state.get("input"))
                        if action:
                            actions.append(redact(action))
                text = "\n".join(t for t in texts if t.strip())
                text = clean_user_text(text) if role == "user" else text.strip()
                newest = max(newest, int(created))
                if not text and not actions:
                    continue
                turn = Turn(role=role, timestamp=_iso(created), text=redact(text), actions=actions)
                previous = piece.turns[-1] if piece.turns else None
                if previous is not None and previous.role == role == "assistant":
                    previous.text = "\n".join(t for t in (previous.text, turn.text) if t)
                    previous.actions.extend(turn.actions)
                else:
                    piece.turns.append(turn)
            if newest > last:
                slices.append((piece, newest))
    finally:
        connection.close()
    return slices


def session_transcripts(roots: Optional[Dict[str, Path | str]] = None) -> List[Path]:
    """Every session transcript of every known agent, oldest path first per agent."""
    found: List[Path] = []
    for agent, (variable, default, pattern) in SESSION_SOURCES.items():
        root = Path((roots or {}).get(agent) or os.environ.get(variable) or default).expanduser()
        if root.is_dir():
            found.extend(sorted(root.glob(pattern)))
    return found


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


def _process_is_gone(pid: int) -> bool:
    """Whether no process has this id. Only asked where asking is harmless (POSIX)."""
    if os.name != "posix" or pid <= 0:
        return False  # on Windows os.kill(pid, 0) would terminate it: rely on the age instead
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # it exists but belongs to someone else
    return False


class _Lock:
    """Cross-process lock: captures start from servers and hooks of sessions that overlap.

    The file holds the owner's process id. A capture can die without releasing
    it — the server sweeps in a daemon thread, killed when the agent closes — so
    a lock whose owner no longer exists, or older than ``LOCK_STALE_SECONDS``,
    is taken over instead of blocking every later capture.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def _abandoned(self) -> bool:
        try:
            if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                return True
            owner = self.path.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            return False
        if not owner.isdigit():
            # No owner written: either a lock from before owners were recorded,
            # or one being created right now. Give the second case a moment.
            try:
                return time.time() - self.path.stat().st_mtime > 5
            except OSError:
                return False
        return int(owner) != os.getpid() and _process_is_gone(int(owner))

    def __enter__(self) -> "_Lock":
        for _ in range(3):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._abandoned():
                    break
                try:
                    self.path.unlink()
                except OSError:
                    break
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            self.held = True
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

    def __init__(self, home: Path | str, *, user_label: str = "User") -> None:
        """
        Args:
            home: ``IHMT_HOME`` — the conversations store is created inside it.
            user_label: Name written before the user's turns.
        """
        self.base = Path(home).expanduser() / CONVERSATIONS_DIRNAME
        self.state_path = self.base / "capture.json"
        self.user_label = user_label
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
            return [r for p in paths for r in self._capture_one(p, state, None) if r.turns or r.skipped]

        self.base.mkdir(parents=True, exist_ok=True)
        with _Lock(self.base / "capture.lock") as lock:
            if not lock.held:
                return [CaptureReport(transcript="", skipped="another capture is running; the next one catches up")]
            state = self._load_state()
            reports: List[CaptureReport] = []
            memory = self.memory
            for path in paths:
                # One batch per transcript, and the state only after it: the
                # catalog is on disk before the offset says "already read", so
                # a run killed halfway (a server shutting down) replays the
                # transcript instead of losing it — leaf ids make that harmless.
                with memory.store.batch():
                    found = self._capture_one(path, state, memory)
                reports.extend(r for r in found if r.turns or r.skipped)
                if found:
                    self._save_state(state)
            if any(r.fragments for r in reports):
                # Not flush(): partial groups stay pending (the trunk references
                # them), so later sessions fill them instead of leaving stubs.
                memory.consolidate()
            return reports

    def capture_all(
        self, roots: Optional[Dict[str, Path | str]] = None, *, dry_run: bool = False
    ) -> List[CaptureReport]:
        """Sweep the session transcripts of every known agent.

        Args:
            roots: Agent name → its configuration directory, to override where
                transcripts are looked for (``{"claude": "~/.claude"}``). Agents
                left out use their environment variable or default directory;
                a directory that does not exist is simply skipped.
            dry_run: Report what would be stored without writing anything.
        """
        return self.capture(session_transcripts(roots), dry_run=dry_run)

    def _capture_one(self, path: Path, state: Dict[str, Any], memory: Optional[IHMT]) -> List[CaptureReport]:
        """Capture one transcript or session database; ``memory`` is ``None`` on a dry run."""
        if path.suffix == ".db":
            return self._capture_database(path, state, memory)
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return [CaptureReport(transcript=key, skipped="no such transcript")]
        entry = state.get(key) if isinstance(state.get(key), dict) else {}
        offset = int(entry.get("offset", 0))
        if offset > size:
            offset = 0  # rewritten from scratch; leaf ids keep a replay from duplicating
        if offset == size:
            return []

        piece = read_transcript(path, offset)
        piece.title = piece.title or str(entry.get("title", ""))
        piece.session_id = piece.session_id or str(entry.get("session", "")) or path.stem
        piece.cwd = piece.cwd or str(entry.get("cwd", ""))
        if not piece.turns and entry.get("agent"):
            piece.agent = str(entry["agent"])
        report = self._store(piece, key, project_of(piece.cwd, path), memory)
        state[key] = {
            "offset": piece.end_offset,
            "title": piece.title,
            "session": piece.session_id,
            "cwd": piece.cwd,
            "agent": piece.agent,
        }
        return [report]

    def _capture_database(self, path: Path, state: Dict[str, Any], memory: Optional[IHMT]) -> List[CaptureReport]:
        """Capture the sessions of an opencode database."""
        key = str(path)
        if not path.is_file():
            return [CaptureReport(transcript=key, skipped="no such transcript")]
        entry = state.get(key) if isinstance(state.get(key), dict) else {}
        seen = dict(entry.get("sessions") or {})
        try:
            slices = read_opencode(path, seen)
        except sqlite3.Error as exc:
            return [CaptureReport(transcript=key, skipped=f"unreadable session database ({exc})")]
        reports = []
        for piece, newest in slices:
            label = f"{key}#{piece.session_id}"
            reports.append(self._store(piece, label, project_of(piece.cwd, path), memory))
            seen[piece.session_id] = newest
        state[key] = {"sessions": seen, "agent": "opencode"}
        return reports

    def _store(self, piece: TranscriptSlice, label: str, project: str, memory: Optional[IHMT]) -> CaptureReport:
        """Render a slice into fragments and ingest them (unless ``memory`` is ``None``)."""
        agent = piece.agent
        # Agents number sessions differently; the distinctive part is the tail for
        # opencode ("ses_…") and the head for the others.
        session = piece.session_id.rsplit("_", 1)[-1][:8] if agent == "opencode" else piece.session_id[:8]
        report = CaptureReport(transcript=label, project=project, turns=len(piece.turns))
        fragments = render_fragments(
            piece.turns,
            project=project,
            session=session,
            title=piece.title,
            user_label=self.user_label,
            assistant_label=AGENT_LABELS.get(agent, agent.capitalize()),
            agent="" if agent == "claude" else agent,
        )
        for timestamp, text in fragments:
            report.fragments += 1
            report.tokens += estimate_tokens(text)
            if memory is not None:
                memory.ingest_text(
                    text,
                    source=f"{SOURCE_PREFIX}{project}/{agent}-{session}",
                    domain=f"conversations.{project}",
                    data_type=DataType.GENERIC,
                    tags=[f"project:{project}", f"session:{session}", f"agent:{agent}"],
                    timestamp=timestamp or None,
                )
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
    parser.add_argument("--all", action="store_true", help="sweep the sessions of every known agent")
    parser.add_argument("--claude-dir", default=None, help="Claude Code configuration directory (~/.claude)")
    parser.add_argument("--codex-dir", default=None, help="Codex configuration directory (~/.codex)")
    parser.add_argument("--opencode-dir", default=None, help="opencode data directory (~/.local/share/opencode)")
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
            roots = {"claude": args.claude_dir, "codex": args.codex_dir, "opencode": args.opencode_dir}
            reports += capture.capture_all({k: v for k, v in roots.items() if v}, dry_run=args.dry_run)
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
