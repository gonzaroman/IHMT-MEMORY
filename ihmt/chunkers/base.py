"""Chunker interface and the block-packing algorithm shared by all splitters.

Every splitter works in two phases:

1. **Block detection** — cut the document only at boundaries that are
   *meaningful for its type*: a syntactic block for code, a paragraph or scene
   for narrative, a dated entry for a journal or clinical note. A block is the
   atomic unit: IHMT never cuts inside one.
2. **Packing** (:meth:`Chunker.pack`) — greedily merge consecutive blocks up to
   ``target_tokens``. If a single block exceeds ``max_tokens`` it is emitted
   whole and flagged ``oversized``: block integrity outranks the token budget.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import IHMTConfig
from ..textutils import estimate_tokens, truncate


@dataclass
class Block:
    """An indivisible span of a source document.

    Attributes:
        text: Exact source text, byte-for-byte, including trailing newlines.
        title: Human-readable label (``"class InventoryService"``, a scene
            heading, a journal date...).
        start_line: 1-based inclusive first line in the source.
        end_line: 1-based inclusive last line in the source.
        kind: Block flavour, e.g. ``"class"``, ``"method"``, ``"paragraph"``.
        tags: Structural tags propagated to the resulting leaf.
        timestamp: Date extracted from the content, when the type carries one.
        hard_boundary: If true, this block never merges with the previous one
            (a new scene, a new clinical encounter, a new journal date).
        context: Enclosing context that is *not* part of the text, such as the
            class header of an extracted method. Stored in leaf metadata so the
            LLM sees it without corrupting the byte-exact content.
    """

    text: str
    title: str = ""
    start_line: int = 1
    end_line: int = 1
    kind: str = "block"
    tags: List[str] = field(default_factory=list)
    timestamp: Optional[str] = None
    hard_boundary: bool = False
    context: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass
class Chunk:
    """A packed group of blocks, ready to become a :class:`~ihmt.models.MemoryLeaf`."""

    text: str
    title: str = ""
    start_line: int = 1
    end_line: int = 1
    kind: str = "chunk"
    tags: List[str] = field(default_factory=list)
    timestamp: Optional[str] = None
    oversized: bool = False
    context: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def split_lines(text: str) -> List[str]:
    """Split into lines while keeping the line terminators (exact round-trip)."""
    return text.splitlines(keepends=True)


def join_lines(lines: Sequence[str], start_line: int, end_line: int) -> str:
    """Join 1-based inclusive line range ``[start_line, end_line]``."""
    return "".join(lines[start_line - 1 : end_line])


class Chunker(ABC):
    """Base class for every splitter.

    Subclasses implement :meth:`blocks`; :meth:`chunk` packs those blocks into
    leaf-sized units. Overriding :meth:`chunk` directly is allowed when a type
    needs custom grouping (see the temporal splitter).
    """

    #: Short identifier recorded in leaf metadata.
    name: str = "base"

    def __init__(self, config: IHMTConfig) -> None:
        self.config = config

    @abstractmethod
    def blocks(self, text: str, *, source: str = "") -> List[Block]:
        """Detect the indivisible blocks of ``text``."""

    def chunk(self, text: str, *, source: str = "") -> List[Chunk]:
        """Split ``text`` into leaf-sized chunks. The public entry point."""
        if not text.strip():
            return []
        return self.pack(self.blocks(text, source=source))

    # ------------------------------------------------------------------ packing
    def pack(self, blocks: Sequence[Block]) -> List[Chunk]:
        """Merge consecutive blocks into chunks of roughly ``target_tokens``.

        The packing rules, in priority order:

        1. A block larger than ``max_tokens`` is emitted alone and flagged
           ``oversized`` — it is never cut.
        2. A block marked ``hard_boundary`` always starts a new chunk.
        3. Otherwise blocks accumulate until adding one would exceed
           ``target_tokens``, provided the current chunk already carries at
           least ``min_tokens`` (this avoids emitting slivers).
        """
        target = self.config.target_tokens
        maximum = self.config.max_tokens
        minimum = self.config.min_tokens

        chunks: List[Chunk] = []
        current: List[Block] = []
        current_tokens = 0

        def flush() -> None:
            nonlocal current, current_tokens
            if current:
                chunks.append(self._merge(current))
                current = []
                current_tokens = 0

        for block in blocks:
            if block.is_blank:
                # Whitespace-only runs ride along with the preceding block so
                # that concatenating chunks reproduces the source exactly.
                if current:
                    current.append(block)
                else:
                    current = [block]
                    current_tokens = 0
                continue

            tokens = block.tokens
            if tokens > maximum:
                flush()
                chunk = self._merge([block])
                chunk.oversized = True
                chunk.meta["oversized_reason"] = (
                    f"indivisible {block.kind} of ~{tokens} tokens exceeds max_tokens={maximum}"
                )
                chunks.append(chunk)
                continue

            if current and (block.hard_boundary or (current_tokens + tokens > target and current_tokens >= minimum)):
                flush()

            current.append(block)
            current_tokens += tokens

        flush()
        return chunks

    def pack_each(self, blocks: Sequence[Block]) -> List[Chunk]:
        """One chunk per block (symbol granularity).

        Whitespace-only blocks ride along with the preceding block, exactly as
        in :meth:`pack`, so concatenating the chunks reproduces the source.
        Runs of *small* blocks of the same kind — import lines, fields, trivial
        accessors — are grouped up to ``target_tokens``, so a lookup never has
        to wade through a dozen one-line leaves.
        """
        small = max(24, self.config.target_tokens // 4)

        def solid(group: List[Block]) -> List[Block]:
            return [b for b in group if not b.is_blank]

        groups: List[List[Block]] = []
        leading: List[Block] = []
        for block in blocks:
            if block.is_blank:
                if groups:
                    groups[-1].append(block)
                else:
                    leading.append(block)
                continue
            if groups and not leading and block.tokens < small:
                previous = solid(groups[-1])
                if (
                    previous
                    and previous[-1].tokens < small
                    and previous[-1].kind == block.kind
                    and sum(b.tokens for b in previous) + block.tokens <= self.config.target_tokens
                ):
                    groups[-1].append(block)
                    continue
            groups.append(leading + [block])
            leading = []
        if leading:
            groups.append(leading)

        chunks: List[Chunk] = []
        for group in groups:
            chunk = self._merge(group)
            big = [b for b in group if not b.is_blank and b.tokens > self.config.max_tokens]
            if big:
                chunk.oversized = True
                chunk.meta["oversized_reason"] = (
                    f"indivisible {big[0].kind} of ~{big[0].tokens} tokens exceeds max_tokens={self.config.max_tokens}"
                )
            chunks.append(chunk)
        return chunks

    def _merge(self, blocks: Sequence[Block]) -> Chunk:
        """Fuse a run of blocks into a single chunk, preserving exact text."""
        named = [b for b in blocks if b.title and not b.is_blank] or list(blocks)
        if len(named) == 1:
            title = named[0].title
        else:
            title = truncate(f"{named[0].title} → {named[-1].title}", 90)

        tags: List[str] = []
        for block in blocks:
            for tag in block.tags:
                if tag not in tags:
                    tags.append(tag)

        timestamps = [b.timestamp for b in blocks if b.timestamp]
        contexts = [b.context for b in blocks if b.context]
        kinds = {b.kind for b in blocks if not b.is_blank}

        return Chunk(
            text="".join(b.text for b in blocks),
            title=title or "untitled",
            start_line=blocks[0].start_line,
            end_line=blocks[-1].end_line,
            kind=kinds.pop() if len(kinds) == 1 else "mixed",
            tags=tags,
            timestamp=timestamps[0] if timestamps else None,
            context=contexts[0] if contexts else "",
            meta={"blocks": len(blocks), "chunker": self.name},
        )

    # ------------------------------------------------------------------ helpers
    def _fallback_blocks(self, text: str) -> List[Block]:
        """Line-based blocks used when no structure can be detected.

        Cuts only at blank lines, so even the degenerate path never splits a
        line in half.
        """
        lines = split_lines(text)
        blocks: List[Block] = []
        start = 1
        for index, line in enumerate(lines, start=1):
            if not line.strip() and index > start:
                blocks.append(
                    Block(
                        text=join_lines(lines, start, index),
                        title=self._first_meaningful_line(lines[start - 1 : index]),
                        start_line=start,
                        end_line=index,
                        kind="segment",
                    )
                )
                start = index + 1
        if start <= len(lines):
            blocks.append(
                Block(
                    text=join_lines(lines, start, len(lines)),
                    title=self._first_meaningful_line(lines[start - 1 :]),
                    start_line=start,
                    end_line=len(lines),
                    kind="segment",
                )
            )
        return blocks

    @staticmethod
    def _first_meaningful_line(lines: Sequence[str], limit: int = 70) -> str:
        """Title heuristic: the first non-empty line, trimmed."""
        for line in lines:
            stripped = line.strip()
            if stripped:
                return truncate(stripped, limit)
        return ""
