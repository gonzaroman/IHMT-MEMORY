#!/usr/bin/env python3
"""MCP server exposing the IHMT memory tree to an LLM client.

Three families of tools are published over stdio:

* **Long-term memory** (across sessions):
  ``search_memory(query, clue, detail)`` walks the tree via
  :class:`SemanticNavigator` and never guesses on an ambiguous query;
  ``save_memory(content, domain, content_type)`` stores new knowledge through
  :class:`UniversalIngestor`. With ``scope="conversations"`` the search walks a
  second tree instead: the past Claude Code sessions that ``main.py capture``
  (run from session hooks) stores under ``$IHMT_HOME/ihmt_conversations``.
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
import functools
import hashlib
import logging
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

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
from ihmt.textutils import content_terms, estimate_tokens, slugify  # noqa: E402
from ihmt.transcripts import CONVERSATIONS_DIRNAME, SOURCE_PREFIX  # noqa: E402

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
#: A conversation fragment is context, not a curated note: show less of it.
CONVERSATION_TEXT_LIMIT = 3000
#: Budget of a project map; beyond it the costliest detail is dropped first.
MAP_TOKEN_BUDGET = 3000
#: Alternative phrasings accepted by one search call.
MAX_FORMULATIONS = 4
DIGEST_CHAR_LIMIT = 2400

SERVER_INSTRUCTIONS = """\
IHMT: the user's long-term memory, plus tools that save tokens in a session.
`search_memory` before answering anything that depends on earlier sessions
(scope="conversations" searches the past sessions themselves); `save_memory`
for durable facts, with `replaces` when it corrects one. Never guess on
AMBIGUOUS, say so on NOT FOUND, always relay OUTDATED.
In a codebase prefer `project_map`, `find_code` and `read_file` (answers
"unchanged" or a diff) to listing and re-reading files.
"""

server = _Server(
    name="ihmt-memory",
    instructions=SERVER_INSTRUCTIONS,
    version="1.3.0",
)

_memory: Optional[IHMT] = None
_projects: Dict[str, ProjectIndex] = {}
_delivered: Dict[str, Tuple[str, str]] = {}  # resolved path -> (sha256, text) handed out this session
_conversations: Optional[IHMT] = None
_session: Optional[IHMT] = None
_session_dir: Optional[tempfile.TemporaryDirectory] = None
_session_counter = 0

#: The SDK runs each tool call in a worker thread, and agents often call several
#: tools at once. The stores and caches above are not thread-safe, so tool calls
#: take turns.
_tool_lock = threading.RLock()
_Tool = TypeVar("_Tool", bound=Callable[..., str])


def _one_at_a_time(tool: _Tool) -> _Tool:
    """Run ``tool`` under the server-wide lock (signature and docstring kept for the SDK)."""

    @functools.wraps(tool)
    def locked(*args, **kwargs):  # type: ignore[no-untyped-def]
        with _tool_lock:
            return tool(*args, **kwargs)

    return locked  # type: ignore[return-value]


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


def get_conversations() -> Optional[IHMT]:
    """The tree of captured sessions, or ``None`` while nothing has been captured.

    Written by ``main.py capture`` from another process, so it is reloaded on
    every call like the long-term store.
    """
    global _conversations
    base = IHMT_HOME / CONVERSATIONS_DIRNAME
    if _conversations is None:
        if not (base / "ihmt_memory" / "root.json").exists():
            return None
        _conversations = IHMT(base)
    else:
        _conversations.store.reload()
    return _conversations


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
        if result.supersedes:
            lines.append(f"    current version of: {', '.join(result.supersedes)}")
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


def _format_compact(response: SearchResponse, limit: int = FULL_TEXT_LIMIT) -> str:
    """Render a search with no bookkeeping: the best memory, and one line per rival.

    Dates and OUTDATED notices are kept — they change the meaning of an answer.
    Internal ids, file paths and tree paths are dropped: the model never needs them.
    """
    best = response.results[0]
    body = (best.content or best.excerpt).rstrip()
    if len(body) > limit:
        body = body[:limit] + "\n[... truncated; narrow the query for the rest]"
    lines = [f"[{best.domain}] {best.timestamp[:10] or 'undated'} (confidence {response.confidence:.2f})"]
    lines += [f"OUTDATED: {notice}" for notice in best.notices]
    lines.append(body)
    earlier = [r for r in response.results if r.leaf_id in best.supersedes]
    if earlier:
        lines.append("")
        lines.append("This is the current version. It replaces an earlier memory — mention the change:")
        for result in earlier:
            # The excerpt, not just the title: the rest of an old memory may still hold.
            date = result.timestamp[:10] or "undated"
            excerpt = result.excerpt[:EXCERPT_LIMIT]
            lines.append(f"  [{result.domain}] " + (excerpt if excerpt.startswith(date) else f"{date}: {excerpt}"))
            lines += [f"  OUTDATED: {notice}" for notice in result.notices]
    others = [r for r in response.results[1:MAX_RESULTS] if r.leaf_id not in best.supersedes]
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


def _format_weak(response: SearchResponse, empty_hint: str) -> str:
    """Render a search whose best leaf only echoes a word or two of the query."""
    lines = [
        f'NOT FOUND: nothing in memory clearly matches "{response.query}" '
        f"(the closest entry holds {response.coverage:.0%} of the query's words). "
        "Tell the user nothing is stored about it rather than guessing. " + empty_hint,
        "",
        "Closest entries, probably unrelated:",
    ]
    for result in response.results[:3]:
        lines.append(f"  [{result.domain}] {result.timestamp[:10] or 'undated'} · {result.title}")
    return "\n".join(lines)


def _outcome(response: SearchResponse) -> int:
    """How useful a response is: 3 an answer, 2 a clue request, 1 a weak echo, 0 nothing."""
    if not response.results:
        return 0
    if response.weak:
        return 1
    return 2 if response.needs_clue else 3


def _search(
    memory: IHMT,
    query: str,
    clue: Optional[str],
    detail: str,
    empty_hint: str,
    *,
    also: Optional[List[str]] = None,
    since: str = "",
    path_prefix: Optional[str] = None,
    limit: int = FULL_TEXT_LIMIT,
) -> str:
    """Shared body of ``search_memory`` and ``recall``.

    ``also`` holds other phrasings of the same question. Matching is lexical, so
    a Spanish note is invisible to an English query: every phrasing is searched
    and the most useful response wins — an answer beats a request for a clue,
    which beats a weak echo. AMBIGUOUS or NOT FOUND is only reported when no
    phrasing did better.
    """
    if not query or not query.strip():
        return "Provide a non-empty query."
    if detail not in ("compact", "full"):
        return 'detail must be "compact" or "full".'
    since = (since or "").strip()
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        return 'since must be a date like "2026-10-01".'
    phrasings = [query] + [p for p in (also or []) if isinstance(p, str) and p.strip()]
    responses = [
        memory.search(p, top_k=MAX_RESULTS, clue=clue, since=since or None, path_prefix=path_prefix)
        for p in dict.fromkeys(phrasings[:MAX_FORMULATIONS])
    ]
    response = max(responses, key=lambda r: (_outcome(r), r.confidence))
    if not response.results:
        filters = (f' (with clue "{clue}")' if clue else "") + (f" since {since}" if since else "")
        return f'No memory found for "{query}"{filters}. {empty_hint}'
    if response.weak:
        return _format_weak(response, empty_hint)
    if response.needs_clue:
        return _format_clue_request(response)
    return _format_compact(response, limit) if detail == "compact" else _format_results(response)


# ------------------------------------------------------ long-term memory tools
@server.tool()
@_one_at_a_time
def search_memory(
    query: str,
    clue: Optional[str] = None,
    detail: str = "compact",
    also: Optional[List[str]] = None,
    since: str = "",
    scope: str = "memory",
    project: str = "",
) -> str:
    """Search the user's long-term memory (IHMT): decisions, setup, preferences, people, earlier sessions.

    Use several distinctive words, not a bare name. Returns the best memory with its date
    (plus one line per other match). AMBIGUOUS means several memories fit equally: ask the
    user or call again with `clue` (a place, date, project). Always relay OUTDATED notices.

    Args:
        query: What to look for, in any language.
        clue: Distinguishing detail, only after an AMBIGUOUS reply.
        detail: "compact" (default) or "full" (adds ids, tree paths and excerpts).
        also: Other phrasings of the same question (another language, synonyms), searched in
            this same call; matching is by words, so this replaces retrying.
        since: Only memories dated on or after "YYYY-MM-DD" — for the current state of a topic.
        scope: "memory" (curated notes, default) or "conversations" (what was said and done
            in past sessions; use it when the notes have nothing).
        project: With scope="conversations", only sessions of this project (its folder name).
    """
    if scope not in ("memory", "conversations"):
        return 'scope must be "memory" or "conversations".'
    try:
        if scope == "conversations":
            conversations = get_conversations()
            if conversations is None:
                return "No conversations have been captured yet (see `main.py capture`)."
            name = slugify(project, fallback="") if project.strip() else ""
            return _search(
                conversations,
                query,
                clue,
                detail,
                "Nothing like that in the captured sessions"
                + (f' of project "{name}"; try without `project`.' if name else "."),
                also=also,
                since=since,
                path_prefix=f"{SOURCE_PREFIX}{name}/" if name else None,
                limit=CONVERSATION_TEXT_LIMIT,
            )
        if project.strip():
            return 'project only applies to scope="conversations"; put the project name in the query instead.'
        return _search(
            get_memory(),
            query,
            clue,
            detail,
            "Nothing has been stored about this yet — consider save_memory once the user tells you "
            "something worth keeping.",
            also=also,
            since=since,
        )
    except IHMTError as exc:
        return f"Memory unavailable: {exc}"


@server.tool()
@_one_at_a_time
def save_memory(content: str, domain: str = "general", content_type: str = "auto", replaces: str = "") -> str:
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
        replaces: When this corrects or updates something saved earlier, a few distinctive
            words describing that earlier memory ("RainDesktop version 1.3"). It is then
            flagged OUTDATED and searches answer with this one first. If several memories
            fit, nothing is flagged and the candidates are listed for `mark_outdated`.
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

    lines = [
        f"Saved {report.chunks} memory entr{'y' if report.chunks == 1 else 'ies'} "
        f"as {report.data_type.value} in domain '{report.domain}' (id {', '.join(report.leaf_ids)}).",
    ]
    if report.facts:
        lines.append(f"Extracted {report.facts} dated fact(s) into the timeline.")

    lines += _apply_correction(memory, content, report.leaf_ids, replaces)

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


_CORRECTION_PREFIX = re.compile(
    r"^\s*(?:\d{4}-\d{1,2}-\d{1,2}[^:\n]{0,40}:\s*)?(?:CORRECTION|CORRECCI[OÓ]N)\b\s*[:—–-]?\s*",
    re.IGNORECASE,
)

#: Share of the ``replaces`` words an earlier memory's text must contain to be
#: flagged as the one corrected.
REPLACES_COVERAGE = 0.5
#: A memory starting "CORRECTION:" is linked to its predecessor on its own only
#: when the predecessor holds this much of its words — otherwise it would be a
#: guess, and the agent is asked to pass ``replaces`` or call ``mark_outdated``.
AUTO_CORRECTION_COVERAGE = 0.6
#: Candidates fetched when looking for the memory a save corrects.
CORRECTION_CANDIDATES = 8


def _apply_correction(memory: IHMT, content: str, new_ids: List[str], replaces: str) -> List[str]:
    """Flag the memory a new save corrects, when one is named or clearly implied.

    Returns the lines to append to the save report.
    """
    if not new_ids:
        return []
    marked = _CORRECTION_PREFIX.match(content)
    query = (replaces or "").strip()
    automatic = not query and marked is not None
    if automatic:
        query = content[marked.end():].strip().splitlines()[0][:300]
    if not query:
        return []

    response = memory.search(query, top_k=CORRECTION_CANDIDATES + len(new_ids))
    terms = set(content_terms(query))

    def held(result) -> float:  # type: ignore[no-untyped-def]
        """Share of the query's words present in the memory's own text."""
        if not terms:
            return 0.0
        return len(terms & set(content_terms(result.content or result.excerpt))) / len(terms)

    # Flagging the wrong memory is worse than flagging none, so the choice is
    # made on what each candidate's text actually says — search scores mix
    # tree levels and are fine for ranking answers, not for this.
    candidates = sorted(
        (r for r in response.results if r.leaf_id not in new_ids),
        key=lambda r: (-held(r), -r.score, r.leaf_id),
    )
    new_id = new_ids[0]
    needed = AUTO_CORRECTION_COVERAGE if automatic else REPLACES_COVERAGE
    if not candidates or held(candidates[0]) < needed:
        if automatic:
            return []
        return ["", f'No earlier memory clearly matches "{query}", so nothing was flagged as outdated.']

    best = candidates[0]
    margin = memory.config.ambiguity_margin
    tied = [c for c in candidates[1:] if held(c) >= held(best) and c.score >= best.score * (1 - margin)]
    clear = not tied
    if clear:
        try:
            notice = memory.supersede(best.leaf_id, new_id)
        except (IHMTError, ValueError) as exc:
            return ["", f"Could not flag the earlier memory: {exc}"]
        return [
            "",
            "This corrects an earlier memory, now flagged OUTDATED — tell the user:",
            f"  {notice}",
        ]

    lines = [
        "",
        (
            "This looks like a correction, but which memory it corrects is unclear."
            if automatic
            else f'Several earlier memories match "{query}"; none was flagged.'
        ),
        f"If one of these is what changed, call mark_outdated(old_id, new_id=\"{new_id}\"):",
    ]
    for result in candidates[:MAX_RESULTS]:
        lines.append(f"  {result.leaf_id} · {result.timestamp[:10] or 'undated'} · {result.title}")
    return lines


@server.tool()
@_one_at_a_time
def mark_outdated(old_id: str, new_id: str) -> str:
    """Flag one memory as corrected by another, after save_memory listed candidates.

    The old memory is kept (it was true on its date) but carries an OUTDATED notice, and
    searches answer with the new one first. Use the ids save_memory or a full search printed.

    Args:
        old_id: Id of the memory that is no longer current.
        new_id: Id of the memory that replaces it.
    """
    try:
        notice = get_memory().supersede(old_id.strip(), new_id.strip())
    except (IHMTError, ValueError) as exc:
        return f"Could not flag {old_id}: {exc}"
    return f"Flagged as outdated — tell the user:\n  {notice}"


# ------------------------------------------------------------ project tools
@server.tool()
@_one_at_a_time
def project_map(path: str, detail: str = "files", subpath: str = "", scope: str = "main") -> str:
    """Compact map of a codebase: files with their size in tokens, optionally their symbols and line ranges.

    Call it before exploring a project instead of listing directories and opening files.
    Indexes the project on first use and resyncs changed files by checksum on every call.
    The map fits a token budget: on a large project it says which directories to zoom into.

    Args:
        path: Project root directory (absolute).
        detail: "files" (cheapest) or "symbols" (adds methods/classes with first-last line).
        subpath: Restrict to a directory inside the project, e.g. "src/main".
        scope: "main" (leave tests out, default), "test" or "all".
    """
    if detail not in ("files", "symbols"):
        return 'detail must be "files" or "symbols".'
    if scope not in SCOPES:
        return f"scope must be one of {', '.join(SCOPES)}."
    try:
        return get_project(path).map(detail=detail, subpath=subpath, scope=scope, max_tokens=MAP_TOKEN_BUDGET)
    except IHMTError as exc:
        return f"Cannot index {path}: {exc}"


@server.tool()
@_one_at_a_time
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
@_one_at_a_time
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
@_one_at_a_time
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
@_one_at_a_time
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
@_one_at_a_time
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
