"""Section-aware splitting for procedures: recipes, protocols, runbooks, manuals.

A numbered step never becomes an orphan: steps attach to the section heading
that introduces them, and an ingredient/material list is kept whole with its
heading. This keeps "how to do X" retrievable as one coherent unit.
"""

from __future__ import annotations

import re
from typing import List

from ..textutils import truncate
from .base import Block, Chunker, join_lines, split_lines

_MD_HEADING = re.compile(r"^\s*(#{1,6})\s+(\S.*)$")
_LABEL_HEADING = re.compile(r"^\s*([A-ZÀ-Ü][^:\n]{2,58}):\s*$")
_STEP_HEADING = re.compile(
    r"^\s*(?:paso|step|fase|phase|etapa|stage)\s*\d+\s*[:.\-)]?\s*(.*)$", re.IGNORECASE
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+\S")
_MATERIALS = re.compile(
    r"^\s*(ingredientes|ingredients|materiales|materials|requisitos|requirements|"
    r"utensilios|tools|equipment)\b",
    re.IGNORECASE,
)


class ProcessChunker(Chunker):
    """Splitter for step-based and section-based documents."""

    name = "process"

    def blocks(self, text: str, *, source: str = "") -> List[Block]:
        """Return one block per section, list items bound to their heading."""
        lines = split_lines(text)
        starts: List[int] = []
        hard: set[int] = set()

        for index, line in enumerate(lines, start=1):
            if not line.strip() or _LIST_ITEM.match(line):
                # List items belong to the section above them.
                continue
            heading = _MD_HEADING.match(line)
            if heading:
                starts.append(index)
                if len(heading.group(1)) <= 2:
                    hard.add(index)
                continue
            if _LABEL_HEADING.match(line) or _STEP_HEADING.match(line):
                starts.append(index)
                if _MATERIALS.match(line):
                    hard.add(index)

        if not starts:
            return self._fallback_blocks(text)
        if starts[0] != 1:
            starts.insert(0, 1)

        blocks: List[Block] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] - 1 if position + 1 < len(starts) else len(lines)
            if end < start:
                continue
            heading_text = lines[start - 1].strip()
            is_materials = bool(_MATERIALS.match(heading_text))
            is_step = bool(_STEP_HEADING.match(heading_text))
            blocks.append(
                Block(
                    text=join_lines(lines, start, end),
                    title=truncate(heading_text.lstrip("# ").rstrip(":"), 70) or f"section {position + 1}",
                    start_line=start,
                    end_line=end,
                    kind="materials" if is_materials else ("step" if is_step else "section"),
                    tags=["process"] + (["materials"] if is_materials else []) + (["step"] if is_step else []),
                    hard_boundary=start in hard,
                )
            )
        return blocks
