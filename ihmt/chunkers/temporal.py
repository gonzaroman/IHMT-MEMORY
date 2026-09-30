"""Date- and session-aware splitting for journals, chats and clinical notes.

One dated entry is atomic, and a change of date is a hard boundary: a leaf never
mixes two encounters or two diary days. The date found in the text becomes the
leaf timestamp, which is what makes the conflict resolver's recency weighting
reflect *when something was true* rather than when it happened to be ingested.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern

from ..textutils import truncate
from .base import Block, Chunker, join_lines, split_lines

MONTHS: Dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
_TEXT_DATE_DMY = re.compile(
    r"\b(\d{1,2})\s*(?:de\s+|\.|\s)\s*([A-Za-zÀ-ÿ]{3,12})\.?\s*(?:de\s+|,\s*|\s)\s*(\d{4})\b",
    re.IGNORECASE,
)
_TEXT_DATE_MDY = re.compile(r"\b([A-Za-zÀ-ÿ]{3,12})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE)

#: Line shapes that open a new dated entry.
ENTRY_MARKERS: List[Pattern[str]] = [
    re.compile(r"^\s*#{1,4}\s+\S"),
    re.compile(r"^\s*\[?\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"^\s*\[?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"),
    re.compile(
        r"^\s*(?:sesi[oó]n|session|entrada|entry|consulta|visita|visit|encounter|d[ií]a|day|nota|note)\b"
        r"[^\n]{0,60}$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*[-=*]{3,}\s*$"),
]

#: Section headers typical of a clinical record.
CLINICAL_MARKERS = re.compile(
    r"^\s*(paciente|patient|motivo de consulta|chief complaint|anamnesis|antecedentes|history|"
    r"exploraci[oó]n|examination|diagn[oó]stico|diagnosis|tratamiento|treatment|plan|evoluci[oó]n|"
    r"follow[- ]?up|alergias|allergies|medicaci[oó]n|medication)\s*:",
    re.IGNORECASE,
)

#: Chat transcript turn, e.g. ``[21:30] Luis: ...`` or ``Luis: ...``.
CHAT_TURN = re.compile(r"^\s*(?:\[\s*\d{1,2}:\d{2}\s*\]\s*)?([A-ZÀ-Ü][\wÀ-ÿ .'-]{1,24}):\s")


def extract_date(text: str, *, day_first: bool = True) -> Optional[str]:
    """Extract the first date in ``text`` as an ISO-8601 UTC timestamp.

    Args:
        text: Text to scan (usually the first lines of an entry).
        day_first: Interpret ambiguous ``d/m/y`` as day-first (European
            convention). Unambiguous values (first component > 12) always win.

    Returns:
        ``"YYYY-MM-DDT00:00:00Z"``, or ``None`` if no plausible date is found.
    """

    def build(year: int, month: int, day: int) -> Optional[str]:
        if not (1 <= month <= 12 and 1 <= day <= 31 and 1000 <= year <= 9999):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"

    match = _ISO_DATE.search(text)
    if match:
        return build(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = _TEXT_DATE_DMY.search(text)
    if match:
        month = MONTHS.get(match.group(2).lower())
        if month:
            return build(int(match.group(3)), month, int(match.group(1)))

    match = _TEXT_DATE_MDY.search(text)
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            return build(int(match.group(3)), month, int(match.group(2)))

    match = _NUMERIC_DATE.search(text)
    if match:
        first, second, raw_year = (int(match.group(1)), int(match.group(2)), match.group(3))
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        day, month = (first, second) if (day_first or first > 12) else (second, first)
        if month > 12 and day <= 12:
            day, month = month, day
        return build(year, month, day)

    return None


class TemporalChunker(Chunker):
    """Splitter for personal logs, chat transcripts and clinical records."""

    name = "temporal"

    def __init__(self, config, *, clinical: bool = False) -> None:
        super().__init__(config)
        self.clinical = clinical

    def blocks(self, text: str, *, source: str = "") -> List[Block]:
        """Return one block per entry, tagged with its inherited date."""
        lines = split_lines(text)
        starts = self._entry_starts(lines)
        if not starts:
            return self._fallback_blocks(text)

        blocks: List[Block] = []
        current_date: Optional[str] = None
        for position, start in enumerate(starts):
            end = starts[position + 1] - 1 if position + 1 < len(starts) else len(lines)
            body = join_lines(lines, start, end)
            head = "".join(lines[start - 1 : min(start + 1, len(lines))])
            found = extract_date(head) or extract_date(body[:400])
            is_new_day = bool(found) and found != current_date
            if found:
                current_date = found

            speaker = CHAT_TURN.match(lines[start - 1])
            tags = ["entry"]
            if speaker:
                tags.append(f"speaker:{speaker.group(1).strip()}")
            if self.clinical:
                tags.append("clinical")
            if current_date:
                tags.append(f"date:{current_date[:10]}")

            blocks.append(
                Block(
                    text=body,
                    title=truncate(lines[start - 1].strip() or f"entry {position + 1}", 70),
                    start_line=start,
                    end_line=end,
                    kind="encounter" if self.clinical else "entry",
                    tags=tags,
                    timestamp=current_date,
                    hard_boundary=is_new_day,
                )
            )
        return blocks

    def _entry_starts(self, lines: List[str]) -> List[int]:
        """1-based line numbers where a new entry begins."""
        starts: List[int] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            is_start = (
                any(pattern.match(line) for pattern in ENTRY_MARKERS)
                or bool(CHAT_TURN.match(line))
                or (self.clinical and bool(CLINICAL_MARKERS.match(line)))
            )
            if is_start:
                starts.append(index)
        if starts and starts[0] != 1:
            # Preamble before the first marker becomes its own leading entry.
            starts.insert(0, 1)
        return starts
