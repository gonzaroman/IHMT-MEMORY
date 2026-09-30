"""Project index: IHMT as a token-saving cache of a source tree.

Long-term memory answers "what did we decide last month?". A coding session
asks a different question, over and over: "where is X, and what does it say?".
Answering it by reading whole files fills the context with text the model does
not need — and the same files get read again after every change or compaction.

A :class:`ProjectIndex` keeps a private IHMT store per project:

* **one leaf per symbol** (``code_chunk_mode="symbol"``), so a lookup returns a
  single method with its ``file:line`` range instead of the whole file;
* **checksum sync** — every call rescans the tree cheaply (size + mtime), hashes
  only what moved, re-ingests only files whose SHA-256 changed, deletes the
  leaves of changed or removed files and rebuilds the branches. A lookup never
  serves stale code;
* **a map for free** — :meth:`map` lists files and their symbols from the sync
  manifest, without opening a single leaf.

The store is derived data: deleting it only costs a re-index. It never touches
the user's long-term memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .api import IHMT
from .models import IHMTError
from .semantic_navigator import SCOPES, looks_like_code
from .textutils import estimate_tokens, short_hash, slugify

#: Text files worth indexing. Anything else (binaries, lock files, media) is skipped.
INDEXABLE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".java", ".kt", ".kts", ".scala", ".groovy", ".js", ".mjs", ".cjs", ".jsx", ".ts",
        ".tsx", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".go", ".rs", ".swift", ".php",
        ".rb", ".sql", ".md", ".txt", ".rst", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg",
        ".properties", ".gradle", ".json", ".html", ".css", ".scss", ".sh", ".bash", ".zsh",
    }
)
#: Directories never worth indexing, at any depth: VCS, dependencies, build output, IDE state.
SKIP_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
        "target", ".idea", ".vscode", ".gradle", ".mvn", ".next", ".nuxt", "coverage",
        ".pytest_cache", ".mypy_cache", ".tox", "ihmt_memory", "ihmt_projects",
    }
)
#: Build-output names that are also legitimate package names deeper down —
#: hexagonal code lives in ``adapter/out`` — so they are skipped only at the root.
SKIP_AT_ROOT = frozenset({"out", "bin", "obj", "env"})
#: Leaves are grouped into branches in timestamp order. Stamping each file with
#: its rank in path order makes every branch cover neighbouring files and
#: symbols — a package, a class — so branch summaries stay meaningful and the
#: descent can tell them apart. (Ingest-time stamps would mix files at random.)
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _path_order_timestamp(rank: int) -> str:
    return (_EPOCH + timedelta(seconds=rank)).isoformat().replace("+00:00", "Z")


MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 5000
MANIFEST = "sources.json"

#: Store shape tuned for code lookups: small leaves, one per symbol.
PROJECT_CONFIG: Dict[str, Any] = {
    "branch_factor": 8,
    "beam_width": 6,  # a wider beam costs local file reads, not model tokens
    "target_tokens": 150,
    "max_tokens": 3000,
    "min_tokens": 1,
    "code_chunk_mode": "symbol",
}


@dataclass
class SyncReport:
    """What a :meth:`ProjectIndex.sync` found and did."""

    files: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    skipped: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0

    @property
    def touched(self) -> int:
        return self.added + self.changed + self.removed

    def summary(self) -> str:
        parts = [f"{self.files} files indexed"]
        if self.touched:
            parts.append(f"{self.added} new, {self.changed} changed, {self.removed} removed")
        else:
            parts.append("no changes")
        if self.truncated:
            parts.append(f"stopped at {MAX_FILES} files")
        return ", ".join(parts) + f" ({self.elapsed_ms:.0f} ms)"


@dataclass
class CodeHit:
    """One symbol returned by :meth:`ProjectIndex.find`."""

    source: str
    title: str
    start_line: int
    end_line: int
    content: str
    context: str = ""

    def render(self, max_chars: Optional[int] = None) -> str:
        body = self.content
        if max_chars is not None and len(body) > max_chars:
            body = body[:max_chars] + "\n[... truncated]"
        header = f"{self.source}:{self.start_line}-{self.end_line}  {self.title}"
        if self.context and self.context.strip() not in body:
            header += f"\n(in: {self.context.strip().splitlines()[0]})"
        return f"{header}\n{body.rstrip()}"


@dataclass
class FindResult:
    """Outcome of a lookup: hits, or the candidates when it is ambiguous."""

    status: str  # "found" | "ambiguous" | "none"
    hits: List[CodeHit] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    confidence: float = 0.0


class ProjectIndex:
    """A per-project IHMT store kept in sync with the files on disk."""

    def __init__(self, project_root: Path | str, store_dir: Path | str) -> None:
        self.root = Path(project_root).expanduser().resolve()
        if not self.root.is_dir():
            raise IHMTError(f"not a directory: {self.root}")
        self.store_dir = Path(store_dir).expanduser()
        if not (self.store_dir / "ihmt_memory" / "root.json").exists():
            IHMT.initialize(self.store_dir, **PROJECT_CONFIG)
        self.memory = IHMT(self.store_dir, auto_consolidate=False)

    @classmethod
    def for_project(cls, project_root: Path | str, cache_home: Path | str) -> "ProjectIndex":
        """Open (or create) the index of ``project_root`` under ``cache_home``."""
        root = Path(project_root).expanduser().resolve()
        name = f"{slugify(root.name, max_length=40) or 'project'}-{short_hash(str(root), 8)}"
        return cls(root, Path(cache_home).expanduser() / name)

    # ----------------------------------------------------------------- manifest
    @property
    def _manifest_path(self) -> Path:
        return self.memory.config.state_dir / MANIFEST

    def _load_manifest(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8")).get("files", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_manifest(self, files: Dict[str, Dict[str, Any]]) -> None:
        payload = {"project": str(self.root), "files": dict(sorted(files.items()))}
        self.memory.store.write_json(self._manifest_path, payload)

    # --------------------------------------------------------------------- scan
    def scan(self) -> Tuple[Dict[str, Path], int, bool]:
        """Indexable files under the root: ``({relative path: path}, skipped, truncated)``."""
        found: Dict[str, Path] = {}
        skipped = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            directories = relative.parts[:-1]
            if any(part in SKIP_DIRECTORIES or part.startswith(".") for part in directories):
                continue
            if directories and directories[0] in SKIP_AT_ROOT:
                continue
            if path.suffix.lower() not in INDEXABLE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                continue
            if len(found) >= MAX_FILES:
                return found, skipped, True
            found[relative.as_posix()] = path
        return found, skipped, False

    # --------------------------------------------------------------------- sync
    def sync(self) -> SyncReport:
        """Bring the index in line with the files on disk. Cheap when nothing changed."""
        started = time.perf_counter()
        report = SyncReport()
        known = self._load_manifest()
        files, report.skipped, report.truncated = self.scan()
        report.files = len(files)

        to_ingest: Dict[str, str] = {}
        manifest: Dict[str, Dict[str, Any]] = {}
        for relative, path in files.items():
            stat = path.stat()
            previous = known.get(relative)
            if previous and previous.get("size") == stat.st_size and previous.get("mtime_ns") == stat.st_mtime_ns:
                manifest[relative] = previous  # untouched: not even hashed
                continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if previous and previous.get("sha256") == digest:
                manifest[relative] = {**previous, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                report.skipped += 1
                continue
            to_ingest[relative] = text
            manifest[relative] = {"sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            if previous:
                report.changed += 1
            else:
                report.added += 1

        removed = [relative for relative in known if relative not in files]
        report.removed = len(removed)

        if report.touched:
            store = self.memory.store
            with store.batch():
                for relative in removed + [r for r in to_ingest if r in known]:
                    for leaf in known[relative].get("leaves", []):
                        store.delete_leaf(leaf["id"])
                rank = {relative: index for index, relative in enumerate(sorted(files))}
                for relative, text in to_ingest.items():
                    ingest = self.memory.ingest_text(
                        text, source=relative, timestamp=_path_order_timestamp(rank[relative])
                    )
                    leaves = []
                    for leaf_id in ingest.leaf_ids:
                        leaf = self.memory.get_leaf(leaf_id)
                        title = leaf.title.split(" · ", 1)[-1]
                        leaves.append(
                            {"id": leaf_id, "title": title, "start": leaf.span.start_line, "end": leaf.span.end_line}
                        )
                    manifest[relative].update(tokens=estimate_tokens(text), leaves=leaves)
            # Branches summarize their children, so rebuild them from the surviving leaves.
            IHMT.initialize(self.store_dir, force=True)
            self.memory = IHMT(self.store_dir, auto_consolidate=False)
            self.memory.flush()

        if report.touched or set(manifest) != set(known):
            self._save_manifest(manifest)
        report.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return report

    # --------------------------------------------------------------------- read
    def map(self, *, detail: str = "symbols", subpath: str = "", max_lines: int = 400) -> str:
        """A compact map of the project, built from the manifest (no leaf is opened).

        Args:
            detail: ``"files"`` (one line per file) or ``"symbols"`` (plus the
                symbols of each file with their line ranges).
            subpath: Restrict the map to a directory, relative to the root.
            max_lines: Cap on the number of lines returned.
        """
        if detail not in ("files", "symbols"):
            raise ValueError('detail must be "files" or "symbols"')
        manifest = self._load_manifest()
        prefix = subpath.strip("/")
        lines: List[str] = []
        total_tokens = 0
        current_dir = None
        for relative, info in sorted(manifest.items()):
            if prefix and not relative.startswith(prefix):
                continue
            total_tokens += int(info.get("tokens", 0))
            directory, _, name = relative.rpartition("/")
            if directory != current_dir:
                lines.append(f"{directory or '.'}/")
                current_dir = directory
            line = f"  {name} ~{info.get('tokens', 0)}"
            if detail == "symbols" and looks_like_code(relative):
                # Only source code has meaningful symbols; for HTML, Markdown or
                # XML the "blocks" are markup fragments that cost tokens and
                # orient no one.
                stem = name.rsplit(".", 1)[0]
                symbols = [
                    f"{self._short_symbol(leaf['title'], stem)} {leaf['start']}-{leaf['end']}"
                    for leaf in info.get("leaves", [])
                    if self._worth_listing(leaf.get("title", ""))
                ]
                if symbols:
                    line += ": " + ", ".join(symbols)
            lines.append(line)
            if len(lines) >= max_lines:
                lines.append(f"[... map truncated at {max_lines} lines; pass subpath to zoom in]")
                break
        header = (
            f"{self.root.name}: {len(manifest)} files, ~{total_tokens} tokens if read whole "
            "(format: file ~tokens: symbol first-last line)"
        )
        return "\n".join([header] + lines)

    @staticmethod
    def _worth_listing(title: str) -> bool:
        """Imports, module preambles and class headers add length, not orientation."""
        if not title:
            return False
        lowered = title.lower()
        return not (lowered.startswith("imports") or lowered.startswith("module level") or "(header)" in lowered)

    @staticmethod
    def _short_symbol(title: str, stem: str) -> str:
        """``Pedido.confirmar`` in Pedido.java is just ``confirmar``; runs become ``a…b``."""
        parts = [p.strip() for p in title.split("→")]
        short = []
        for part in parts:
            for prefix in (f"{stem}.", f"class {stem}", f"record {stem}", f"interface {stem}", f"enum {stem}"):
                if part.startswith(prefix) and part != prefix.strip("."):
                    part = part[len(prefix):].lstrip(". ") or part
                    break
            short.append(part)
        return "…".join(short) if len(short) > 1 else short[0]

    def find(
        self,
        query: str,
        *,
        scope: str = "main",
        subpath: str = "",
        top_k: int = 1,
        clue: Optional[str] = None,
    ) -> FindResult:
        """Return the symbol(s) that best answer ``query``."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        exact = self._find_exact_symbol(query, scope=scope, subpath=subpath.strip("/"))
        if exact is not None:
            return FindResult(status="found", hits=[exact], confidence=1.0)
        response = self.memory.search(
            query, top_k=max(3, top_k), clue=clue, scope=scope, path_prefix=subpath.strip("/") or None
        )
        if not response.results:
            return FindResult(status="none")
        sources = {self._source_of(r.leaf_id) for r in response.results[:3]}
        if response.needs_clue and len(sources) > 1:
            candidates = []
            for result in response.results[:4]:
                candidates.append(f"{self._source_of(result.leaf_id)} · {result.title.split(' · ', 1)[-1]}")
            return FindResult(status="ambiguous", candidates=candidates, confidence=response.confidence)
        hits = []
        for result in response.results[:top_k]:
            leaf = self.memory.get_leaf(result.leaf_id)
            hits.append(
                CodeHit(
                    source=leaf.source,
                    title=leaf.title.split(" · ", 1)[-1],
                    start_line=leaf.span.start_line,
                    end_line=leaf.span.end_line,
                    content=leaf.content,
                    context=str(leaf.extra.get("context", "")),
                )
            )
        return FindResult(status="found", hits=hits, confidence=response.confidence)

    #: Paths that hold legacy or compatibility copies of an API; a symbol found
    #: there only wins when nothing else defines it.
    _LEGACY_PARTS = ("v1/", "deprecated/", "compat/", "legacy/")

    def _find_exact_symbol(self, query: str, *, scope: str, subpath: str) -> Optional[CodeHit]:
        """Resolve a query that names a symbol (``BaseModel.model_dump``, ``field_validator``).

        Looked up in the sync manifest — an in-memory scan, no tree descent — so
        a lookup by name is never lost to lexical ranking. Among several
        definitions (``@overload`` stubs, legacy copies, tests) the largest leaf
        of the most relevant file wins: that is where the implementation lives.
        """
        from .semantic_navigator import looks_like_test

        identifiers = [i for i in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*[A-Za-z0-9_]", query) if len(i) >= 4]
        if not identifiers:
            return None
        wanted_full = {i for i in identifiers if "." in i}
        wanted_last = {i.rsplit(".", 1)[-1] for i in identifiers}
        candidates = []
        for relative, info in self._load_manifest().items():
            if subpath and not relative.startswith(subpath):
                continue
            if scope != "all" and looks_like_test(relative) != (scope == "test"):
                continue
            for leaf in info.get("leaves", []):
                for part in (p.strip() for p in leaf.get("title", "").split("→")):
                    name = re.sub(r"^(async\s+)?(def|function|class|method|record|interface|enum)\s+", "", part)
                    name = name.split(" (", 1)[0].strip()
                    if not name:
                        continue
                    full = name in wanted_full
                    last = name.rsplit(".", 1)[-1] in wanted_last
                    if full or last:
                        size = int(leaf.get("end", 0)) - int(leaf.get("start", 0))
                        legacy = any(marker in relative for marker in self._LEGACY_PARTS)
                        candidates.append(((full, not legacy, size), relative, leaf))
                        break
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, _, leaf = candidates[0]
        try:
            stored = self.memory.get_leaf(leaf["id"])
        except Exception:  # noqa: BLE001 - a stale manifest entry falls back to search
            return None
        return CodeHit(
            source=stored.source,
            title=stored.title.split(" · ", 1)[-1],
            start_line=stored.span.start_line,
            end_line=stored.span.end_line,
            content=stored.content,
            context=str(stored.extra.get("context", "")),
        )

    def _source_of(self, leaf_id: str) -> str:
        entry = self.memory.store.catalog.entries.get(leaf_id)
        return entry.source if entry else ""


__all__ = ["ProjectIndex", "SyncReport", "CodeHit", "FindResult", "PROJECT_CONFIG"]
