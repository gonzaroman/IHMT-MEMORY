"""Tree-walking retrieval: root → branch → ... → leaf.

The navigator never scans the store. It opens ``root.json``, ranks the domains,
then descends one layer at a time keeping only the ``beam_width`` best branches
alive. Because every parent embeds its children's titles, keywords and
excerpts, ranking a level costs no extra file reads — the number of files opened
for a query is ``beam_width x depth``, i.e. logarithmic in the number of leaves
rather than linear.

When the descent cannot separate the candidates — the classic "which Luis?"
case — the navigator refuses to guess and returns a :class:`ClueRequest`.
Supplying a clue re-runs the query as a **joint cross-reference**
(``Luis`` + ``vacaciones en Benidorm``): branches matching both term groups are
boosted, branches matching only one are demoted.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import IHMTConfig
from .models import ChildRef, MemoryLeaf, NodeKind, NodeNotFoundError
from .storage import MemoryStore
from .textutils import content_terms, dedupe, normalize, truncate

#: Depth guard for a pathological tree.
MAX_DESCENT = 32

#: Search scopes for code: production sources, tests, or everything.
SCOPES = ("all", "main", "test")

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)(/|$)"
    r"|(Test|Tests|IT|Spec)\.(java|kt|scala|groovy|cs|swift)$"
    r"|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
    r"|\.(test|spec)\.[jt]sx?$"
)
_CODE_SUFFIX_RE = re.compile(r"\.(py|pyi|java|js|mjs|jsx|ts|tsx|c|h|cc|cpp|cxx|hpp|hh|cs|go|rs|kt|swift|php|scala)$")


def looks_like_test(source: str) -> bool:
    """Heuristic: does ``source`` (a file path) hold tests rather than product code?"""
    return bool(source) and bool(_TEST_PATH_RE.search(source.replace("\\", "/")))


def looks_like_code(source: str) -> bool:
    """Whether ``source`` is a source-code file path."""
    return bool(source) and bool(_CODE_SUFFIX_RE.search(source.lower()))


@dataclass
class ScoredCandidate:
    """A branch or leaf reference with its ranking score."""

    ref: ChildRef
    score: float
    coverage: float
    groups_matched: int = 0

    @property
    def id(self) -> str:
        return self.ref.id


@dataclass
class SearchResult:
    """One retrieved leaf."""

    leaf_id: str
    title: str
    domain: str
    score: float
    excerpt: str
    path: List[str] = field(default_factory=list)
    timestamp: str = ""
    tags: List[str] = field(default_factory=list)
    status: str = "ACTIVE"
    content: str = ""
    notices: List[str] = field(default_factory=list)
    file: str = ""

    def to_dict(self, *, include_content: bool = False) -> Dict[str, Any]:
        data = {
            "leaf_id": self.leaf_id,
            "title": self.title,
            "domain": self.domain,
            "score": round(self.score, 4),
            "excerpt": self.excerpt,
            "path": self.path,
            "timestamp": self.timestamp,
            "tags": self.tags,
            "status": self.status,
            "notices": self.notices,
            "file": self.file,
        }
        if include_content:
            data["content"] = self.content
        return data


@dataclass
class ClueOption:
    """One of the competing branches offered to the user for disambiguation."""

    id: str
    title: str
    domain: str
    score: float
    hint: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "domain": self.domain,
            "score": round(self.score, 4),
            "hint": self.hint,
        }


@dataclass
class ClueRequest:
    """A request for a clarifying clue, raised instead of a confident guess."""

    query: str
    reason: str
    options: List[ClueOption] = field(default_factory=list)
    confidence: float = 0.0

    def prompt(self) -> str:
        """Human-readable question to put to the user."""
        lines = [
            f'"{self.query}" is ambiguous ({self.reason}, confidence {self.confidence:.2f}).',
            "It could belong to any of these branches:",
        ]
        for index, option in enumerate(self.options, start=1):
            lines.append(f"  {index}. [{option.domain}] {option.title} — {option.hint}")
        lines.append("Give me a clue to narrow it down (e.g. a place, a date, a project):")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
            "options": [o.to_dict() for o in self.options],
        }


@dataclass
class SearchResponse:
    """Everything a query produced, including how it got there."""

    query: str
    results: List[SearchResult] = field(default_factory=list)
    confidence: float = 0.0
    ambiguous: bool = False
    clue_request: Optional[ClueRequest] = None
    node_reads: int = 0
    leaf_reads: int = 0
    depth: int = 0
    clues_used: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def needs_clue(self) -> bool:
        """Whether the caller should ask the user for a clue."""
        return self.clue_request is not None

    @property
    def best(self) -> Optional[SearchResult]:
        return self.results[0] if self.results else None

    def to_dict(self, *, include_content: bool = False) -> Dict[str, Any]:
        return {
            "query": self.query,
            "confidence": round(self.confidence, 4),
            "ambiguous": self.ambiguous,
            "clues_used": self.clues_used,
            "node_reads": self.node_reads,
            "leaf_reads": self.leaf_reads,
            "depth": self.depth,
            "clue_request": self.clue_request.to_dict() if self.clue_request else None,
            "results": [r.to_dict(include_content=include_content) for r in self.results],
            "trace": self.trace,
        }


class BM25Ranker:
    """Okapi BM25 over the candidates of a single tree level.

    Document frequencies are computed across the siblings being compared, which
    is exactly the discrimination the descent needs — and, unlike a global
    index, costs no extra reads.
    """

    K1 = 1.5
    B = 0.75

    #: Field weights: a hit in a title or keyword means more than one in prose.
    WEIGHTS = {"title": 3, "keywords": 3, "tags": 2, "excerpt": 1}

    def __init__(self, documents: Sequence[Tuple[str, List[str]]]) -> None:
        """
        Args:
            documents: ``(doc_id, terms)`` pairs; terms may repeat to encode
                field weighting.
        """
        self.doc_terms: Dict[str, List[str]] = {doc_id: terms for doc_id, terms in documents}
        self.doc_freq: Dict[str, int] = defaultdict(int)
        for terms in self.doc_terms.values():
            for term in set(terms):
                self.doc_freq[term] += 1
        lengths = [len(t) for t in self.doc_terms.values()] or [1]
        self.avg_length = sum(lengths) / len(lengths)
        self.total = max(1, len(self.doc_terms))

    def idf(self, term: str) -> float:
        """Inverse document frequency, floored at zero."""
        df = self.doc_freq.get(term, 0)
        return max(0.0, math.log(1.0 + (self.total - df + 0.5) / (df + 0.5)))

    def score(self, doc_id: str, terms: Sequence[str]) -> float:
        """BM25 score of one document against ``terms``."""
        doc = self.doc_terms.get(doc_id, [])
        if not doc or not terms:
            return 0.0
        length = len(doc)
        counts: Dict[str, int] = defaultdict(int)
        for term in doc:
            counts[term] += 1
        total = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            denominator = frequency + self.K1 * (1 - self.B + self.B * length / self.avg_length)
            total += self.idf(term) * (frequency * (self.K1 + 1)) / denominator
        return total

    def coverage(self, doc_id: str, terms: Sequence[str]) -> float:
        """Fraction of distinct query terms present in the document."""
        unique = set(terms)
        if not unique:
            return 0.0
        present = set(self.doc_terms.get(doc_id, []))
        return len(unique & present) / len(unique)

    @staticmethod
    def weighted_terms(title: str, keywords: Sequence[str], tags: Sequence[str], body: str) -> List[str]:
        """Flatten a candidate's fields into a weighted bag of terms."""
        terms: List[str] = []
        terms += content_terms(title) * BM25Ranker.WEIGHTS["title"]
        terms += [normalize(k) for k in keywords] * BM25Ranker.WEIGHTS["keywords"]
        for tag in tags:
            terms += content_terms(tag.replace(":", " ")) * BM25Ranker.WEIGHTS["tags"]
        terms += content_terms(body) * BM25Ranker.WEIGHTS["excerpt"]
        return terms


class SemanticNavigator:
    """Root-to-leaf pathfinder with an interactive disambiguation loop."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        conflict_resolver: Optional[Any] = None,
        config: Optional[IHMTConfig] = None,
    ) -> None:
        """
        Args:
            store: Store to search.
            conflict_resolver: Optional resolver; when present, results carry
                timeline notices (e.g. "superseded in 2026").
            config: Overrides the store configuration (beam width, thresholds).
        """
        self.store = store
        self.config = config or store.config
        self.conflict_resolver = conflict_resolver

    # ------------------------------------------------------------------- api
    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        clue: Optional[str] = None,
        include_content: bool = True,
        scope: str = "all",
        path_prefix: Optional[str] = None,
        include_historical: bool = False,
    ) -> SearchResponse:
        """Walk the tree and return the best-matching leaves.

        Args:
            query: Natural-language or keyword query.
            top_k: Maximum number of leaves returned.
            clue: Optional clarifying clue, cross-referenced with ``query``.
            include_content: Load full leaf content for the returned results.
            scope: ``"all"``, ``"main"`` (skip test sources) or ``"test"``.
            path_prefix: Only leaves whose source starts with this prefix.
            include_historical: Also return leaves demoted to ``HISTORICAL``
                (earlier versions of a re-ingested file).

        Filters are evaluated against the in-memory catalog, so they prune
        candidates without opening a single extra file.

        Returns:
            A :class:`SearchResponse`. If the descent is ambiguous, its
            ``clue_request`` is set and the caller should ask the user.
        """
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
        groups = self._term_groups(query, clue)
        response = SearchResponse(query=query, clues_used=[clue] if clue else [])
        flat_terms = {term for group in groups for term in group}
        if not any(groups):
            return response

        # ---- level 0: the trunk -----------------------------------------
        root = self.store.load_root()
        response.node_reads += 1
        frontier = self._select_domains(root, groups, response)
        if not frontier:
            return response

        # ---- descent ------------------------------------------------------
        leaf_pool: Dict[str, ScoredCandidate] = {}
        parents: Dict[str, str] = {}
        level = 0
        keep_leaves = max(self.config.beam_width, top_k)

        while frontier and level < MAX_DESCENT:
            ranked = self._rank(frontier, groups)
            leaves = [
                self._adjust_for_code(c, flat_terms, scope)
                for c in ranked
                if c.ref.kind is NodeKind.LEAF and self._admissible(c.ref, scope, path_prefix, include_historical)
            ]
            leaves = sorted(leaves, key=lambda c: (-c.score, c.id))[:keep_leaves]
            nodes = [c for c in ranked if c.ref.kind is NodeKind.NODE][: self.config.beam_width]

            for candidate in leaves:
                if candidate.score <= 0.0:
                    # Not a match at all: no query term reached this leaf's
                    # title, keywords, tags or excerpt. Returning it would turn
                    # "nothing is stored about this" into a confident-looking
                    # wrong answer, or into a pointless disambiguation prompt.
                    continue
                previous = leaf_pool.get(candidate.id)
                if previous is None or candidate.score > previous.score:
                    leaf_pool[candidate.id] = candidate

            response.trace.append(
                {
                    "level": level,
                    "considered": len(frontier),
                    "expanded": [c.id for c in nodes],
                    "top": [(c.id, round(c.score, 3)) for c in ranked[:3]],
                }
            )
            if not nodes:
                break

            next_frontier: List[ChildRef] = []
            for candidate in nodes:
                try:
                    node = self.store.load_node(candidate.id)
                except NodeNotFoundError:
                    continue
                response.node_reads += 1
                for child in node.children:
                    parents[child.id] = node.node_id
                    next_frontier.append(child)
            frontier = next_frontier
            level += 1

        response.depth = level
        ordered = sorted(leaf_pool.values(), key=lambda c: (-c.score, c.id))[:top_k]
        response.results = [self._materialize(c, parents, include_content, response) for c in ordered]

        # ---- confidence & ambiguity ---------------------------------------
        response.confidence = self._confidence(ordered)
        clue_request = self._maybe_clue_request(
            query,
            ordered,
            response.confidence,
            query_terms=[term for group in groups for term in group],
        )
        if clue_request is not None:
            response.ambiguous = True
            response.clue_request = clue_request
        return response

    def cross_reference(self, query: str, clue: str, *, top_k: int = 5) -> SearchResponse:
        """Joint query: ``query`` AND ``clue``.

        Candidates matching both term groups are boosted; those matching only
        one are demoted. This is what turns "Luis" plus "vacaciones en
        Benidorm" into a single unambiguous branch.
        """
        response = self.search(query, top_k=top_k, clue=clue)
        response.query = f"{query} + {clue}"
        return response

    def search_interactive(
        self,
        query: str,
        clue_provider: Callable[[ClueRequest], Optional[str]],
        *,
        top_k: int = 5,
        max_rounds: Optional[int] = None,
    ) -> SearchResponse:
        """Run the **Interactive Clue Loop**.

        Searches; while the result is ambiguous, asks ``clue_provider`` for a
        clarifying clue and re-runs the query as a cross-reference. Stops when
        the answer is confident, when the provider declines (returns ``None`` or
        an empty string), or after ``max_rounds`` rounds.

        Args:
            query: Original query.
            clue_provider: Callable receiving the :class:`ClueRequest` and
                returning a clue. In the CLI this wraps :func:`input`; an agent
                can supply the LLM's own follow-up instead.
            top_k: Maximum number of leaves returned.
            max_rounds: Overrides ``config.max_clue_rounds``.

        Returns:
            The final :class:`SearchResponse`. If it is still ambiguous, its
            ``clue_request`` is preserved so the caller can report honestly.
        """
        rounds = self.config.max_clue_rounds if max_rounds is None else max_rounds
        response = self.search(query, top_k=top_k)
        clues: List[str] = []

        for _ in range(max(0, rounds)):
            if not response.needs_clue:
                break
            clue = clue_provider(response.clue_request)  # type: ignore[arg-type]
            if not clue or not clue.strip():
                break
            if normalize(clue) in {normalize(c) for c in clues}:
                # The same clue twice cannot narrow anything further; asking
                # again would only loop.
                break
            clues.append(clue.strip())
            response = self.search(query, top_k=top_k, clue=" ".join(clues))
            response.query = f"{query} + {' + '.join(clues)}"
            response.clues_used = list(clues)
        return response

    # -------------------------------------------------------------- internals
    @staticmethod
    def _term_groups(query: str, clue: Optional[str]) -> List[List[str]]:
        """Split a query (and optional clue) into term groups to cross-match."""
        groups = [dedupe(content_terms(query))]
        if clue and clue.strip():
            clue_terms = dedupe(content_terms(clue))
            if clue_terms:
                groups.append(clue_terms)
        return [g for g in groups if g]

    def _select_domains(
        self,
        root: Any,
        groups: List[List[str]],
        response: SearchResponse,
    ) -> List[ChildRef]:
        """Rank the trunk's domains and return the children to descend into."""
        entries = list(root.domains.values())
        if not entries:
            return []

        documents = [
            (
                entry.domain,
                BM25Ranker.weighted_terms(entry.domain.replace(".", " "), entry.keywords, [], entry.summary),
            )
            for entry in entries
        ]
        ranker = BM25Ranker(documents)
        flat = [term for group in groups for term in group]
        scored = sorted(
            ((entry, ranker.score(entry.domain, flat)) for entry in entries),
            key=lambda pair: (-pair[1], pair[0].domain),
        )
        response.trace.append(
            {
                "level": "root",
                "considered": len(entries),
                "top": [(entry.domain, round(score, 3)) for entry, score in scored[:4]],
            }
        )

        # Domain scores only *order* the search: a query whose words never
        # appear in a domain digest may still match a leaf inside it, so every
        # domain stays reachable. The beam is applied one level down.
        limit = max(self.config.beam_width, 2)
        selected = [entry for entry, score in scored if score > 0][:limit] or entries
        refs: List[ChildRef] = []
        for entry in selected:
            refs.extend(entry.top_nodes)
        return refs

    def _rank(self, refs: Sequence[ChildRef], groups: List[List[str]]) -> List[ScoredCandidate]:
        """Score one level of candidates, applying the cross-reference rules."""
        unique: Dict[str, ChildRef] = {}
        for ref in refs:
            unique.setdefault(ref.id, ref)

        documents = [
            (
                ref.id,
                BM25Ranker.weighted_terms(ref.title, ref.keywords, ref.tags, ref.excerpt),
            )
            for ref in unique.values()
        ]
        ranker = BM25Ranker(documents)
        flat = [term for group in groups for term in group]

        candidates: List[ScoredCandidate] = []
        for ref in unique.values():
            per_group = [ranker.score(ref.id, group) for group in groups]
            matched = sum(1 for value in per_group if value > 0)
            score = sum(per_group)
            if len(groups) > 1:
                # Joint cross-reference: reward the intersection, demote a
                # candidate that only satisfies one side of the query.
                score *= 1.6 if matched == len(groups) else 0.7
            candidates.append(
                ScoredCandidate(
                    ref=ref,
                    score=score,
                    coverage=ranker.coverage(ref.id, flat),
                    groups_matched=matched,
                )
            )
        return sorted(candidates, key=lambda c: (-c.score, c.id))

    def _admissible(
        self, ref: ChildRef, scope: str, path_prefix: Optional[str], include_historical: bool
    ) -> bool:
        """Apply the search filters to a leaf, using only the catalog."""
        entry = self.store.catalog.entries.get(ref.id)
        if entry is None:
            # Not indexed (stale catalog): only a filter we cannot evaluate excludes it.
            return scope == "all" and not path_prefix
        if not include_historical and entry.status == "HISTORICAL":
            return False
        if path_prefix and not entry.source.startswith(path_prefix):
            return False
        if scope != "all" and looks_like_code(entry.source):
            if looks_like_test(entry.source) != (scope == "test"):
                return False
        return True

    def _adjust_for_code(self, candidate: ScoredCandidate, query_terms: set, scope: str) -> ScoredCandidate:
        """Code-aware re-weighting of a leaf candidate.

        BM25 over titles and keywords confuses classes with similar names. Two
        cheap signals from the catalog fix most of it: a query that names the
        file («AnadirLineaService») should prefer that file, and a test should
        not outrank the class it tests unless the query asks about tests.
        """
        entry = self.store.catalog.entries.get(candidate.ref.id)
        if entry is None or not looks_like_code(entry.source) or candidate.score <= 0:
            return candidate
        stem = entry.source.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stem_terms = set(content_terms(stem))
        factor = 1.0
        if stem_terms and query_terms:
            # Reward the share of the query that the file name explains, so
            # "PedidoJpaMapper …" prefers PedidoJpaMapper.java over Pedido.java.
            factor *= 1.0 + 0.8 * len(stem_terms & query_terms) / max(len(stem_terms), len(query_terms))
        if scope == "all" and looks_like_test(entry.source) and not ({"test", "tests"} & query_terms):
            factor *= 0.6
        if entry.title.endswith("· imports") or "· imports →" in entry.title:
            # Import lines name half the codebase; they are rarely the answer.
            factor *= 0.3
        if factor != 1.0:
            candidate.score *= factor
        return candidate

    def _materialize(
        self,
        candidate: ScoredCandidate,
        parents: Dict[str, str],
        include_content: bool,
        response: SearchResponse,
    ) -> SearchResult:
        """Turn a winning candidate into a full result, reading the leaf file."""
        ref = candidate.ref
        leaf: Optional[MemoryLeaf] = None
        if include_content:
            try:
                leaf = self.store.load_leaf(ref.id)
                response.leaf_reads += 1
            except NodeNotFoundError:
                leaf = None

        result = SearchResult(
            leaf_id=ref.id,
            title=leaf.title if leaf else ref.title,
            domain=leaf.domain if leaf else "",
            score=candidate.score,
            excerpt=ref.excerpt or (leaf.excerpt() if leaf else ""),
            path=self._path_for(ref.id, parents),
            timestamp=leaf.timestamp if leaf else ref.timestamp,
            tags=list(leaf.tags) if leaf else list(ref.tags),
            status=leaf.status.value if leaf else "ACTIVE",
            content=leaf.content if leaf else "",
            file=ref.path,
        )
        if leaf is not None and self.conflict_resolver is not None:
            result.notices = self.conflict_resolver.notices_for_leaf(leaf)
        return result

    @staticmethod
    def _path_for(leaf_id: str, parents: Dict[str, str]) -> List[str]:
        """Reconstruct ``root → ... → leaf`` from the descent's parent links."""
        chain = [leaf_id]
        seen = {leaf_id}
        cursor = parents.get(leaf_id)
        while cursor and cursor not in seen:
            chain.append(cursor)
            seen.add(cursor)
            cursor = parents.get(cursor)
        chain.append("root")
        return list(reversed(chain))

    @staticmethod
    def _confidence(candidates: Sequence[ScoredCandidate]) -> float:
        """Blend query coverage with the margin over the runner-up.

        Coverage answers "does this leaf actually contain what was asked?";
        the margin answers "is it distinguishable from its rivals?". A common
        first name scores high on the first and near zero on the second, which
        is precisely the situation that must trigger a clue request.
        """
        if not candidates:
            return 0.0
        best = candidates[0]
        if best.score <= 0:
            return 0.0
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        margin = (best.score - runner_up) / best.score
        return round(min(1.0, 0.6 * best.coverage + 0.4 * margin), 4)

    def _maybe_clue_request(
        self,
        query: str,
        candidates: Sequence[ScoredCandidate],
        confidence: float,
        *,
        query_terms: Sequence[str] = (),
    ) -> Optional[ClueRequest]:
        """Decide whether to ask the user instead of answering.

        Three triggers, any of which is enough:

        * overall confidence below the configured threshold;
        * near-tied candidates sitting in *different* branches or domains;
        * near-tied candidates under an *underspecified* query — one or two
          terms, such as a bare first name. Several memories mentioning "Luis"
          score identically, and picking one at random would be a fabrication.
        """
        if not candidates:
            return None
        best = candidates[0]
        rivals = [
            c
            for c in candidates[1:]
            if best.score > 0 and c.score >= best.score * (1 - self.config.ambiguity_margin)
        ]
        distinct_branches = len({self._branch_of(c.ref) for c in [best, *rivals]})
        underspecified = len(set(query_terms)) <= 2

        reasons: List[str] = []
        if confidence < self.config.confidence_threshold:
            reasons.append("low confidence")
        if rivals and distinct_branches > 1:
            reasons.append("several branches match equally well")
        elif rivals and underspecified:
            reasons.append(f"{len(rivals) + 1} memories match this query equally well")
        if not reasons:
            return None
        reason = "; ".join(reasons)

        options = [
            ClueOption(
                id=c.ref.id,
                title=truncate(c.ref.title, 80),
                domain=self._branch_of(c.ref),
                score=c.score,
                hint=truncate(c.ref.excerpt, 110),
            )
            for c in list(candidates)[:4]
        ]
        return ClueRequest(query=query, reason=reason, options=options, confidence=confidence)

    @staticmethod
    def _branch_of(ref: ChildRef) -> str:
        """Domain a candidate belongs to, read from its tags or its path."""
        for tag in ref.tags:
            if tag.startswith("domain:"):
                return tag.split(":", 1)[1]
        parts = ref.path.split("/")
        return parts[1] if len(parts) > 1 and parts[0] == "layer_0" else (parts[0] if parts else "?")

    # ------------------------------------------------------------- utilities
    def get_leaf(self, leaf_id: str) -> MemoryLeaf:
        """Fetch one leaf by identifier."""
        return self.store.load_leaf(leaf_id)

    def outline(self, *, max_depth: int = 2, max_children: int = 6) -> List[str]:
        """Render a readable outline of the tree for inspection.

        Args:
            max_depth: How many branch levels to expand below each domain.
            max_children: Maximum children listed per node.
        """
        root = self.store.load_root()
        lines = [f"root.json — {root.leaf_count} leaves / {root.node_count} nodes / depth {root.depth}"]
        for domain, entry in sorted(root.domains.items()):
            lines.append(f"├─ [{domain}] {entry.leaf_count} leaves · {truncate(entry.summary, 90)}")
            for ref in entry.top_nodes[:max_children]:
                lines.extend(self._outline_ref(ref, depth=1, max_depth=max_depth, max_children=max_children))
        return lines

    def _outline_ref(self, ref: ChildRef, *, depth: int, max_depth: int, max_children: int) -> List[str]:
        indent = "│  " * depth
        label = f"{indent}├─ {ref.id} · {truncate(ref.title, 70)}"
        if ref.kind is NodeKind.LEAF or depth >= max_depth:
            return [label]
        try:
            node = self.store.load_node(ref.id)
        except NodeNotFoundError:
            return [label]
        lines = [label]
        for child in node.children[:max_children]:
            lines.extend(
                self._outline_ref(child, depth=depth + 1, max_depth=max_depth, max_children=max_children)
            )
        return lines
