"""Fallback splitter for material with no recognizable structure.

Behaves like the narrative splitter with scene detection disabled: paragraphs
stay atomic, and the only cut inside a paragraph is the sentence-boundary split
of an over-long one.
"""

from __future__ import annotations

from .narrative import NarrativeChunker


class GenericChunker(NarrativeChunker):
    """Paragraph-atomic splitter used when no domain-specific rule applies."""

    name = "generic"

    def __init__(self, config) -> None:
        super().__init__(config, detect_scenes=False)
