"""Summarization backends used to build every branch of the tree.

A backend turns *N* children (leaves, or lower branch nodes) into the compact
description that becomes their parent. Two are shipped:

* :class:`HeuristicSummarizer` — stdlib only, deterministic, offline. The
  default, and what the test suite asserts against.
* :class:`AnthropicSummarizer` — optional. Used only when explicitly selected
  *and* the ``anthropic`` package plus an API key are available; any failure
  degrades to the heuristic backend rather than breaking a consolidation.

Implementing :class:`SummarizerBackend` is all that is needed to plug in a
different model or a local runtime.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence, runtime_checkable

from .config import IHMTConfig
from .textutils import (
    dedupe,
    extract_entities,
    extract_keywords,
    summarize_extractive,
    truncate,
)


@dataclass
class SummaryInput:
    """One child as seen by a summarizer."""

    id: str
    title: str
    text: str
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    timestamp: str = ""

    def as_prompt_line(self, limit: int = 400) -> str:
        return f"- [{self.timestamp[:10] or '????'}] {self.title}: {truncate(self.text, limit)}"


@dataclass
class NodeSummary:
    """What a backend must produce for a branch node."""

    title: str
    summary: str
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    backend: str = "heuristic"


@runtime_checkable
class SummarizerBackend(Protocol):
    """Interface every summarization backend implements."""

    name: str

    def summarize(
        self,
        children: Sequence[SummaryInput],
        *,
        domain: str,
        layer: int,
    ) -> NodeSummary:
        """Condense ``children`` into the description of their parent node."""
        ...


class HeuristicSummarizer:
    """Offline extractive summarizer.

    Ranks terms by frequency (minus stop words) and selects the most
    representative sentences across the children. Fully deterministic: the same
    children always produce the same node, which is what makes the tree shape
    reproducible and testable.
    """

    name = "heuristic"

    def __init__(self, config: Optional[IHMTConfig] = None) -> None:
        self.config = config

    def summarize(
        self,
        children: Sequence[SummaryInput],
        *,
        domain: str,
        layer: int,
    ) -> NodeSummary:
        """See :class:`SummarizerBackend`."""
        if not children:
            return NodeSummary(title=f"{domain} (empty)", summary="", backend=self.name)

        corpus = "\n".join(f"{c.title}. {c.text}" for c in children)
        is_code = domain.startswith("software") or any("lang:" in t for c in children for t in c.tags)

        inherited = [k for child in children for k in child.keywords]
        keywords = dedupe(
            inherited + extract_keywords(corpus, top_k=18, code=is_code, boost=[c.title for c in children]),
            limit=14,
        )
        entities = dedupe(
            [e for child in children for e in extract_entities(child.title, limit=3)]
            + extract_entities(corpus, limit=14),
            limit=12,
        )
        tags = dedupe([t for child in children for t in child.tags], limit=12)

        timestamps = sorted(t[:10] for t in (c.timestamp for c in children) if t)
        period = ""
        if timestamps:
            period = timestamps[0] if timestamps[0] == timestamps[-1] else f"{timestamps[0]}..{timestamps[-1]}"

        body = summarize_extractive(
            " ".join(f"{c.title}: {truncate(c.text, 300)}" for c in children),
            max_sentences=4,
            max_chars=700,
        )
        headline = (
            f"[{domain} · layer {layer} · {len(children)} children"
            + (f" · {period}" if period else "")
            + "] "
        )
        topics = ", ".join(dedupe([c.title for c in children], limit=6))
        summary = f"{headline}Covers: {topics}. {body}".strip()

        return NodeSummary(
            title=self._title(domain, layer, entities, keywords, period),
            summary=truncate(summary, 900),
            keywords=keywords,
            entities=entities,
            tags=tags,
            backend=self.name,
        )

    @staticmethod
    def _title(domain: str, layer: int, entities: Sequence[str], keywords: Sequence[str], period: str) -> str:
        """Short, human-scannable label for a branch."""
        focus = ", ".join(list(entities)[:2] or list(keywords)[:3]) or "misc"
        suffix = f" [{period}]" if period else ""
        return truncate(f"L{layer} {domain} · {focus}{suffix}", 90)


class AnthropicSummarizer:
    """Optional LLM-backed summarizer (Claude via the ``anthropic`` SDK).

    Selected with ``summarizer_backend = "anthropic"`` in the store config, and
    only if the SDK is importable and ``ANTHROPIC_API_KEY`` is set. Every
    failure path — missing SDK, missing key, network error, unparseable
    response — silently falls back to :class:`HeuristicSummarizer`, so a
    consolidation can never be lost because a model was unreachable.
    """

    name = "anthropic"

    #: Response contract requested from the model.
    PROMPT = (
        "You are the summarization layer of a hierarchical memory tree.\n"
        "Condense the child nodes below into their parent node.\n"
        "Domain: {domain}. Tree layer: {layer}.\n\n"
        "Children:\n{children}\n\n"
        "Reply with ONLY a JSON object with these keys:\n"
        '{{"title": "<= 90 chars", "summary": "<= 700 chars, factual, no preamble", '
        '"keywords": ["<= 14 lowercase terms"], "entities": ["<= 12 proper nouns or identifiers"]}}'
    )

    def __init__(self, config: IHMTConfig, *, api_key: Optional[str] = None) -> None:
        self.config = config
        self.model = config.summarizer_model
        self.fallback = HeuristicSummarizer(config)
        self._client = self._build_client(api_key)

    @staticmethod
    def _build_client(api_key: Optional[str]):  # type: ignore[no-untyped-def]
        """Instantiate the SDK client, or ``None`` if unavailable."""
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            return anthropic.Anthropic(api_key=key)
        except Exception:  # pragma: no cover - defensive
            return None

    @property
    def available(self) -> bool:
        """Whether the LLM path is usable right now."""
        return self._client is not None

    def summarize(
        self,
        children: Sequence[SummaryInput],
        *,
        domain: str,
        layer: int,
    ) -> NodeSummary:
        """See :class:`SummarizerBackend`. Falls back on any failure."""
        if not self.available or not children:
            return self.fallback.summarize(children, domain=domain, layer=layer)

        prompt = self.PROMPT.format(
            domain=domain,
            layer=layer,
            children="\n".join(child.as_prompt_line() for child in children),
        )
        try:
            response = self._client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(block, "text", "") for block in response.content)
            payload = self._parse_json(text)
        except Exception:
            return self.fallback.summarize(children, domain=domain, layer=layer)

        if not payload:
            return self.fallback.summarize(children, domain=domain, layer=layer)

        base = self.fallback.summarize(children, domain=domain, layer=layer)
        return NodeSummary(
            title=truncate(str(payload.get("title") or base.title), 90),
            summary=truncate(str(payload.get("summary") or base.summary), 900),
            keywords=dedupe([str(k) for k in payload.get("keywords", [])] or base.keywords, limit=14),
            entities=dedupe([str(e) for e in payload.get("entities", [])] or base.entities, limit=12),
            tags=base.tags,
            backend=self.name,
        )

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, object]]:
        """Extract the JSON object from a model reply, tolerating code fences."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def get_summarizer(config: IHMTConfig) -> SummarizerBackend:
    """Instantiate the backend named by ``config.summarizer_backend``.

    An unknown or unavailable backend resolves to :class:`HeuristicSummarizer`,
    keeping the framework usable with zero dependencies and no credentials.
    """
    if config.summarizer_backend == "anthropic":
        backend = AnthropicSummarizer(config)
        return backend if backend.available else backend.fallback
    return HeuristicSummarizer(config)
