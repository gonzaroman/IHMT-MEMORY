"""Bottom-up construction and maintenance of the branch layers.

The summarizer watches layer 0. As soon as ``branch_factor`` leaves of the same
domain have no parent, it fires a **Summarization Event**: those leaves are
condensed into a layer-1 branch node, which is written, and only then are the
children stamped with their ``parent_id``. The same rule then applies to layer 1
against layer 2, and so on, until the tree converges — after which ``root.json``
is rewritten to point at the top branch of every domain.

Two properties matter and are tested:

* **Idempotence** — running :meth:`RecursiveSummarizer.consolidate` twice with
  no new material is a no-op.
* **Crash safety** — a parent is durable before its children reference it, so an
  interrupted run re-processes a group instead of orphaning it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import BranchNode, ChildRef, DomainEntry, NodeKind, NodeNotFoundError, RootIndex, TimeRange
from .storage import CatalogEntry, MemoryStore
from .summarizers import SummarizerBackend, SummaryInput, get_summarizer
from .textutils import dedupe, short_hash, slugify, timestamp_sort_key, truncate, utc_now_iso

#: Safety valve: no tree ever legitimately needs this many layers.
MAX_LAYERS = 64


@dataclass
class SummarizationEvent:
    """Record of one branch node being created."""

    node_id: str
    layer: int
    domain: str
    child_ids: List[str]
    trigger: str  # "threshold" | "flush"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "layer": self.layer,
            "domain": self.domain,
            "children": len(self.child_ids),
            "child_ids": self.child_ids,
            "trigger": self.trigger,
            "created_at": self.created_at,
        }


@dataclass
class ConsolidationReport:
    """Outcome of a consolidation pass."""

    events: List[SummarizationEvent] = field(default_factory=list)
    depth: int = 0
    pending: Dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """Whether any branch node was created."""
        return bool(self.events)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "created_nodes": len(self.events),
            "depth": self.depth,
            "pending": self.pending,
        }


class RecursiveSummarizer:
    """Builds and maintains layers 1..N, then rewrites the trunk."""

    def __init__(self, store: MemoryStore, backend: Optional[SummarizerBackend] = None) -> None:
        """
        Args:
            store: Store to consolidate.
            backend: Summarization backend; resolved from the store config when
                omitted (offline heuristic by default).
        """
        self.store = store
        self.config = store.config
        self.backend = backend or get_summarizer(store.config)

    # ------------------------------------------------------------------- api
    def consolidate(self, *, force: bool = False) -> ConsolidationReport:
        """Propagate summaries upward until the tree converges.

        Args:
            force: Also promote groups smaller than ``branch_factor`` (a
                "flush"). Use when you want the tree fully closed rather than
                waiting for more material.

        Returns:
            A :class:`ConsolidationReport` listing every Summarization Event.
        """
        report = ConsolidationReport()
        layer = 0
        while layer < MAX_LAYERS:
            report.events.extend(self._consolidate_layer(layer, force=force))
            layer += 1
            if not self._has_work(layer, force=force):
                break

        self.rebuild_root()
        report.depth = self.store.max_layer()
        report.pending = self._pending_by_domain_counts(0)
        return report

    def flush(self) -> ConsolidationReport:
        """Close the tree completely, promoting partial groups too."""
        return self.consolidate(force=True)

    # -------------------------------------------------------------- internals
    def _has_work(self, layer: int, *, force: bool) -> bool:
        """Whether ``layer`` holds a group large enough to be promoted."""
        groups = self._pending_by_domain(layer)
        threshold = 2 if force else self.config.branch_factor
        return any(len(items) >= threshold for items in groups.values())

    def _pending_by_domain(self, layer: int) -> Dict[str, List[CatalogEntry]]:
        """Parentless entries at ``layer``, grouped by domain."""
        groups: Dict[str, List[CatalogEntry]] = defaultdict(list)
        for entry in self.store.pending(layer):
            groups[entry.domain].append(entry)
        return groups

    def _pending_by_domain_counts(self, layer: int) -> Dict[str, int]:
        return {domain: len(items) for domain, items in sorted(self._pending_by_domain(layer).items())}

    def _consolidate_layer(self, layer: int, *, force: bool) -> List[SummarizationEvent]:
        """Fire every Summarization Event due at ``layer``."""
        branch_factor = self.config.branch_factor
        events: List[SummarizationEvent] = []

        for domain, entries in sorted(self._pending_by_domain(layer).items()):
            index = 0
            while index < len(entries):
                batch = entries[index : index + branch_factor]
                complete = len(batch) == branch_factor
                if not complete:
                    # A partial group only becomes a node on an explicit flush,
                    # and never as a pointless single-child link.
                    if not force or len(batch) < 2:
                        break
                node = self._build_node(batch, layer=layer + 1, domain=domain)
                self.store.save_node(node)
                for child in batch:
                    self.store.set_parent(child.id, node.node_id)
                events.append(
                    SummarizationEvent(
                        node_id=node.node_id,
                        layer=layer + 1,
                        domain=domain,
                        child_ids=[c.id for c in batch],
                        trigger="threshold" if complete else "flush",
                    )
                )
                index += len(batch)
        return events

    def _build_node(self, entries: Sequence[CatalogEntry], *, layer: int, domain: str) -> BranchNode:
        """Summarize ``entries`` into their parent branch node."""
        children = [self._child_ref(entry) for entry in entries]
        inputs = [self._summary_input(entry, ref) for entry, ref in zip(entries, children)]
        summary = self.backend.summarize(inputs, domain=domain, layer=layer)

        timestamps = sorted(t for t in (c.timestamp for c in children) if t)
        node_id = f"N{layer}-{slugify(domain, max_length=24)}-{short_hash('|'.join(c.id for c in children), 10)}"

        return BranchNode(
            node_id=node_id,
            layer=layer,
            domain=domain,
            title=summary.title,
            summary=summary.summary,
            keywords=summary.keywords,
            tags=summary.tags,
            entities=summary.entities,
            children=children,
            time_range=TimeRange(timestamps[0] if timestamps else "", timestamps[-1] if timestamps else ""),
            leaf_count=sum(child.leaf_count for child in children),
            backend=getattr(self.backend, "name", "heuristic"),
        )

    def _child_ref(self, entry: CatalogEntry) -> ChildRef:
        """Build the parent-side descriptor of one child.

        The descriptor carries enough signal (title, excerpt, keywords, tags)
        for the navigator to rank the child *without opening its file* — the
        property that keeps retrieval logarithmic.
        """
        excerpt_chars = self.config.excerpt_chars
        if entry.kind is NodeKind.LEAF:
            leaf = self.store.load_leaf(entry.id)
            return ChildRef(
                id=leaf.leaf_id,
                kind=NodeKind.LEAF,
                title=leaf.title,
                excerpt=leaf.excerpt(excerpt_chars),
                path=self.store.relative(self.store.leaf_path(leaf.leaf_id, leaf.domain)),
                keywords=list(leaf.keywords[:10]),
                tags=list(leaf.tags[:8]),
                timestamp=leaf.timestamp,
                leaf_count=1,
            )
        node = self.store.load_node(entry.id)
        return ChildRef(
            id=node.node_id,
            kind=NodeKind.NODE,
            title=node.title,
            excerpt=truncate(node.summary, excerpt_chars),
            path=self.store.relative(self.store.node_path(node.node_id, node.layer)),
            keywords=list(node.keywords[:10]),
            tags=list(node.tags[:8]),
            timestamp=node.time_range.end or node.updated_at,
            leaf_count=max(1, node.leaf_count),
        )

    @staticmethod
    def _summary_input(entry: CatalogEntry, ref: ChildRef) -> SummaryInput:
        return SummaryInput(
            id=ref.id,
            title=ref.title,
            text=ref.excerpt,
            keywords=list(ref.keywords),
            tags=list(ref.tags),
            timestamp=ref.timestamp or entry.timestamp,
        )

    # ------------------------------------------------------------------ trunk
    def rebuild_root(self) -> RootIndex:
        """Rewrite ``root.json`` from the current state of the tree.

        The trunk indexes, per domain, every parentless item — normally the top
        branch nodes, but also any leaf still awaiting consolidation, so that
        *nothing in the store is unreachable from the root*.
        """
        try:
            root = self.store.load_root()
        except Exception:  # pragma: no cover - a missing trunk is recoverable
            root = RootIndex()

        tops: Dict[str, List[CatalogEntry]] = defaultdict(list)
        for entry in self.store.catalog.entries.values():
            if not entry.parent_id:
                tops[entry.domain].append(entry)

        domains: Dict[str, DomainEntry] = {}
        for domain, entries in sorted(tops.items()):
            entries.sort(key=lambda e: (-e.layer, timestamp_sort_key(e.timestamp), e.id))
            # No trimming: *everything* without a parent is referenced here. A
            # silent cap would push material out of the root's reach —
            # invisible to search, the interface and the MCP server. The
            # amount is bounded by consolidation: at most branch_factor-1
            # items remain pending per layer.
            refs = [self._child_ref(entry) for entry in entries]
            digest = self.backend.summarize(
                [self._summary_input(entry, ref) for entry, ref in zip(entries, refs)],
                domain=domain,
                layer=max(e.layer for e in entries) + 1,
            )
            timestamps = sorted(t for t in (r.timestamp for r in refs) if t)
            domains[domain] = DomainEntry(
                domain=domain,
                summary=digest.summary,
                keywords=digest.keywords,
                top_nodes=refs,
                leaf_count=sum(r.leaf_count for r in refs),
                time_range=TimeRange(timestamps[0] if timestamps else "", timestamps[-1] if timestamps else ""),
            )

        stats = self.store.stats()
        root.domains = domains
        root.depth = stats["depth"]
        root.leaf_count = stats["leaves"]
        root.node_count = stats["nodes"]
        root.topics = self._global_topics(domains.values())
        root.summary = self._global_summary(domains.values(), stats)
        root.time_range = self._global_time_range(domains.values())
        self.store.save_root(root)
        return root

    @staticmethod
    def _global_topics(domains: Iterable[DomainEntry], limit: int = 24) -> List[str]:
        """Cross-domain topic list shown at the top of the trunk."""
        topics: List[str] = []
        for domain in domains:
            topics.append(domain.domain)
            topics.extend(domain.keywords[:5])
        return dedupe(topics, limit=limit)

    @staticmethod
    def _global_summary(domains: Iterable[DomainEntry], stats: Dict[str, Any]) -> str:
        """One-paragraph description of the whole store."""
        parts = [
            f"{d.domain} ({d.leaf_count} leaves"
            + (f", {d.time_range.start[:10]}..{d.time_range.end[:10]}" if d.time_range.start else "")
            + ")"
            for d in domains
        ]
        if not parts:
            return "Empty memory tree."
        return truncate(
            f"{stats['leaves']} leaves across {len(parts)} domains, depth {stats['depth']}. "
            f"Domains: {'; '.join(parts)}.",
            900,
        )

    @staticmethod
    def _global_time_range(domains: Iterable[DomainEntry]) -> TimeRange:
        starts = sorted(d.time_range.start for d in domains if d.time_range.start)
        ends = sorted(d.time_range.end for d in domains if d.time_range.end)
        return TimeRange(starts[0] if starts else "", ends[-1] if ends else "")

    # ------------------------------------------------------------- inspection
    def path_to_root(self, node_id: str) -> List[str]:
        """Return the ancestor chain of ``node_id``, ending at ``"root"``.

        Raises:
            NodeNotFoundError: If ``node_id`` is unknown.
        """
        entry = self.store.catalog.entries.get(node_id)
        if entry is None:
            raise NodeNotFoundError(f"{node_id!r} is not in the catalog")
        chain = [node_id]
        seen = {node_id}
        while entry is not None and entry.parent_id:
            if entry.parent_id in seen:  # pragma: no cover - cycle guard
                break
            chain.append(entry.parent_id)
            seen.add(entry.parent_id)
            entry = self.store.catalog.entries.get(entry.parent_id)
        chain.append("root")
        return chain
