#!/usr/bin/env python3
"""MCP server exposing the IHMT memory tree to an LLM client.

Three families of tools are published over stdio:

* **Long-term memory** (across sessions):
  ``search_memory(query, clue, detail)`` walks the tree via
  :class:`SemanticNavigator` and never guesses on an ambiguous query;
  ``save_memory(content, domain, content_type)`` stores new knowledge through
  :class:`UniversalIngestor`.
* **Project index** (saves tokens *within* a session): ``project_map`` returns a
  compact map of a source tree, ``find_code`` returns just the symbol that
  answers a question (``file:lines`` + code), and ``read_file`` answers
  "unchanged" or a diff when a file is read again. The index lives in its own
  store per project under ``$IHMT_PROJECTS_DIR`` and resyncs by checksum on
  every call, so it never serves stale code.
* **Session memory** (scratch notes for this session only): ``note``,
  ``recall`` and ``digest_output``. Held in a temporary store that disappears
  when the server process — i.e. the client session — ends.

Run it directly (``python mcp_server.py``) or let the MCP client launch it; see
``.mcp.json`` in the project root for the registration snippet.

The long-term store is ``$IHMT_HOME/ihmt_memory`` — ``IHMT_HOME`` defaults to
the directory containing this file — and is created on first use.

Requires the MCP SDK (``pip install "mcp[cli]"``); the IHMT core itself stays
dependency-free.

IMPORTANT for maintainers: stdout is the JSON-RPC channel. Never ``print()``
here — diagnostics go to stderr.
"""

from __future__ import annotations

import atexit
import difflib
import hashlib
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------- SDK
# MCP 2.x renamed FastMCP to MCPServer. Both spellings, plus the standalone
# fastmcp distribution, expose the same `.tool()` / `.run()` surface used here.
try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - depends on the installed SDK
    try:  # MCP SDK 1.x
        from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]
    except ImportError:
        try:  # standalone fastmcp distribution
            from fastmcp import FastMCP as _Server  # type: ignore[assignment]
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "The MCP SDK is required to run the IHMT memory server.\n"
                '  pip install "mcp[cli]"\n'
                f"(import failed: {exc})"
            ) from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ihmt import IHMT  # noqa: E402  (path setup must precede the import)
from ihmt.models import DataType, IHMTError  # noqa: E402
from ihmt.project_index import ProjectIndex  # noqa: E402
from ihmt.semantic_navigator import SCOPES, SearchResponse  # noqa: E402
from ihmt.textutils import estimate_tokens  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="ihmt-mcp: %(message)s")
logger = logging.getLogger("ihmt.mcp")

#: Where the long-term store lives. Override with IHMT_HOME to share one memory
#: across several projects, or to keep a separate memory per project.
IHMT_HOME = Path(os.environ.get("IHMT_HOME", Path(__file__).resolve().parent)).expanduser()
#: Where per-project indexes live. They are derived data: safe to delete.
IHMT_PROJECTS_DIR = Path(os.environ.get("IHMT_PROJECTS_DIR", IHMT_HOME / "ihmt_projects")).expanduser()

#: Result-shaping limits, chosen to keep a tool response affordable in context.
MAX_RESULTS = 3
FULL_TEXT_LIMIT = 6000
EXCERPT_LIMIT = 400
MAX_READ_BYTES = 512 * 1024
DIGEST_CHAR_LIMIT = 2400

SERVER_INSTRUCTIONS = """\
IHMT is a hierarchical memory tree on the local filesystem.

Long-term memory: `search_memory` before answering anything that depends on
earlier sessions (setup, decisions, preferences, people); `save_memory` for
durable facts and decisions. Never guess on AMBIGUOUS (retry with `clue`) and
always relay OUTDATED notices.

Saving tokens inside a session: orient yourself in a codebase with
`project_map` instead of listing and reading files; use `find_code` to get just
the method that answers a question; re-read files with `read_file`, which
answers "unchanged" or a diff. Keep what you learn with `note`/`recall`, and
pass long logs through `digest_output`.
"""

server = _Server(
    name="ihmt-memory",
    instructions=SERVER_INSTRUCTIONS,
    version="1.1.0",
)

_memory: Optional[IHMT] = None
_projects: Dict[str, ProjectIndex] = {}
_delivered: Dict[str, Tuple[str, str]] = {}  # resolved path -> (sha256, text) handed out this session
_session: Optional[IHMT] = None
_session_dir: Optional[tempfile.TemporaryDirectory] = None
_session_counter = 0


def get_memory() -> IHMT:
    """Return the shared long-term facade, creating the store on first use.

    Cached state is dropped on every call so the server never serves a stale
    catalog after the CLI (or another process) has written to the same store.
    """
    global _memory
    if _memory is None:
        store_dir = IHMT_HOME / "ihmt_memory"
        if not (store_dir / "root.json").exists():
            logger.info("initializing new IHMT store at %s", store_dir)
            _memory = IHMT.initialize(IHMT_HOME)
        else:
            _memory = IHMT(IHMT_HOME)
        logger.info("store ready at %s (%s leaves)", store_dir, _memory.stats()["leaves"])
    else:
        _memory.store.reload()
        _memory.resolver.reload()
    return _memory


def get_project(path: str) -> ProjectIndex:
    """Open (once per session) and resync the index of the project at ``path``."""
    root = Path(path).expanduser().resolve()
    index = _projects.get(str(root))
    if index is None:
        index = ProjectIndex.for_project(root, IHMT_PROJECTS_DIR)
        _projects[str(root)] = index
    report = index.sync()
    if report.touched:
        logger.info("project %s: %s", root, report.summary())
    return index


def get_session_memory() -> IHMT:
    """A scratch store that lives exactly as long as this server process."""
    global _session, _session_dir
    if _session is None:
        _session_dir = tempfile.TemporaryDirectory(prefix="ihmt-session-")
        atexit.register(_session_dir.cleanup)
        _session = IHMT.initialize(_session_dir.name, branch_factor=4, target_tokens=400, max_tokens=1200)
    return _session


# ------------------------------------------------------------------ rendering
def _format_results(response: SearchResponse) -> str:
    """Render a successful search in full: metadata plus excerpts of the rivals."""
    lines = [
        f"{len(response.results)} memory match(es) for \"{response.query}\" "
        f"(confidence {response.confidence:.2f}; "
        f"{response.node_reads} branch files read, depth {response.depth}).",
    ]

    for rank, result in enumerate(response.results[:MAX_RESULTS], start=1):
        date = result.timestamp[:10] or "undated"
        lines.append("")
        lines.append(f"--- {rank}. [{result.domain}] {date} · {result.leaf_id}")
        lines.append(f"    source: {result.file}")
        lines.append(f"    path:   {' > '.join(result.path)}")
        for notice in result.notices:
            lines.append(f"    OUTDATED: {notice}")
        if rank == 1:
            body = result.content or result.excerpt
            if len(body) > FULL_TEXT_LIMIT:
                body = body[:FULL_TEXT_LIMIT] + "\n[... truncated; narrow the query for the rest]"
            lines.append("")
            lines.append(body.rstrip())
        else:
            lines.append(f"    excerpt: {result.excerpt[:EXCERPT_LIMIT]}")

    if len(response.results) > 1:
        lines.append("")
        lines.append("(Full text shown for the top match only. Refine the query to promote another.)")
    return "\n".join(lines)


def _format_compact(response: SearchResponse) -> str:
    """Render a search with no bookkeeping: the best memory, and one line per rival.

    Dates and OUTDATED notices are kept — they change the meaning of an answer.
    Internal ids, file paths and tree paths are dropped: the model never needs them.
    """
    best = response.results[0]
    body = (best.content or best.excerpt).rstrip()
    if len(body) > FULL_TEXT_LIMIT:
        body = body[:FULL_TEXT_LIMIT] + "\n[... truncated; narrow the query for the rest]"
    lines = [f"[{best.domain}] {best.timestamp[:10] or 'undated'} (confidence {response.confidence:.2f})"]
    lines += [f"OUTDATED: {notice}" for notice in best.notices]
    lines.append(body)
    others = response.results[1:MAX_RESULTS]
    if others:
        lines.append("")
        lines.append(
            "Also matching: "
            + " | ".join(f"[{r.domain}] {r.timestamp[:10] or 'undated'} · {r.title}" for r in others)
        )
    return "\n".join(lines)


def _format_clue_request(response: SearchResponse) -> str:
    """Render an ambiguous search as a request for disambiguation."""
    request = response.clue_request
    assert request is not None  # guarded by the caller
    lines = [
        f'AMBIGUOUS: "{request.query}" — {request.reason} (confidence {request.confidence:.2f}).',
        "Do not pick one at random. Either ask the user which they mean, or call",
        "search_memory again with `clue` set to a distinguishing detail (a place,",
        "a date, a project, a person).",
        "",
        "Competing memories:",
    ]
    for index, option in enumerate(request.options, start=1):
        lines.append(f"  {index}. [{option.domain}] {option.title}")
        lines.append(f"     {option.hint}")
    return "\n".join(lines)


def _search(memory: IHMT, query: str, clue: Optional[str], detail: str, empty_hint: str) -> str:
    """Shared body of ``search_memory`` and ``recall``."""
    if not query or not query.strip():
        return "Provide a non-empty query."
    if detail not in ("compact", "full"):
        return 'detail must be "compact" or "full".'
    response = memory.search(query, top_k=MAX_RESULTS, clue=clue)
    if not response.results:
        return f'No memory found for "{query}"' + (f' (with clue "{clue}")' if clue else "") + f". {empty_hint}"
    if response.needs_clue:
        return _format_clue_request(response)
    return _format_compact(response) if detail == "compact" else _format_results(response)


# ------------------------------------------------------ long-term memory tools
@server.tool()
def search_memory(query: str, clue: Optional[str] = None, detail: str = "compact") -> str:
    """Search the user's long-term memory (IHMT): decisions, setup, preferences, people, earlier sessions.

    Use several distinctive words, not a bare name. Returns the best memory with its date
    (plus one line per other match). AMBIGUOUS means several memories fit equally: ask the
    user or call again with `clue` (a place, date, project). Always relay OUTDATED notices.

    Args:
        query: What to look for, in any language.
        clue: Distinguishing detail, only after an AMBIGUOUS reply.
        detail: "compact" (default) or "full" (adds ids, tree paths and excerpts).
    """
    try:
        return _search(
            get_memory(),
            query,
            clue,
            detail,
            "Nothing has been stored about this yet — consider save_memory once the user tells you "
            "something worth keeping.",
        )
    except IHMTError as exc:
        return f"Memory unavailable: {exc}"


@server.tool()
def save_memory(content: str, domain: str = "general", content_type: str = "auto") -> str:
    """Store durable knowledge in the user's long-term memory (IHMT).

    Save decisions and why, setup, preferences, people and projects, procedures. Not transient
    chat, not secrets. Write it to make sense months later, and start with the date
    ("2026-02-03: ...") when it belongs to one. Reports how it was filed and any contradiction
    with an earlier memory — relay that notice.

    Args:
        content: The text to remember, self-contained.
        domain: Leave "general" to let IHMT classify it, or e.g. "software.java".
        content_type: "auto" (recommended), "code", "narrative", "clinical", "personal",
            "process" or "generic".
    """
    if not content or not content.strip():
        return "Nothing to save: content is empty."

    requested_type: Optional[DataType] = None
    normalized_type = (content_type or "auto").strip().lower()
    if normalized_type not in ("", "auto"):
        try:
            requested_type = DataType(normalized_type.upper())
        except ValueError:
            valid = ", ".join(["auto"] + [t.value.lower() for t in DataType])
            return f'Unknown content_type "{content_type}". Use one of: {valid}.'

    # "general" is the documented default and means "let IHMT decide"; the
    # detector falls back to the general domain anyway when it cannot classify.
    requested_domain = None if (domain or "").strip().lower() in ("", "general", "auto") else domain.strip()

    try:
        memory = get_memory()
        before = {(c.subject, c.attribute, c.new.value) for c in memory.conflicts()}
        report = memory.ingest_text(
            content,
            source="mcp://claude-code",
            domain=requested_domain,
            data_type=requested_type,
            tags=["source:claude-code"],
        )
        after = memory.conflicts()
    except IHMTError as exc:
        return f"Could not save to memory: {exc}"

    if not report.chunks:
        return "Nothing to save: the content held no storable text."

    stats = memory.stats()
    lines = [
        f"Saved {report.chunks} memory entr{'y' if report.chunks == 1 else 'ies'} "
        f"(~{report.tokens} tokens) as {report.data_type.value} in domain '{report.domain}'"
        + (f" (detection confidence {report.detection.confidence:.2f})." if requested_type is None else "."),
        f"Memory now holds {stats['leaves']} entries across {len(stats['domains'])} domains "
        f"({stats['nodes']} summary nodes, depth {stats['depth']}).",
    ]
    if report.facts:
        lines.append(f"Extracted {report.facts} dated fact(s) into the timeline.")

    new_conflicts = [c for c in after if (c.subject, c.attribute, c.new.value) not in before]
    if new_conflicts:
        lines.append("")
        lines.append("This updates something remembered earlier — tell the user:")
        for conflict in new_conflicts:
            lines.append(f"  {conflict.render_notice()}")
            lines.append(
                f"  (the earlier value is kept as history, retrievable for questions about that period)"
            )
    return "\n".join(lines)


# ------------------------------------------------------------ project tools
@server.tool()
def project_map(path: str, detail: str = "files", subpath: str = "") -> str:
    """Compact map of a codebase: files with their size in tokens, optionally their symbols and line ranges.

    Call it before exploring a project instead of listing directories and opening files.
    Indexes the project on first use and resyncs changed files by checksum on every call.

    Args:
        path: Project root directory (absolute).
        detail: "files" (cheapest) or "symbols" (adds methods/classes with first-last line).
        subpath: Restrict to a directory inside the project, e.g. "src/main".
    """
    if detail not in ("files", "symbols"):
        return 'detail must be "files" or "symbols".'
    try:
        return get_project(path).map(detail=detail, subpath=subpath)
    except IHMTError as exc:
        return f"Cannot index {path}: {exc}"


@server.tool()
def find_code(
    query: str,
    path: str,
    scope: str = "main",
    subpath: str = "",
    clue: Optional[str] = None,
    max_tokens: int = 1500,
) -> str:
    """Find the method or class that answers a question about a codebase; returns only that symbol.

    Output: "file:first-last  Symbol" and its code, instead of whole files. Name the class or
    method if you know it ("PedidoJpaMapper aDominio"). AMBIGUOUS lists candidates in different
    files: refine the query or pass `clue`. The index resyncs by checksum, so code is never stale.

    Args:
        query: What you are looking for: identifiers plus a few words.
        path: Project root directory (absolute).
        scope: "main" (skip tests, default), "test" or "all".
        subpath: Restrict to a directory inside the project.
        clue: Extra detail after an AMBIGUOUS reply.
        max_tokens: Cap on the returned code.
    """
    if scope not in SCOPES:
        return f"scope must be one of {', '.join(SCOPES)}."
    if not query or not query.strip():
        return "Provide a non-empty query."
    try:
        result = get_project(path).find(query, scope=scope, subpath=subpath, clue=clue)
    except IHMTError as exc:
        return f"Cannot index {path}: {exc}"
    if result.status == "none":
        return f'No code matches "{query}" (scope={scope}). Try other identifiers or scope="all".'
    if result.status == "ambiguous":
        return (
            f'AMBIGUOUS: "{query}" matches symbols in several files. Refine the query or pass `clue`:\n'
            + "\n".join(f"  - {candidate}" for candidate in result.candidates)
        )
    return result.hits[0].render(max_chars=max(200, max_tokens * 4))


@server.tool()
def read_file(path: str, force: bool = False, start_line: int = 0, end_line: int = 0) -> str:
    """Read a text file, remembering what you were given this session.

    Reading it again returns "UNCHANGED" (a few tokens) or only a unified diff against your earlier
    copy. Prefer it over re-reading a file you already read in this session. Pass force=true if
    the earlier copy is no longer in your context (e.g. after a context compaction). With
    start_line/end_line (e.g. a range from project_map) only those lines are returned.

    Args:
        path: Absolute path of the file.
        force: Return the full content even if it has not changed.
        start_line: First line to return (1-based); 0 for the whole file.
        end_line: Last line to return (inclusive); 0 for the end of the file.
    """
    file_path = Path(path).expanduser().resolve()
    if start_line or end_line:
        if not file_path.is_file():
            return f"No such file: {file_path}"
        try:
            all_lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return f"{file_path} is not UTF-8 text."
        first = max(1, start_line or 1)
        last = min(len(all_lines), end_line or len(all_lines))
        if first > last:
            return f"Empty range {first}-{last}: the file has {len(all_lines)} lines."
        body = "\n".join(all_lines[first - 1:last])
        return f"{file_path.name}:{first}-{last}\n{body}"
    if not file_path.is_file():
        return f"No such file: {file_path}"
    data = file_path.read_bytes()
    if len(data) > MAX_READ_BYTES:
        return f"{file_path} is {len(data) // 1024} KB; use find_code to get the part you need."
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"{file_path} is not UTF-8 text."
    digest = hashlib.sha256(data).hexdigest()
    key = str(file_path)
    previous = _delivered.get(key)
    _delivered[key] = (digest, text)
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)

    if previous is not None and not force:
        if previous[0] == digest:
            return f"UNCHANGED since your last read ({lines} lines). Use your copy; force=true if it left your context."
        diff = "\n".join(
            difflib.unified_diff(
                previous[1].splitlines(), text.splitlines(), fromfile="your copy", tofile="now", n=2, lineterm=""
            )
        )
        if len(diff) < 0.6 * len(text):
            return f"CHANGED since your last read; diff against your copy:\n{diff}"
    return f"{file_path} ({lines} lines, ~{estimate_tokens(text)} tokens)\n{text}"


# ------------------------------------------------------------ session tools
@server.tool()
def note(text: str) -> str:
    """Keep something you learned in this session (where a thing lives, what a method does, a result).

    Session notes are cheap to recall later and survive a context compaction; they vanish when the
    session ends. Include file:line when relevant. Durable facts belong in save_memory instead.
    """
    global _session_counter
    if not text or not text.strip():
        return "Nothing to note."
    _session_counter += 1
    memory = get_session_memory()
    report = memory.ingest_text(text, source=f"session://note/{_session_counter}", data_type=DataType.GENERIC)
    return f"Noted (~{report.tokens} tokens). {memory.stats()['leaves']} session notes so far; use recall to find them."


@server.tool()
def recall(query: str, clue: Optional[str] = None) -> str:
    """Search what you kept earlier in this session with note or digest_output (not long-term memory).

    Cheaper than re-reading files or re-running commands to remember what you already found out.
    """
    return _search(
        get_session_memory(),
        query,
        clue,
        "compact",
        "Nothing like that was noted in this session.",
    )


_DIGEST_RE = re.compile(
    r"error|fail|exception|traceback|caused by|warn|panic|fatal|build (success|failure)|tests? run|"
    r"passed|skipped|assert|expected|✗|✘|FAILED|ERROR",
    re.IGNORECASE,
)


@server.tool()
def digest_output(text: str, label: str = "output") -> str:
    """Condense a long command output (build log, test report) and keep the full text for recall.

    Returns the first lines, every line that mentions errors, failures, warnings or test totals,
    and the last lines. The whole output stays queryable with recall("<label> ...") this session.

    Args:
        text: The raw output.
        label: A short name to recall it by, e.g. "maven-build".
    """
    global _session_counter
    if not text or not text.strip():
        return "Nothing to digest."
    _session_counter += 1
    get_session_memory().ingest_text(
        f"{label}\n{text}", source=f"session://output/{label}/{_session_counter}", data_type=DataType.GENERIC
    )
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    keep: List[str] = []
    seen = set()

    def add(line: str) -> None:
        if line not in seen:
            seen.add(line)
            keep.append(line)

    for line in lines[:3]:
        add(line)
    for line in lines[3:-5]:
        if _DIGEST_RE.search(line):
            add(line)
    for line in lines[-5:]:
        add(line)
    digest = "\n".join(keep)
    if len(digest) > DIGEST_CHAR_LIMIT:
        digest = digest[:DIGEST_CHAR_LIMIT] + "\n[... more matching lines: recall them with a specific query]"
    return (
        f"{label}: {len(lines)} lines (~{estimate_tokens(text)} tokens) condensed to ~{estimate_tokens(digest)}.\n"
        f"{digest}"
    )


# ----------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    """Start the server on stdio."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        sys.stderr.write(__doc__ or "")
        return 0
    logger.info("serving IHMT memory from %s", IHMT_HOME)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
