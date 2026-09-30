"""Ingestion pipeline: raw material in, layer-0 leaves out.

The ingestor is the only writer of layer 0. For every document it:

1. **detects** the material type and domain (:mod:`ihmt.detectors`),
2. **splits** it with the matching type-aware chunker, never cutting inside a
   logical block,
3. **enriches** each chunk with keywords, entities, tags and a timestamp,
4. **writes** a :class:`~ihmt.models.MemoryLeaf` carrying a strict JSON header,
5. optionally **extracts dated facts** for the timeline and **triggers**
   consolidation once enough leaves have accumulated.

Leaf identifiers are deterministic (domain + source + position + content hash),
so re-ingesting an unchanged document rewrites the same leaves instead of
duplicating them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .chunkers import Chunk, get_chunker
from .detectors import Detection, DomainDetector
from .models import DataType, IHMTError, LeafStatus, MemoryLeaf, NodeNotFoundError, Span
from .storage import MemoryStore
from .textutils import (
    dedupe,
    estimate_tokens,
    extract_entities,
    extract_keywords,
    sha256_hex,
    short_hash,
    slugify,
    truncate,
    utc_now_iso,
)

#: Files that are never worth ingesting when walking a directory.
SKIP_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", "ihmt_memory"}
)
SKIP_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar", ".so", ".dll", ".class", ".pyc", ".bin"}
)


@dataclass
class IngestReport:
    """Outcome of ingesting one document."""

    source: str
    detection: Detection
    leaf_ids: List[str] = field(default_factory=list)
    chunks: int = 0
    tokens: int = 0
    oversized: int = 0
    facts: int = 0
    superseded: int = 0
    elapsed_ms: float = 0.0

    @property
    def domain(self) -> str:
        return self.detection.domain

    @property
    def data_type(self) -> DataType:
        return self.detection.data_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "domain": self.domain,
            "data_type": self.data_type.value,
            "confidence": round(self.detection.confidence, 3),
            "chunks": self.chunks,
            "tokens": self.tokens,
            "oversized": self.oversized,
            "facts": self.facts,
            "leaf_ids": self.leaf_ids,
            "superseded": self.superseded,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


class UniversalIngestor:
    """Turns any document into typed, self-describing memory leaves."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        detector: Optional[DomainDetector] = None,
        summarizer: Optional[Any] = None,
        conflict_resolver: Optional[Any] = None,
        auto_consolidate: bool = True,
    ) -> None:
        """
        Args:
            store: Target store.
            detector: Type/domain classifier; a default one is created.
            summarizer: Object exposing ``consolidate()``. Lazily constructed
                when ``auto_consolidate`` is on.
            conflict_resolver: Object exposing ``extract_from_leaf(leaf)``.
                When provided, dated facts are harvested during ingestion.
            auto_consolidate: Fire a Summarization Event after each document
                once ``branch_factor`` leaves are pending.
        """
        self.store = store
        self.config = store.config
        self.detector = detector or DomainDetector()
        self.conflict_resolver = conflict_resolver
        self.auto_consolidate = auto_consolidate
        self._summarizer = summarizer

    @property
    def summarizer(self):  # type: ignore[no-untyped-def]
        """The consolidation engine, built on first use."""
        if self._summarizer is None:
            from .recursive_summarizer import RecursiveSummarizer

            self._summarizer = RecursiveSummarizer(self.store)
        return self._summarizer

    # -------------------------------------------------------------- ingestion
    def ingest_text(
        self,
        text: str,
        *,
        source: str = "<inline>",
        domain: Optional[str] = None,
        data_type: Optional[DataType] = None,
        tags: Optional[Sequence[str]] = None,
        timestamp: Optional[str] = None,
        replace_previous: bool = False,
    ) -> IngestReport:
        """Ingest an in-memory document.

        Args:
            text: UTF-8 content.
            source: Origin label recorded in every leaf.
            domain: Override the detected domain.
            data_type: Override the detected material type.
            tags: Extra tags applied to every produced leaf.
            timestamp: Override the timestamp of every produced leaf.
            replace_previous: ``source`` names one document that has just been
                re-read (a file): leaves of an earlier version that the new
                version no longer produces are demoted to ``HISTORICAL`` — kept,
                but no longer served as current. Leave it off for sources shared
                by many independent documents, such as ``mcp://claude-code``.

        Returns:
            An :class:`IngestReport`.

        Raises:
            IHMTError: If the store has not been initialized.
        """
        started = time.perf_counter()
        if not self.store.exists():
            raise IHMTError("store is not initialized; run 'python init_ihmt.py' first")

        detection = self.detector.detect(text, source=source, data_type=data_type, domain=domain)
        report = IngestReport(source=source, detection=detection)
        if not text.strip():
            return report

        chunker = get_chunker(detection.data_type, self.config, source=source)
        chunks = chunker.chunk(text, source=source)

        with self.store.batch():
            for index, chunk in enumerate(chunks):
                leaf = self._build_leaf(
                    chunk,
                    index=index,
                    detection=detection,
                    source=source,
                    extra_tags=tags or (),
                    forced_timestamp=timestamp,
                )
                self._save_preserving_parent(leaf, reactivate=replace_previous)
                report.leaf_ids.append(leaf.leaf_id)
                report.tokens += leaf.token_estimate
                report.oversized += 1 if leaf.oversized else 0
                if self.conflict_resolver is not None:
                    report.facts += len(self.conflict_resolver.extract_from_leaf(leaf))
            if replace_previous:
                report.superseded = self._demote_previous_version(source, set(report.leaf_ids))

        report.chunks = len(report.leaf_ids)
        if self.auto_consolidate and report.chunks:
            self.summarizer.consolidate()
        report.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return report

    def ingest_file(
        self,
        path: Path | str,
        *,
        domain: Optional[str] = None,
        data_type: Optional[DataType] = None,
        tags: Optional[Sequence[str]] = None,
        replace_previous: bool = True,
    ) -> IngestReport:
        """Ingest a file, using its modification time as the default timestamp.

        Re-ingesting a file that has changed demotes the leaves of its previous
        version to ``HISTORICAL`` (see ``replace_previous`` in
        :meth:`ingest_text`), so a search never serves stale content as current.

        Raises:
            IHMTError: If the file is missing or is not decodable as text.
        """
        file_path = Path(path).expanduser()
        try:
            text = file_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise IHMTError(f"no such file: {file_path}") from exc
        except UnicodeDecodeError as exc:
            raise IHMTError(f"{file_path} is not UTF-8 text; binary files are not ingestible") from exc

        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
        return self.ingest_text(
            text,
            source=str(file_path),
            domain=domain,
            data_type=data_type,
            tags=tags,
            timestamp=mtime.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            replace_previous=replace_previous,
        )

    def ingest_directory(
        self,
        directory: Path | str,
        *,
        pattern: str = "*",
        recursive: bool = True,
        domain: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[IngestReport]:
        """Ingest every readable text file under ``directory``.

        Version-control, dependency and build directories are skipped, as are
        known binary suffixes.
        """
        base = Path(directory).expanduser()
        if not base.is_dir():
            raise IHMTError(f"not a directory: {base}")

        reports: List[IngestReport] = []
        globber = base.rglob if recursive else base.glob
        for path in sorted(globber(pattern)):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                reports.append(self.ingest_file(path, domain=domain, tags=tags))
            except IHMTError:
                continue
        return reports

    # ---------------------------------------------------------------- helpers
    def _build_leaf(
        self,
        chunk: Chunk,
        *,
        index: int,
        detection: Detection,
        source: str,
        extra_tags: Iterable[str],
        forced_timestamp: Optional[str],
    ) -> MemoryLeaf:
        """Assemble the leaf for one chunk, including its metadata header."""
        domain = detection.domain
        is_code = detection.data_type is DataType.CODE
        content = chunk.text

        timestamp = chunk.timestamp or forced_timestamp or utc_now_iso()
        tags = dedupe(
            list(chunk.tags)
            + list(extra_tags)
            + [f"type:{detection.data_type.value.lower()}", f"domain:{domain}"]
            + ([f"lang:{detection.language}"] if detection.language else []),
            limit=16,
        )
        title = truncate(f"{Path(source).name or source} · {chunk.title}", 110)
        leaf_id = self._leaf_id(domain, source, index, content)

        extra: Dict[str, Any] = {
            "chunker": chunk.meta.get("chunker", ""),
            "block_kind": chunk.kind,
            "blocks": chunk.meta.get("blocks", 1),
            "detection_confidence": round(detection.confidence, 3),
        }
        if chunk.context:
            extra["context"] = chunk.context
        if chunk.oversized:
            extra["oversized_reason"] = chunk.meta.get("oversized_reason", "")

        return MemoryLeaf(
            leaf_id=leaf_id,
            domain=domain,
            data_type=detection.data_type,
            title=title,
            content=content,
            source=source,
            timestamp=timestamp,
            ingested_at=utc_now_iso(),
            tags=tags,
            keywords=extract_keywords(f"{chunk.title}\n{content}", top_k=14, code=is_code),
            entities=extract_entities(content, limit=10),
            span=Span(chunk.start_line, chunk.end_line),
            order_index=index,
            token_estimate=estimate_tokens(content),
            checksum=f"sha256:{sha256_hex(content)}",
            oversized=chunk.oversized,
            extra=extra,
        )

    @staticmethod
    def _leaf_id(domain: str, source: str, index: int, content: str) -> str:
        """Deterministic, filesystem-safe identifier for a leaf."""
        digest = short_hash(f"{source}|{index}|{content}", 10)
        return f"L-{slugify(domain, max_length=24)}-{index:04d}-{digest}"

    def _demote_previous_version(self, source: str, current: set) -> int:
        """Mark leaves of ``source`` absent from ``current`` as ``HISTORICAL``."""
        successor = next(iter(sorted(current)), None)
        demoted = 0
        for entry in self.store.leaves_by_source(source):
            if entry.id in current or entry.status == LeafStatus.HISTORICAL.value:
                continue
            try:
                old = self.store.load_leaf(entry.id)
            except NodeNotFoundError:
                continue
            old.status = LeafStatus.HISTORICAL
            old.superseded_by = successor
            self.store.save_leaf(old)
            demoted += 1
        return demoted

    def _save_preserving_parent(self, leaf: MemoryLeaf, *, reactivate: bool = False) -> None:
        """Write a leaf, keeping any parent link a previous version had.

        Re-ingesting an unchanged document must not detach leaves from the tree
        that was already built above them — but a link is only carried over if
        the parent still exists, so a leaf can never point at a branch that was
        deleted or reset. With ``reactivate``, a leaf that an earlier version
        had demoted comes back as ``ACTIVE`` (the file returned to that text).
        """
        try:
            existing = self.store.load_leaf(leaf.leaf_id)
        except NodeNotFoundError:
            existing = None
        if existing is not None:
            parent_alive = bool(existing.parent_id) and existing.parent_id in self.store.catalog.entries
            leaf.parent_id = existing.parent_id if parent_alive else None
            if not reactivate:
                leaf.status = existing.status
                leaf.superseded_by = existing.superseded_by
        self.store.save_leaf(leaf)
