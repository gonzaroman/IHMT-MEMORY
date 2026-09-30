"""Language-agnostic text primitives shared by every IHMT component.

Everything here is stdlib-only and deterministic: the tree shape produced by
the offline pipeline must be reproducible so that it can be asserted in tests.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Sequence

# Rough tokens-per-character ratio. Good enough for budgeting; IHMT never needs
# an exact count, only a stable one.
_CHARS_PER_TOKEN = 4

_WORD_RE = re.compile(r"[0-9A-Za-z_À-ɏ]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+(?=[\"'¿¡(\[]?[A-ZÀ-Ü0-9])")
_CAMEL_RE = re.compile(r"[A-Z][a-z0-9]+|[A-Z]{2,}(?![a-z])|[a-z0-9]+")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

#: Stop words for the two languages IHMT is demonstrated with. Unknown
#: languages simply keep their function words, which the IDF weighting in the
#: navigator then discounts naturally.
STOPWORDS: frozenset = frozenset(
    """
    a about above after again against all am an and any are aren as at be because been before being
    below between both but by can cannot could did do does doing don down during each few for from
    further had has have having he her here hers him his how i if in into is it its itself just me
    more most my no nor not of off on once only or other our out over own same she should so some
    such than that the their them then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your
    a al algo algunas algunos ante antes como con contra cual cuando de del desde donde dos el ella
    ellas ellos en entre era erais eran eres es esa esas ese eso esos esta estaba estas este esto
    estos fue fui ha habia han hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos
    nosotros o os otra otro para pero poco por porque que quien se sea segun ser si sin sobre solo
    son su sus tambien tanto te tiene tienen todo todos tu tus un una uno unos vosotros y ya yo
    """.split()
)

#: Tokens that carry no retrieval signal in source code.
CODE_STOPWORDS: frozenset = frozenset(
    """
    public private protected static final void return new class interface extends implements import
    package this super null true false int long float double boolean char string var let const
    function def self none elif else if for while try catch except finally throw throws raise with
    as from lambda pass break continue struct namespace using include template typename auto def
    """.split()
)


# --------------------------------------------------------------------- timing
def utc_now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 ``Z`` timestamp."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    """Best-effort parse of an ISO-8601 timestamp or a bare ``YYYY-MM-DD`` date.

    Returns ``None`` instead of raising, because timestamps come from user data
    and a malformed one must never abort an ingest.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def timestamp_sort_key(value: str | None) -> float:
    """Sortable float for a timestamp; unparseable values sort first."""
    parsed = parse_timestamp(value)
    return parsed.timestamp() if parsed else float("-inf")


def year_of(value: str | None) -> str:
    """Return the year component of a timestamp, or ``"?"`` if unknown."""
    parsed = parse_timestamp(value)
    return str(parsed.year) if parsed else "?"


# --------------------------------------------------------------------- hashing
def sha256_hex(text: str) -> str:
    """Hex SHA-256 digest of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 8) -> str:
    """Short, stable, filesystem-safe digest used inside identifiers."""
    return sha256_hex(text)[:length]


# ------------------------------------------------------------------ normalize
def strip_accents(text: str) -> str:
    """Remove diacritics so that ``vacaciones`` matches ``vacacionés``."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize(text: str) -> str:
    """Lowercase and de-accent, the canonical form used for all matching."""
    return strip_accents(text).lower()


def slugify(text: str, fallback: str = "general", max_length: int = 48) -> str:
    """Convert arbitrary text into a filesystem-safe slug.

    Dots are preserved so hierarchical domains such as ``software.java``
    survive round-tripping.
    """
    cleaned = normalize(text).strip()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned).strip("-._")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:max_length] or fallback


# ----------------------------------------------------------------- tokenizing
def tokenize(text: str, *, min_length: int = 2) -> List[str]:
    """Split text into normalized word tokens, splitting CamelCase identifiers.

    ``reserveStock`` yields ``["reservestock", "reserve", "stock"]`` so that a
    natural-language query can reach a code identifier.
    """
    tokens: List[str] = []
    for raw in _WORD_RE.findall(text):
        norm = normalize(raw)
        if len(norm) >= min_length:
            tokens.append(norm)
        parts = _CAMEL_RE.findall(raw)
        if len(parts) > 1:
            tokens.extend(p for p in (normalize(x) for x in parts) if len(p) >= min_length)
        for piece in raw.split("_"):
            piece_norm = normalize(piece)
            if len(piece_norm) >= min_length and piece_norm != norm:
                tokens.append(piece_norm)
    return tokens


def content_terms(text: str, *, code: bool = False) -> List[str]:
    """Tokenize and drop stop words (plus language keywords for code)."""
    stop = STOPWORDS | CODE_STOPWORDS if code else STOPWORDS
    return [t for t in tokenize(text) if t not in stop and not t.isdigit()]


def estimate_tokens(text: str) -> int:
    """Estimate the LLM token count of ``text``.

    Uses a character ratio rather than a real tokenizer to keep IHMT free of
    dependencies; budgets only need to be consistent, not exact.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def split_sentences(text: str) -> List[str]:
    """Split a paragraph into sentences. Used only as a last-resort boundary."""
    parts = [s.strip() for s in _SENTENCE_RE.split(text.strip()) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


# ------------------------------------------------------------------- keywords
def extract_keywords(
    text: str,
    *,
    top_k: int = 12,
    code: bool = False,
    boost: Sequence[str] = (),
) -> List[str]:
    """Return the most salient terms of ``text``, most frequent first.

    Args:
        text: Source text.
        top_k: Maximum number of keywords returned.
        code: Whether to additionally filter programming-language keywords.
        boost: Terms (e.g. a title) counted twice.

    Returns:
        Deterministically ordered keywords: by descending frequency, then
        alphabetically, so identical inputs always yield identical nodes.
    """
    counts = Counter(content_terms(text, code=code))
    for term in boost:
        for token in content_terms(term, code=code):
            counts[token] += 2
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ranked[:top_k]]


def extract_entities(text: str, *, limit: int = 12) -> List[str]:
    """Heuristically extract proper nouns and code identifiers.

    Deliberately simple and language-independent: capitalized words that are
    not sentence-initial stop words, plus CamelCase identifiers. Entities are
    what let the navigator notice that "Luis" appears under several branches.
    """
    found: Dict[str, int] = {}
    for match in re.finditer(r"\b[A-ZÀ-Ü][\wÀ-ɏ]{2,}\b", text):
        word = match.group(0)
        if normalize(word) in STOPWORDS:
            continue
        found[word] = found.get(word, 0) + 1
    for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*\b", text):
        word = match.group(0)
        found[word] = found.get(word, 0) + 1
    ranked = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]


def summarize_extractive(text: str, *, max_sentences: int = 3, max_chars: int = 600) -> str:
    """Pick the most representative sentences of ``text``.

    Sentences are scored by the summed frequency of their content terms,
    normalized by length so long sentences do not automatically win. The
    selected sentences are re-emitted in their original order.
    """
    sentences = [s for s in split_sentences(text) if s]
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return " ".join(sentences)[:max_chars]

    freq = Counter(content_terms(text))
    scored: List[tuple[float, int]] = []
    for index, sentence in enumerate(sentences):
        terms = content_terms(sentence)
        if not terms:
            continue
        score = sum(freq[t] for t in terms) / (len(terms) ** 0.5)
        # Slight lead bias: openings usually carry the topic.
        score *= 1.0 + (0.15 if index == 0 else 0.0)
        scored.append((score, index))

    chosen = sorted(idx for _, idx in sorted(scored, key=lambda x: -x[0])[:max_sentences])
    return " ".join(sentences[i] for i in chosen)[:max_chars]


def truncate(text: str, limit: int) -> str:
    """Collapse whitespace and truncate to ``limit`` characters with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def dedupe(items: Iterable[str], *, limit: int | None = None) -> List[str]:
    """Order-preserving de-duplication (case-insensitive)."""
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        key = normalize(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if limit is not None and len(out) >= limit:
            break
    return out
