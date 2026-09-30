"""Paragraph- and scene-aware splitting for prose.

A paragraph is atomic. Scene and chapter markers are hard boundaries, so a
chunk never straddles a scene change. The only case that ever cuts inside a
paragraph is a single paragraph larger than ``max_tokens``, which is split at
sentence boundaries — never mid-sentence.
"""

from __future__ import annotations

import re
from typing import List, Pattern

from ..textutils import truncate
from .base import Block, Chunker, join_lines, split_lines

#: Lines that open a new scene, chapter or section.
SCENE_MARKERS: List[Pattern[str]] = [
    re.compile(r"^\s*(?:\*\s*){3,}\s*$"),
    re.compile(r"^\s*[-–—=~_]{3,}\s*$"),
    re.compile(r"^\s*#{1,3}\s+\S"),
    re.compile(r"^\s*(?:chapter|cap[ií]tulo|part|parte|scene|escena|act|acto)\b.{0,60}$", re.IGNORECASE),
    re.compile(r"^\s*(?:prologue|pr[oó]logo|epilogue|ep[ií]logo)\b.{0,40}$", re.IGNORECASE),
]

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])[\"'”’)\]]?\s+(?=[\"'“¿¡(\[]?[A-ZÀ-Ü0-9])")


def is_scene_marker(line: str) -> bool:
    """Whether ``line`` opens a new scene/chapter/section."""
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in SCENE_MARKERS)


def split_sentences_exact(text: str) -> List[str]:
    """Split into sentences without losing a single character.

    Unlike :func:`ihmt.textutils.split_sentences`, the separating whitespace is
    kept attached to the preceding sentence so that ``"".join(result) == text``.
    """
    cuts = [match.end() for match in _SENTENCE_BOUNDARY.finditer(text)]
    if not cuts:
        return [text]
    parts: List[str] = []
    previous = 0
    for cut in cuts:
        parts.append(text[previous:cut])
        previous = cut
    if previous < len(text):
        parts.append(text[previous:])
    return parts


class NarrativeChunker(Chunker):
    """Splitter for stories, essays, articles and any free-flowing prose."""

    name = "narrative"

    def __init__(self, config, *, detect_scenes: bool = True) -> None:
        super().__init__(config)
        self.detect_scenes = detect_scenes

    def blocks(self, text: str, *, source: str = "") -> List[Block]:
        """Return one block per paragraph, with scenes as hard boundaries."""
        lines = split_lines(text)
        blocks: List[Block] = []
        index = 0
        total = len(lines)

        while index < total:
            start = index
            while index < total and not lines[index].strip():
                index += 1
            if index >= total:
                # Trailing whitespace: keep it so the text round-trips exactly.
                if blocks:
                    blocks[-1].text += join_lines(lines, start + 1, total)
                    blocks[-1].end_line = total
                break

            content_start = index
            scene = self.detect_scenes and is_scene_marker(lines[content_start])
            while index < total and lines[index].strip():
                index += 1

            block = Block(
                text=join_lines(lines, start + 1, index),
                title=truncate(lines[content_start].strip(), 70),
                start_line=start + 1,
                end_line=index,
                kind="scene" if scene else "paragraph",
                tags=["scene"] if scene else [],
                hard_boundary=scene,
            )
            if block.tokens > self.config.max_tokens:
                blocks.extend(self._split_long_paragraph(block))
            else:
                blocks.append(block)

        return blocks

    def _split_long_paragraph(self, block: Block) -> List[Block]:
        """Break an over-long paragraph at sentence boundaries."""
        sentences = split_sentences_exact(block.text)
        if len(sentences) < 2:
            return [block]

        out: List[Block] = []
        buffer: List[str] = []
        line_cursor = block.start_line
        part = 1
        target = self.config.target_tokens

        def flush() -> None:
            nonlocal buffer, line_cursor, part
            if not buffer:
                return
            body = "".join(buffer)
            span = body.count("\n")
            out.append(
                Block(
                    text=body,
                    title=f"{block.title} (part {part})",
                    start_line=line_cursor,
                    end_line=line_cursor + span,
                    kind="paragraph-part",
                    tags=list(block.tags) + ["split:sentence"],
                )
            )
            line_cursor += span
            part += 1
            buffer = []

        for sentence in sentences:
            candidate = "".join(buffer) + sentence
            if buffer and len(candidate) // 4 > target:
                flush()
            buffer.append(sentence)
        flush()
        return out or [block]
