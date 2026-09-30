"""High-level facade wiring the five components together.

Most callers — the CLI, a notebook, or an LLM tool layer — only need this::

    from ihmt import IHMT

    memory = IHMT.initialize("./workspace")     # or IHMT("./workspace")
    memory.ingest_file("InventoryService.java")
    memory.flush()                              # close the tree
    answer = memory.search("reserveStock concurrency")
    answer = memory.ask("Luis", clue_provider=input_clue)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import IHMTConfig
from .conflict_resolver import ConflictResolver
from .detectors import DomainDetector
from .models import Contradiction, DataType, MemoryLeaf
from .recursive_summarizer import ConsolidationReport, RecursiveSummarizer
from .semantic_navigator import ClueRequest, SearchResponse, SemanticNavigator
from .storage import MemoryStore
from .summarizers import SummarizerBackend
from .universal_ingestor import IngestReport, UniversalIngestor


class IHMT:
    """One initialized memory tree, with every component pre-wired."""

    def __init__(
        self,
        base_dir: Path | str = ".",
        *,
        backend: Optional[SummarizerBackend] = None,
        auto_consolidate: bool = True,
        require_init: bool = True,
    ) -> None:
        """
        Args:
            base_dir: Directory containing (or to contain) ``ihmt_memory``.
            backend: Summarization backend override.
            auto_consolidate: Fire Summarization Events during ingestion.
            require_init: Raise if the store does not exist yet.
        """
        self.store = MemoryStore.open(base_dir, require_init=require_init)
        self.config: IHMTConfig = self.store.config
        self.detector = DomainDetector()
        self.resolver = ConflictResolver(self.store)
        self.summarizer = RecursiveSummarizer(self.store, backend=backend)
        self.ingestor = UniversalIngestor(
            self.store,
            detector=self.detector,
            summarizer=self.summarizer,
            conflict_resolver=self.resolver,
            auto_consolidate=auto_consolidate,
        )
        self.navigator = SemanticNavigator(self.store, conflict_resolver=self.resolver)

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def initialize(
        cls,
        base_dir: Path | str = ".",
        *,
        force: bool = False,
        **overrides: Any,
    ) -> "IHMT":
        """Create the store on disk (idempotent) and return a ready facade.

        Args:
            base_dir: Where to create ``ihmt_memory``.
            force: Discard all *derived* state — trunk, branch layers, catalog
                and fact timeline — and detach the surviving leaves so the next
                consolidation rebuilds the hierarchy. Leaf content is kept.
            **overrides: Configuration fields, e.g. ``branch_factor=4``.
        """
        config = IHMTConfig.load(base_dir)
        for key, value in overrides.items():
            if not hasattr(config, key):
                raise ValueError(f"unknown configuration field: {key}")
            setattr(config, key, value)
        config.validate()
        MemoryStore(config).initialize(force=force)
        return cls(base_dir)

    # -------------------------------------------------------------- ingestion
    def ingest_text(self, text: str, **kwargs: Any) -> IngestReport:
        """Ingest an in-memory document. See :meth:`UniversalIngestor.ingest_text`."""
        return self.ingestor.ingest_text(text, **kwargs)

    def ingest_file(self, path: Path | str, **kwargs: Any) -> IngestReport:
        """Ingest a single file."""
        return self.ingestor.ingest_file(path, **kwargs)

    def ingest_directory(self, path: Path | str, **kwargs: Any) -> List[IngestReport]:
        """Ingest every text file under a directory."""
        return self.ingestor.ingest_directory(path, **kwargs)

    # ---------------------------------------------------------- consolidation
    def consolidate(self, *, force: bool = False) -> ConsolidationReport:
        """Run pending Summarization Events and rewrite the trunk."""
        return self.summarizer.consolidate(force=force)

    def flush(self) -> ConsolidationReport:
        """Close the tree completely, promoting partial groups as well."""
        return self.summarizer.flush()

    # ------------------------------------------------------------- retrieval
    def search(self, query: str, **kwargs: Any) -> SearchResponse:
        """Walk the tree for ``query``."""
        return self.navigator.search(query, **kwargs)

    def ask(
        self,
        query: str,
        clue_provider: Callable[[ClueRequest], Optional[str]],
        **kwargs: Any,
    ) -> SearchResponse:
        """Search with the interactive clue loop enabled."""
        return self.navigator.search_interactive(query, clue_provider, **kwargs)

    def get_leaf(self, leaf_id: str) -> MemoryLeaf:
        """Fetch one leaf by identifier."""
        return self.store.load_leaf(leaf_id)

    def outline(self, **kwargs: Any) -> List[str]:
        """Readable outline of the tree."""
        return self.navigator.outline(**kwargs)

    # -------------------------------------------------------------- timeline
    def record_fact(self, subject: str, attribute: str, value: str, **kwargs: Any):
        """Add an assertion to the timeline."""
        return self.resolver.record_fact(subject, attribute, value, **kwargs)

    def conflicts(self, **kwargs: Any) -> List[Contradiction]:
        """Every attribute whose value changed over time."""
        return self.resolver.detect_conflicts(**kwargs)

    def notices(self, **kwargs: Any) -> List[str]:
        """User-facing contradiction notices."""
        return [c.render_notice() for c in self.resolver.detect_conflicts(**kwargs)]

    # ------------------------------------------------------------ inspection
    def stats(self) -> Dict[str, Any]:
        """Counters describing the store."""
        stats = self.store.stats()
        stats["facts"] = len(self.resolver.facts)
        stats["conflicts"] = len(self.resolver.detect_conflicts())
        return stats

    def rebuild(self) -> Dict[str, Any]:
        """Rebuild the catalog, the timeline and the trunk from the files."""
        self.store.rebuild_catalog()
        facts = self.resolver.rebuild()
        self.summarizer.rebuild_root()
        return {"catalog_entries": len(self.store.catalog.entries), "facts": facts}


__all__ = ["IHMT"]
