"""Timeline management and contradiction handling.

Memory that only ever appends eventually contradicts itself: "I live in Madrid"
(2024) and "I live in Valencia" (2026) are both in the store, and a naive
retriever will happily serve either. IHMT resolves this with **recency
weighting** over a per-attribute timeline:

* every dated assertion is kept — nothing is deleted;
* the most recent value of a ``(subject, attribute)`` pair is ``ACTIVE``;
* every earlier value becomes ``HISTORICAL``, stamped with ``superseded_by``
  and a ``valid_from``/``valid_to`` interval, so the past stays queryable;
* whenever a value changes, a transparent notice is produced for the user:
  *"In 2024 you said ... but in 2026 you updated to ..."*.

Facts arrive either programmatically through :meth:`ConflictResolver.record_fact`
or by pattern extraction from leaves during ingestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

from .models import Contradiction, Fact, LeafStatus, MemoryLeaf, NodeNotFoundError
from .storage import MemoryStore
from .textutils import (
    normalize,
    short_hash,
    timestamp_sort_key,
    truncate,
    utc_now_iso,
)


@dataclass(frozen=True)
class FactPattern:
    """A regex rule that lifts a dated fact out of free text."""

    attribute: str
    pattern: Pattern[str]
    subject: str = "user"
    group: int = 1

    def apply(self, text: str) -> List[str]:
        """Return every value this rule finds in ``text``."""
        return [match.group(self.group).strip() for match in self.pattern.finditer(text) if match.group(self.group)]


#: Value-bearing tail of a sentence: stop at punctuation or a clause break.
_VALUE = r"([^.,;:\n!?]{2,80})"

#: Built-in extraction rules (English + Spanish). Extend via
#: :meth:`ConflictResolver.add_pattern` for a new domain.
DEFAULT_PATTERNS: Tuple[FactPattern, ...] = (
    FactPattern("location", re.compile(rf"\b(?:vivo|resido|estoy viviendo)\s+en\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("location", re.compile(rf"\bme\s+(?:he\s+)?mudad[oa]\s+a\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("location", re.compile(rf"\bI\s+(?:now\s+)?live\s+in\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("location", re.compile(rf"\bI\s+moved\s+to\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("employer", re.compile(rf"\btrabajo\s+en\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("employer", re.compile(rf"\bI\s+work\s+(?:at|for)\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("stack", re.compile(rf"\b(?:mi|nuestro)\s+stack\s+(?:es|ser[aá])\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("stack", re.compile(rf"\b(?:our|my)\s+stack\s+is\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("stack", re.compile(rf"\bmigra(?:mos|do)\s+(?:el\s+backend\s+)?a\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("stack", re.compile(rf"\bwe\s+migrated\s+to\s+{_VALUE}", re.IGNORECASE)),
    FactPattern("diagnosis", re.compile(rf"^\s*(?:diagn[oó]stico|diagnosis)\s*:\s*{_VALUE}", re.IGNORECASE | re.MULTILINE)),
    FactPattern("treatment", re.compile(rf"^\s*(?:tratamiento|treatment)\s*:\s*{_VALUE}", re.IGNORECASE | re.MULTILINE)),
    FactPattern("medication", re.compile(rf"^\s*(?:medicaci[oó]n|medication)\s*:\s*{_VALUE}", re.IGNORECASE | re.MULTILINE)),
)

#: Rule locating the subject of a clinical record.
PATIENT_RE = re.compile(r"^\s*(?:paciente|patient)\s*:\s*([^\n,;(]{2,60})", re.IGNORECASE | re.MULTILINE)

#: Attributes whose subject is the patient rather than the user.
CLINICAL_ATTRIBUTES = frozenset({"diagnosis", "treatment", "medication"})


class ConflictResolver:
    """Maintains the fact timeline and reports contradictions.

    Also acts as the *Timeline Manager*: :meth:`state_at` answers "what was true
    on this date?", while :meth:`active_state` answers "what is true now?".
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        patterns: Optional[Sequence[FactPattern]] = None,
        auto_flag_leaves: bool = True,
    ) -> None:
        """
        Args:
            store: Store whose ``state/facts.json`` is managed.
            patterns: Extraction rules; defaults to :data:`DEFAULT_PATTERNS`.
            auto_flag_leaves: Annotate a leaf's metadata when one of the facts
                it sourced is superseded.
        """
        self.store = store
        self.patterns: List[FactPattern] = list(patterns if patterns is not None else DEFAULT_PATTERNS)
        self.auto_flag_leaves = auto_flag_leaves
        self._facts: Optional[List[Fact]] = None

    # ---------------------------------------------------------- persistence
    @property
    def facts(self) -> List[Fact]:
        """Every fact on the timeline, lazily loaded."""
        if self._facts is None:
            if self.store.config.facts_path.exists():
                payload = self.store.read_json(self.store.config.facts_path)
                self._facts = [Fact.from_dict(item) for item in payload.get("facts", [])]
            else:
                self._facts = []
        return self._facts

    def save(self) -> None:
        """Persist the timeline atomically, ordered by key then date."""
        ordered = sorted(self.facts, key=lambda f: (f.key, timestamp_sort_key(f.timestamp), f.fact_id))
        self.store.write_json(
            self.store.config.facts_path,
            {"schema_version": 1, "updated_at": utc_now_iso(), "facts": [f.to_dict() for f in ordered]},
        )

    def reload(self) -> None:
        """Drop the cached timeline so the next read comes from disk.

        Needed by long-lived processes that share a store with other writers.
        """
        self._facts = None

    def add_pattern(self, pattern: FactPattern) -> None:
        """Register an extra extraction rule (e.g. for a new domain)."""
        self.patterns.append(pattern)

    # -------------------------------------------------------------- recording
    def record_fact(
        self,
        subject: str,
        attribute: str,
        value: str,
        *,
        timestamp: str,
        source_leaf_id: Optional[str] = None,
        domain: str = "general",
        note: str = "",
    ) -> Fact:
        """Add an assertion to the timeline and re-apply recency weighting.

        Args:
            subject: Who/what the assertion is about (``"user"``, a patient...).
            attribute: The attribute being asserted (``"location"``...).
            value: The asserted value.
            timestamp: When the assertion was made or was true (ISO-8601).
            source_leaf_id: Leaf this came from, if any.
            domain: Domain of the source material.
            note: Free-form annotation.

        Returns:
            The stored :class:`~ihmt.models.Fact`, already resolved against the
            rest of its timeline.
        """
        fact = Fact(
            subject=subject.strip() or "user",
            attribute=attribute.strip(),
            value=truncate(value.strip(), 120),
            timestamp=timestamp or utc_now_iso(),
            source_leaf_id=source_leaf_id,
            domain=domain,
            valid_from=timestamp or utc_now_iso(),
        )
        fact.fact_id = f"F-{short_hash(f'{fact.key}|{normalize(fact.value)}|{fact.timestamp}', 10)}"

        existing = {f.fact_id for f in self.facts}
        if fact.fact_id in existing:
            # Re-ingesting the same material must not duplicate the timeline.
            return next(f for f in self.facts if f.fact_id == fact.fact_id)

        self.facts.append(fact)
        self._resolve_key(fact.key)
        self.save()
        return fact

    def extract_from_leaf(self, leaf: MemoryLeaf) -> List[Fact]:
        """Harvest dated facts from a leaf using the registered patterns.

        The leaf's own timestamp dates every fact it yields, which is what makes
        recency weighting reflect when something was true rather than when it
        was ingested.
        """
        patient = PATIENT_RE.search(leaf.content)
        found: List[Fact] = []
        for rule in self.patterns:
            for value in rule.apply(leaf.content):
                subject = rule.subject
                if rule.attribute in CLINICAL_ATTRIBUTES:
                    subject = patient.group(1).strip() if patient else "patient"
                found.append(
                    self.record_fact(
                        subject,
                        rule.attribute,
                        value,
                        timestamp=leaf.timestamp,
                        source_leaf_id=leaf.leaf_id,
                        domain=leaf.domain,
                    )
                )
        return found

    # -------------------------------------------------------------- timelines
    def _resolve_key(self, key: str) -> None:
        """Apply recency weighting to one ``(subject, attribute)`` timeline.

        The newest assertion wins; everything older is demoted to
        ``HISTORICAL`` and given the interval during which it held.
        """
        timeline = sorted(
            (f for f in self.facts if f.key == key),
            key=lambda f: (timestamp_sort_key(f.timestamp), f.fact_id),
        )
        if not timeline:
            return

        newest = timeline[-1]
        for index, fact in enumerate(timeline):
            if fact is newest:
                fact.status = LeafStatus.ACTIVE
                fact.superseded_by = None
                fact.valid_to = None
            else:
                successor = timeline[index + 1]
                was_active = fact.status is LeafStatus.ACTIVE
                fact.status = LeafStatus.HISTORICAL
                fact.superseded_by = successor.fact_id
                fact.valid_to = successor.timestamp
                if self.auto_flag_leaves and was_active and normalize(fact.value) != normalize(newest.value):
                    self._flag_leaf(fact, newest)
            fact.valid_from = fact.valid_from or fact.timestamp

    def _flag_leaf(self, old: Fact, new: Fact) -> None:
        """Annotate the source leaf of a superseded fact.

        The leaf keeps ``status = ACTIVE``: what it says was true *on its own
        date*, and that remains a valid answer to a historical question. What
        changes is that it now carries the notice explaining it has been
        superseded, so a retrieval of it can never silently read as current.
        """
        if not old.source_leaf_id:
            return
        try:
            leaf = self.store.load_leaf(old.source_leaf_id)
        except NodeNotFoundError:
            return
        notices = list(leaf.extra.get("superseded_facts", []))
        record = {
            "attribute": old.attribute,
            "old_value": old.value,
            "new_value": new.value,
            "superseded_at": new.timestamp,
            "notice": Contradiction(old.subject, old.attribute, old, new).render_notice(),
        }
        if record not in notices:
            notices.append(record)
            leaf.extra["superseded_facts"] = notices
            self.store.save_leaf(leaf)

    def timeline(self, subject: str, attribute: str) -> List[Fact]:
        """Every value ``(subject, attribute)`` has held, oldest first."""
        key = f"{normalize(subject)}::{normalize(attribute)}"
        return sorted(
            (f for f in self.facts if f.key == key),
            key=lambda f: (timestamp_sort_key(f.timestamp), f.fact_id),
        )

    def active_state(self, subject: Optional[str] = None) -> Dict[str, Fact]:
        """Current value of every attribute, keyed by ``subject::attribute``."""
        wanted = normalize(subject) if subject else None
        return {
            fact.key: fact
            for fact in self.facts
            if fact.status is LeafStatus.ACTIVE and (wanted is None or normalize(fact.subject) == wanted)
        }

    def state_at(self, when: str, subject: Optional[str] = None) -> Dict[str, Fact]:
        """What was true at ``when`` — the timeline query.

        Args:
            when: ISO-8601 instant or date.
            subject: Restrict to one subject.

        Returns:
            The latest assertion of each attribute that was made at or before
            ``when``.
        """
        cutoff = timestamp_sort_key(when)
        wanted = normalize(subject) if subject else None
        snapshot: Dict[str, Fact] = {}
        for fact in sorted(self.facts, key=lambda f: timestamp_sort_key(f.timestamp)):
            if timestamp_sort_key(fact.timestamp) > cutoff:
                continue
            if wanted is not None and normalize(fact.subject) != wanted:
                continue
            snapshot[fact.key] = fact
        return snapshot

    # ------------------------------------------------------------- conflicts
    def detect_conflicts(self, *, subject: Optional[str] = None) -> List[Contradiction]:
        """Every attribute whose value has changed over time.

        Only genuine changes are reported: repeating the same value at a later
        date is a confirmation, not a contradiction.
        """
        by_key: Dict[str, List[Fact]] = {}
        for fact in self.facts:
            if subject and normalize(fact.subject) != normalize(subject):
                continue
            by_key.setdefault(fact.key, []).append(fact)

        conflicts: List[Contradiction] = []
        for timeline in by_key.values():
            ordered = sorted(timeline, key=lambda f: (timestamp_sort_key(f.timestamp), f.fact_id))
            newest = ordered[-1]
            previous = next(
                (f for f in reversed(ordered[:-1]) if normalize(f.value) != normalize(newest.value)),
                None,
            )
            if previous is not None:
                conflicts.append(
                    Contradiction(subject=newest.subject, attribute=newest.attribute, old=previous, new=newest)
                )
        return sorted(conflicts, key=lambda c: (c.subject, c.attribute))

    def notices_for_leaf(self, leaf: MemoryLeaf) -> List[str]:
        """Transparency notices attached to a retrieved leaf.

        Returned by the navigator alongside the leaf, so an answer built on
        outdated material always arrives with its correction.
        """
        notices = [str(item.get("notice", "")) for item in leaf.extra.get("superseded_facts", [])]
        for fact in self.facts:
            if fact.source_leaf_id != leaf.leaf_id or fact.status is not LeafStatus.HISTORICAL:
                continue
            current = self.active_state().get(fact.key)
            if current is None or normalize(current.value) == normalize(fact.value):
                continue
            notice = Contradiction(fact.subject, fact.attribute, fact, current).render_notice()
            if notice not in notices:
                notices.append(notice)
        return [n for n in notices if n]

    def report(self, *, subject: Optional[str] = None) -> Dict[str, Any]:
        """Machine-readable snapshot of state, history and contradictions."""
        conflicts = self.detect_conflicts(subject=subject)
        return {
            "active_state": {k: v.to_dict() for k, v in sorted(self.active_state(subject).items())},
            "conflicts": [c.to_dict() for c in conflicts],
            "notices": [c.render_notice() for c in conflicts],
            "fact_count": len(self.facts),
        }

    def rebuild(self) -> int:
        """Re-extract every fact from every leaf on disk.

        Use after editing extraction rules or restoring leaves from a backup.

        Returns:
            The number of facts on the timeline afterwards.
        """
        self._facts = []
        self.save()
        for leaf in self.store.iter_leaves():
            self.extract_from_leaf(leaf)
        return len(self.facts)


def summarize_notices(conflicts: Iterable[Contradiction], formatter: Optional[Callable[[Contradiction], str]] = None) -> List[str]:
    """Render a list of contradictions as user-facing notices."""
    render = formatter or (lambda c: c.render_notice())
    return [render(conflict) for conflict in conflicts]


__all__ = [
    "ConflictResolver",
    "FactPattern",
    "DEFAULT_PATTERNS",
    "summarize_notices",
]
