"""Type-aware splitters and the factory that selects one.

Adding support for a new material type means adding a :class:`~ihmt.chunkers.base.Chunker`
subclass and one entry in :func:`get_chunker` — nothing else in the framework
changes.
"""

from __future__ import annotations

from typing import Optional

from ..config import IHMTConfig
from ..models import DataType
from .base import Block, Chunk, Chunker
from .code import BraceScanner, CodeAwareChunker
from .generic import GenericChunker
from .narrative import NarrativeChunker
from .process import ProcessChunker
from .temporal import TemporalChunker, extract_date

__all__ = [
    "Block",
    "Chunk",
    "Chunker",
    "BraceScanner",
    "CodeAwareChunker",
    "GenericChunker",
    "NarrativeChunker",
    "ProcessChunker",
    "TemporalChunker",
    "extract_date",
    "get_chunker",
]


def get_chunker(data_type: DataType, config: IHMTConfig, *, source: str = "") -> Chunker:
    """Return the splitter responsible for ``data_type``.

    Args:
        data_type: Type decided by :class:`~ihmt.detectors.DomainDetector`.
        config: Store configuration (token budgets).
        source: Optional filename, used to pin the programming language.
    """
    if data_type is DataType.CODE:
        return CodeAwareChunker(config, language=CodeAwareChunker.language_for(source))
    if data_type is DataType.NARRATIVE:
        return NarrativeChunker(config)
    if data_type is DataType.CLINICAL:
        return TemporalChunker(config, clinical=True)
    if data_type is DataType.PERSONAL:
        return TemporalChunker(config, clinical=False)
    if data_type is DataType.PROCESS:
        return ProcessChunker(config)
    return GenericChunker(config)
