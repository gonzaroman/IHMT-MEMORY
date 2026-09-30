"""MCP server registration: detect the state, preview and apply.

This is the part that motivated the interface. Registering IHMT in Claude Code
otherwise means writing absolute paths into a JSON file or typing a four-line
command, and choosing blindly between two scopes:

* **project** — a ``.mcp.json`` in the project folder. The memory travels with
  the project and nothing else sees it.
* **user** — ``claude mcp add --scope user``. A single memory, available from
  any folder.

The flow is always the same: :func:`detect_state` says how things stand,
:func:`build_plan` builds *exactly* what would be done and returns it to show
on screen, and :func:`apply_plan` only runs if the person presses *Apply*.
Commands are launched as an argument list, never through a shell.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Name the server is registered under.
SERVER_NAME = "ihmt-memory"

#: Repository root (where ``mcp_server.py`` and, if present, ``.venv`` live).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: How long we wait for the Claude Code CLI, which runs health checks.
CLAUDE_TIMEOUT = 25.0

#: Seconds the ``claude mcp list`` answer is reused. Querying it costs ~1.5 s
#: because it health-checks every server, and the interface needs it several
#: times in a row (preview, apply, refresh state).
CLI_CACHE_TTL = 15.0

#: Cache for that query: ``(instant, probe)``.
_cli_cache: Optional[Tuple[float, "Probe"]] = None


def invalidate_cache() -> None:
    """Forget what was cached. Called after applying changes."""
    global _cli_cache
    _cli_cache = None
    _has_mcp.cache_clear()


@dataclass
class Interpreter:
    """Python interpreter that will run the MCP server."""

    path: str
    has_mcp: bool
    is_venv: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "has_mcp": self.has_mcp, "is_venv": self.is_venv}


class Verdict(str, Enum):
    """Single answer to "does the memory work, yes or no?".

    It exists because the screen used to show four separate signals
    (registered for the project, registered for the user, server health, SDK
    present) that the reader had to combine in their head… and that
    contradicted each other on top of that.
    """

    WORKING = "WORKING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    NOT_REGISTERED = "NOT_REGISTERED"
    SDK_MISSING = "SDK_MISSING"
    CLI_MISSING = "CLI_MISSING"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Probe:
    """What the CLI knows about *our* server, and only about it."""

    known: bool
    status: str  # "connected" | "pending" | "failed" | "unknown" | "unavailable"
    scope: Optional[str] = None  # "project" | "user" | "local"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"known": self.known, "status": self.status, "scope": self.scope, "detail": self.detail}


@dataclass
class SetupState:
    """Snapshot of how the server is configured right now."""

    claude_cli: Optional[str]
    interpreter: Interpreter
    server_script: str
    project_dir: str
    project_registered: bool
    probe: Probe
    verdict: Verdict = Verdict.UNKNOWN
    registered_home: Optional[str] = None
    memory_ready: bool = False
    notes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def effective_scope(self) -> Optional[str]:
        """The scope actually in charge, according to the CLI itself."""
        return self.probe.scope

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claude_cli": self.claude_cli,
            "interpreter": self.interpreter.to_dict(),
            "server_script": self.server_script,
            "project_dir": self.project_dir,
            "project_registered": self.project_registered,
            "verdict": self.verdict.value,
            "effective_scope": self.effective_scope,
            "registered_home": self.registered_home,
            "memory_ready": self.memory_ready,
            "detail": self.probe.detail,
            "notes": self.notes,
            "problems": self.problems,
        }


@dataclass
class SetupPlan:
    """Exactly what would be done, ready to show before touching anything."""

    scope: str  # "project" | "user"
    kind: str  # "file" | "command"
    memory_home: str
    preview: str
    command: List[str] = field(default_factory=list)
    file_path: str = ""
    file_content: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "kind": self.kind,
            "memory_home": self.memory_home,
            "preview": self.preview,
            "command": self.command,
            "file_path": self.file_path,
            "file_content": self.file_content,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------ detection
def find_interpreter() -> Interpreter:
    """Choose which Python starts the MCP server.

    Prefers the project's ``.venv``, which is where the SDK gets installed; if
    it lacks the ``mcp`` package, it falls back to the current interpreter and
    reports the problem.
    """
    candidates: List[Path] = []
    for relative in (".venv/bin/python", ".venv/Scripts/python.exe"):
        path = PROJECT_ROOT / relative
        if path.exists():
            candidates.append(path)
    candidates.append(Path(sys.executable))

    first: Optional[Interpreter] = None
    for candidate in candidates:
        is_venv = ".venv" in candidate.parts
        has = _has_mcp(candidate)
        interpreter = Interpreter(path=str(candidate), has_mcp=has, is_venv=is_venv)
        if has:
            return interpreter
        first = first or interpreter
    return first or Interpreter(path=sys.executable, has_mcp=False, is_venv=False)


@functools.lru_cache(maxsize=8)
def _has_mcp(interpreter: Path) -> bool:
    """Check whether that interpreter can import the MCP SDK.

    Cached: launching an interpreter costs half a second and the answer does
    not change while the process lives, unless the SDK gets installed — that
    is what :func:`invalidate_cache` is for.
    """
    try:
        result = subprocess.run(
            [str(interpreter), "-c", "import mcp"],
            capture_output=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def project_config_path(project_dir: Path) -> Path:
    """Path of a project's ``.mcp.json``."""
    return Path(project_dir) / ".mcp.json"


def read_project_config(project_dir: Path) -> Dict[str, Any]:
    """Read the project's ``.mcp.json``; return ``{}`` if missing or broken."""
    path = project_config_path(project_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def detect_state(project_dir: Path | str) -> SetupState:
    """Diagnose for real whether the server is operational."""
    project = Path(project_dir).expanduser().resolve()
    interpreter = find_interpreter()
    server = PROJECT_ROOT / "mcp_server.py"
    problems: List[str] = []
    notes: List[str] = []

    if not server.exists():  # pragma: no cover - only if the file is missing
        problems.append("missing_server_script")
    if not interpreter.has_mcp:
        problems.append("missing_mcp_sdk")

    config = read_project_config(project)
    entry = (config.get("mcpServers") or {}).get(SERVER_NAME) or {}
    project_registered = bool(entry)
    registered_home = (entry.get("env") or {}).get("IHMT_HOME")

    cli = shutil.which("claude")
    if cli is None:
        problems.append("missing_claude_cli")

    probe = probe_server(cli) if cli else Probe(known=False, status="unavailable")

    # The CLI is the authority on which registration is in charge. If there is
    # also a project one and a different one governs, it is worth saying so:
    # it is a classic source of "but I did register it".
    if project_registered and probe.scope and probe.scope != "project":
        notes.append("duplicate_registration")

    home = registered_home or str(project)
    memory_ready = (Path(home) / "ihmt_memory" / "root.json").exists()
    if probe.known and not memory_ready:
        notes.append("memory_not_created_yet")

    # A registration in .mcp.json needs explicit approval every time, because
    # that file may come inside someone else's repository. A user-scope one is
    # added by the person themselves, so it is trusted and just works. Whoever
    # gets stuck approving deserves to know that way out exists.
    if probe.status == "pending" and probe.scope == "project":
        notes.append("user_scope_needs_no_approval")

    return SetupState(
        claude_cli=cli,
        interpreter=interpreter,
        server_script=str(server),
        project_dir=str(project),
        project_registered=project_registered,
        probe=probe,
        verdict=_decide(probe, interpreter, cli),
        registered_home=registered_home,
        memory_ready=memory_ready,
        notes=notes,
        problems=problems,
    )


def _decide(probe: Probe, interpreter: Interpreter, cli: Optional[str]) -> Verdict:
    """Combine the separate signals into a single answer.

    Order matters: without the SDK the server will not start however
    "connected" the CLI says it is, so that check comes before any other.
    """
    if cli is None:
        return Verdict.CLI_MISSING
    if not interpreter.has_mcp:
        return Verdict.SDK_MISSING
    if not probe.known:
        return Verdict.NOT_REGISTERED
    return {
        "connected": Verdict.WORKING,
        "pending": Verdict.PENDING_APPROVAL,
        "failed": Verdict.FAILED,
    }.get(probe.status, Verdict.UNKNOWN)


def probe_server(cli: str) -> Probe:
    """Ask the CLI about *our* server with ``claude mcp get``.

    ``get`` is used rather than ``list`` for two reasons: ``list`` mixes in the
    user's other servers (noise, and data that is none of our business) and
    does not say which scope each one belongs to — which is exactly what we
    needed to know.

    The answer is reused for :data:`CLI_CACHE_TTL` seconds.
    """
    global _cli_cache
    now = time.monotonic()
    if _cli_cache is not None and (now - _cli_cache[0]) < CLI_CACHE_TTL:
        return _cli_cache[1]

    try:
        result = subprocess.run(
            [cli, "mcp", "get", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return Probe(known=False, status="unavailable")

    probe = parse_probe(f"{result.stdout}{result.stderr}".strip())
    _cli_cache = (now, probe)
    return probe


def parse_probe(output: str) -> Probe:
    """Interpret the output of ``claude mcp get``.

    Two lessons learned from the real CLI: when the server does not exist it
    answers ``No MCP server named …`` **with exit code 0**, so the text has to
    be inspected; and statuses come with emoji (``⏸ Pending approval``), so
    recognition goes by keywords. A status that does not fit is returned as
    ``unknown`` with its text intact: we would rather admit we do not
    understand it than claim everything is fine.
    """
    if not output:
        return Probe(known=False, status="unavailable")
    if "no mcp server named" in output.lower():
        return Probe(known=False, status="absent", detail=output)

    lower = output.lower()
    if "pending approval" in lower:
        status = "pending"
    elif "connected" in lower:
        status = "connected"
    elif "failed" in lower or "error" in lower:
        status = "failed"
    else:
        status = "unknown"

    return Probe(known=True, status=status, scope=_parse_scope(output), detail=output)


def _parse_scope(output: str) -> Optional[str]:
    """Extract the scope from the ``Scope:`` line.

    Beware of substrings: the CLI writes
    ``Scope: User config (available in all your projects)``, where the word
    "projects" appears inside the description of a *user* scope. Searching
    for a bare "project" gave the opposite of the real scope, so this requires
    the form ``<scope> config`` and, as a fallback, the first word.
    """
    for line in output.splitlines():
        if not line.strip().lower().startswith("scope:"):
            continue
        value = line.split(":", 1)[1].strip().lower()

        exact = re.search(r"\b(project|user|local)\s+config\b", value)
        if exact:
            return exact.group(1)

        first = value.split()[0] if value.split() else ""
        return first if first in ("project", "user", "local") else None
    return None


# --------------------------------------------------------------- construction
def build_plan(scope: str, memory_home: Path | str, project_dir: Path | str) -> SetupPlan:
    """Build what would be done, without doing anything.

    Args:
        scope: ``"project"`` (a ``.mcp.json``) or ``"user"`` (``claude mcp add``).
        memory_home: Folder that will contain ``ihmt_memory``.
        project_dir: Project the ``"project"`` scope applies to.

    Returns:
        A :class:`SetupPlan` with the exact text to show on screen.

    Raises:
        ValueError: If the scope is not one of the two accepted.
    """
    if scope not in ("project", "user"):
        raise ValueError(f"unknown scope: {scope!r}")

    memory = Path(memory_home).expanduser().resolve()
    project = Path(project_dir).expanduser().resolve()
    state = detect_state(project)
    warnings: List[str] = list(state.problems)

    if scope == "project":
        config = read_project_config(project)
        servers = dict(config.get("mcpServers") or {})
        others = [n for n in servers if n != SERVER_NAME]
        servers[SERVER_NAME] = {
            "command": state.interpreter.path,
            "args": [state.server_script],
            "env": {"IHMT_HOME": str(memory)},
        }
        config["mcpServers"] = servers
        content = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        if others:
            warnings.append("keeps_other_servers")
        return SetupPlan(
            scope=scope,
            kind="file",
            memory_home=str(memory),
            preview=content,
            file_path=str(project_config_path(project)),
            file_content=content,
            warnings=warnings,
        )

    command = [
        state.claude_cli or "claude",
        "mcp",
        "add",
        SERVER_NAME,
        "--scope",
        "user",
        "-e",
        f"IHMT_HOME={memory}",
        "--",
        state.interpreter.path,
        state.server_script,
    ]
    if state.probe.known and state.effective_scope == "user":
        warnings.append("replaces_existing")

    # It runs with the absolute path (more robust if the process PATH differs),
    # but it is shown with a bare "claude": the real path usually comes from a
    # Node version manager and stops existing when that gets updated, so
    # copying it would hand someone a command with an expiry date.
    visible = ["claude", *command[1:]]
    return SetupPlan(
        scope=scope,
        kind="command",
        memory_home=str(memory),
        preview=" ".join(_quote(part) for part in visible),
        command=command,
        warnings=warnings,
    )


def _quote(part: str) -> str:
    """Quote an argument only when it needs it, so the command can be copied."""
    return f'"{part}"' if " " in part else part


# ---------------------------------------------------------------- application
def apply_plan(plan: SetupPlan) -> Dict[str, Any]:
    """Execute the plan. The only function in the module that writes anything.

    Returns:
        ``{"ok": bool, "output": str, "error": str}``. Never raises: failures
        come back as text so they can be shown on screen.
    """
    result = _write_project_config(plan) if plan.kind == "file" else _run_command(plan)
    # The cache is no longer valid: we just changed the registration.
    invalidate_cache()
    return result


def _write_project_config(plan: SetupPlan) -> Dict[str, Any]:
    """Write the project's ``.mcp.json`` atomically."""
    target = Path(plan.file_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(plan.file_content, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        return {"ok": False, "output": "", "error": str(exc)}
    return {"ok": True, "output": f"{target}", "error": ""}


def _run_command(plan: SetupPlan) -> Dict[str, Any]:
    """Launch ``claude mcp add`` as an argument list (never through a shell)."""
    if not plan.command:
        return {"ok": False, "output": "", "error": "empty_command"}
    if shutil.which(plan.command[0]) is None and not Path(plan.command[0]).exists():
        return {"ok": False, "output": "", "error": "missing_claude_cli"}
    try:
        result = subprocess.run(
            plan.command,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "output": "", "error": str(exc)}
    output = f"{result.stdout}{result.stderr}".strip()
    return {"ok": result.returncode == 0, "output": output, "error": "" if result.returncode == 0 else output}
