"""IHMT — Infinite Hierarchical Memory Tree.

A domain-agnostic, dependency-free long-term memory for LLMs. Knowledge is
stored as a recursive tree on the local filesystem: raw ``.txt`` leaves at
layer 0, JSON "summaries of summaries" in layers 1..N, and a single
``root.json`` trunk. Retrieval walks that tree instead of scanning it, so the
cost of a query grows with the *depth* of the tree, not with its size.

Typical use::

    from ihmt import IHMT

    memory = IHMT.initialize("./workspace")
    memory.ingest_file("notes/journal.txt")
    memory.flush()
    print(memory.search("vacaciones en Benidorm").best.excerpt)
"""

from __future__ import annotations

from .api import IHMT
from .project_index import CodeHit, FindResult, ProjectIndex, SyncReport
from .chunkers import (
    Block,
    Chunk,
    Chunker,
    CodeAwareChunker,
    GenericChunker,
    NarrativeChunker,
    ProcessChunker,
    TemporalChunker,
    get_chunker,
)
from .config import IHMTConfig
from .conflict_resolver import ConflictResolver, FactPattern
from .detectors import Detection, DomainDetector
from .models import (
    BranchNode,
    ChildRef,
    Contradiction,
    DataType,
    DomainEntry,
    Fact,
    IHMTError,
    LeafStatus,
    MalformedLeafError,
    MemoryLeaf,
    NodeKind,
    NodeNotFoundError,
    RootIndex,
    StoreNotInitializedError,
)
from .recursive_summarizer import ConsolidationReport, RecursiveSummarizer, SummarizationEvent
from .semantic_navigator import (
    ClueOption,
    ClueRequest,
    SearchResponse,
    SearchResult,
    SemanticNavigator,
)
from .storage import Catalog, CatalogEntry, MemoryStore
from .summarizers import (
    AnthropicSummarizer,
    HeuristicSummarizer,
    NodeSummary,
    SummarizerBackend,
    SummaryInput,
    get_summarizer,
)
from .universal_ingestor import IngestReport, UniversalIngestor

__version__ = "1.0.0"

__all__ = [
    "ProjectIndex",
    "SyncReport",
    "CodeHit",
    "FindResult",
    "__version__",
    # facade
    "IHMT",
    "IHMTConfig",
    # storage
    "MemoryStore",
    "Catalog",
    "CatalogEntry",
    # model
    "MemoryLeaf",
    "BranchNode",
    "ChildRef",
    "RootIndex",
    "DomainEntry",
    "Fact",
    "Contradiction",
    "DataType",
    "NodeKind",
    "LeafStatus",
    "IHMTError",
    "NodeNotFoundError",
    "MalformedLeafError",
    "StoreNotInitializedError",
    # pipeline
    "DomainDetector",
    "Detection",
    "UniversalIngestor",
    "IngestReport",
    "RecursiveSummarizer",
    "ConsolidationReport",
    "SummarizationEvent",
    "SemanticNavigator",
    "SearchResponse",
    "SearchResult",
    "ClueRequest",
    "ClueOption",
    "ConflictResolver",
    "FactPattern",
    # chunkers
    "Chunker",
    "Chunk",
    "Block",
    "CodeAwareChunker",
    "NarrativeChunker",
    "TemporalChunker",
    "ProcessChunker",
    "GenericChunker",
    "get_chunker",
    # summarizers
    "SummarizerBackend",
    "SummaryInput",
    "NodeSummary",
    "HeuristicSummarizer",
    "AnthropicSummarizer",
    "get_summarizer",
]
