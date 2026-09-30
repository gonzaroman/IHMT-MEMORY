"""Core data model of the Infinite Hierarchical Memory Tree.

Three node kinds make up the tree:

* :class:`MemoryLeaf` — layer 0. A raw UTF-8 text chunk carrying a strict JSON
  metadata header, stored as ``.txt``.
* :class:`BranchNode` — layers 1..N. A JSON summary of ``branch_factor``
  children (leaves, or lower branch nodes).
* :class:`RootIndex` — the trunk (``root.json``). The single entry point of
  every retrieval.

:class:`Fact` and :class:`Contradiction` back the timeline layer used by the
conflict resolver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import SCHEMA_VERSION
from .textutils import truncate, utc_now_iso

#: Delimiters framing the JSON metadata header of every ``.txt`` leaf.
META_OPEN = "<<<IHMT-META"
META_CLOSE = "IHMT-META>>>"


class DataType(str, Enum):
    """Nature of the ingested material, decided by the domain detector."""

    CODE = "CODE"
    NARRATIVE = "NARRATIVE"
    CLINICAL = "CLINICAL"
    PERSONAL = "PERSONAL"
    PROCESS = "PROCESS"
    GENERIC = "GENERIC"

    @classmethod
    def coerce(cls, value: "str | DataType | None") -> "DataType":
        """Convert a string to a member, defaulting to :attr:`GENERIC`."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return cls.GENERIC


class NodeKind(str, Enum):
    """Discriminator used inside :class:`ChildRef` and the catalog."""

    LEAF = "leaf"
    NODE = "node"


class LeafStatus(str, Enum):
    """Timeline status of a leaf or fact.

    ``HISTORICAL`` material is never deleted: it is demoted so that the past
    stays queryable while the present stays unambiguous.
    """

    ACTIVE = "ACTIVE"
    HISTORICAL = "HISTORICAL"


class IHMTError(Exception):
    """Base class for every error raised by the framework."""


class StoreNotInitializedError(IHMTError):
    """Raised when an operation targets a directory with no IHMT store."""


class NodeNotFoundError(IHMTError):
    """Raised when an identifier does not resolve to a node on disk."""


class MalformedLeafError(IHMTError):
    """Raised when a ``.txt`` leaf is missing or has a corrupt JSON header."""


@dataclass
class Span:
    """Inclusive 1-based line range of a chunk within its source document."""

    start_line: int = 0
    end_line: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {"start_line": self.start_line, "end_line": self.end_line}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Span":
        data = data or {}
        return cls(int(data.get("start_line", 0)), int(data.get("end_line", 0)))


@dataclass
class MemoryLeaf:
    """A layer-0 memory: raw text plus the metadata that makes it findable.

    The metadata is written into the ``.txt`` file itself, so a leaf remains
    self-describing even if the catalog is lost or the file is moved.
    """

    leaf_id: str
    domain: str
    data_type: DataType = DataType.GENERIC
    title: str = ""
    content: str = ""
    source: str = ""
    timestamp: str = field(default_factory=utc_now_iso)
    ingested_at: str = field(default_factory=utc_now_iso)
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    span: Span = field(default_factory=Span)
    order_index: int = 0
    token_estimate: int = 0
    checksum: str = ""
    oversized: bool = False
    status: LeafStatus = LeafStatus.ACTIVE
    superseded_by: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------ views
    @property
    def kind(self) -> NodeKind:
        return NodeKind.LEAF

    @property
    def layer(self) -> int:
        return 0

    def excerpt(self, limit: int = 320) -> str:
        """Single-line preview used inside parent nodes and search results."""
        return truncate(self.content, limit)

    def searchable_text(self) -> str:
        """Concatenation of every field the navigator scores a leaf on."""
        return " ".join(
            [self.title, " ".join(self.tags), " ".join(self.keywords), " ".join(self.entities), self.content]
        )

    # ------------------------------------------------------------ (de)serial
    def header_dict(self) -> Dict[str, Any]:
        """The JSON object embedded at the top of the ``.txt`` file."""
        return {
            "leaf_id": self.leaf_id,
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "ingested_at": self.ingested_at,
            "data_type": self.data_type.value,
            "domain": self.domain,
            "title": self.title,
            "tags": list(self.tags),
            "keywords": list(self.keywords),
            "entities": list(self.entities),
            "parent_id": self.parent_id,
            "source": self.source,
            "span": self.span.to_dict(),
            "order_index": self.order_index,
            "token_estimate": self.token_estimate,
            "checksum": self.checksum,
            "oversized": self.oversized,
            "status": self.status.value,
            "superseded_by": self.superseded_by,
            "extra": self.extra,
        }

    def render(self) -> str:
        """Serialize the leaf to its on-disk ``.txt`` representation."""
        header = json.dumps(self.header_dict(), indent=2, ensure_ascii=False)
        return f"{META_OPEN}\n{header}\n{META_CLOSE}\n{self.content}"

    @classmethod
    def parse(cls, text: str, *, path_hint: str = "<memory>") -> "MemoryLeaf":
        """Parse the on-disk representation produced by :meth:`render`.

        Raises:
            MalformedLeafError: If the delimiters or the JSON header are
                missing or invalid.
        """
        if not text.startswith(META_OPEN):
            raise MalformedLeafError(f"{path_hint}: missing '{META_OPEN}' header")
        close_at = text.find(META_CLOSE)
        if close_at == -1:
            raise MalformedLeafError(f"{path_hint}: unterminated metadata header")
        raw_header = text[len(META_OPEN) : close_at].strip()
        body_at = close_at + len(META_CLOSE)
        # A single newline separates the header from the body; it is a
        # delimiter, not content.
        if text[body_at : body_at + 1] == "\n":
            body_at += 1
        try:
            header = json.loads(raw_header)
        except json.JSONDecodeError as exc:
            raise MalformedLeafError(f"{path_hint}: invalid JSON header: {exc}") from exc
        return cls.from_header(header, content=text[body_at:])

    @classmethod
    def from_header(cls, header: Dict[str, Any], *, content: str = "") -> "MemoryLeaf":
        """Rebuild a leaf from a parsed header dict and its body."""
        try:
            leaf_id = header["leaf_id"]
        except KeyError as exc:
            raise MalformedLeafError("leaf header has no 'leaf_id'") from exc
        return cls(
            leaf_id=leaf_id,
            domain=header.get("domain", "general"),
            data_type=DataType.coerce(header.get("data_type")),
            title=header.get("title", ""),
            content=content,
            source=header.get("source", ""),
            timestamp=header.get("timestamp", ""),
            ingested_at=header.get("ingested_at", ""),
            tags=list(header.get("tags", [])),
            keywords=list(header.get("keywords", [])),
            entities=list(header.get("entities", [])),
            parent_id=header.get("parent_id"),
            span=Span.from_dict(header.get("span")),
            order_index=int(header.get("order_index", 0)),
            token_estimate=int(header.get("token_estimate", 0)),
            checksum=header.get("checksum", ""),
            oversized=bool(header.get("oversized", False)),
            status=LeafStatus(header.get("status", LeafStatus.ACTIVE.value)),
            superseded_by=header.get("superseded_by"),
            extra=dict(header.get("extra", {})),
            schema_version=int(header.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class ChildRef:
    """Descriptor of a child, embedded in its parent.

    Carrying title, excerpt and keywords in the parent is what keeps retrieval
    logarithmic: a branch can be scored and ranked without opening any child.
    """

    id: str
    kind: NodeKind
    title: str = ""
    excerpt: str = ""
    path: str = ""
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    timestamp: str = ""
    leaf_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "excerpt": self.excerpt,
            "path": self.path,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "timestamp": self.timestamp,
            "leaf_count": self.leaf_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChildRef":
        return cls(
            id=data["id"],
            kind=NodeKind(data.get("kind", NodeKind.LEAF.value)),
            title=data.get("title", ""),
            excerpt=data.get("excerpt", ""),
            path=data.get("path", ""),
            keywords=list(data.get("keywords", [])),
            tags=list(data.get("tags", [])),
            timestamp=data.get("timestamp", ""),
            leaf_count=int(data.get("leaf_count", 1)),
        )

    def searchable_text(self) -> str:
        return " ".join([self.title, self.excerpt, " ".join(self.keywords), " ".join(self.tags)])


@dataclass
class TimeRange:
    """Oldest and newest timestamps covered by a subtree."""

    start: str = ""
    end: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TimeRange":
        data = data or {}
        return cls(data.get("start", ""), data.get("end", ""))


@dataclass
class BranchNode:
    """A layer >= 1 summary node: a "summary of summaries"."""

    node_id: str
    layer: int
    domain: str
    title: str = ""
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    children: List[ChildRef] = field(default_factory=list)
    parent_id: Optional[str] = None
    time_range: TimeRange = field(default_factory=TimeRange)
    leaf_count: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    backend: str = "heuristic"
    schema_version: int = SCHEMA_VERSION

    @property
    def kind(self) -> NodeKind:
        return NodeKind.NODE

    @property
    def child_ids(self) -> List[str]:
        return [child.id for child in self.children]

    def searchable_text(self) -> str:
        """Every field the navigator scores this branch on."""
        return " ".join(
            [
                self.title,
                self.summary,
                " ".join(self.keywords),
                " ".join(self.tags),
                " ".join(self.entities),
                " ".join(child.searchable_text() for child in self.children),
            ]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "schema_version": self.schema_version,
            "layer": self.layer,
            "domain": self.domain,
            "title": self.title,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "entities": list(self.entities),
            "parent_id": self.parent_id,
            "leaf_count": self.leaf_count,
            "time_range": self.time_range.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "backend": self.backend,
            "child_ids": self.child_ids,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BranchNode":
        try:
            node_id = data["node_id"]
        except KeyError as exc:
            raise NodeNotFoundError("branch node JSON has no 'node_id'") from exc
        return cls(
            node_id=node_id,
            layer=int(data.get("layer", 1)),
            domain=data.get("domain", "general"),
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            keywords=list(data.get("keywords", [])),
            tags=list(data.get("tags", [])),
            entities=list(data.get("entities", [])),
            children=[ChildRef.from_dict(c) for c in data.get("children", [])],
            parent_id=data.get("parent_id"),
            time_range=TimeRange.from_dict(data.get("time_range")),
            leaf_count=int(data.get("leaf_count", 0)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            backend=data.get("backend", "heuristic"),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class DomainEntry:
    """Per-domain section of :class:`RootIndex`."""

    domain: str
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    top_nodes: List[ChildRef] = field(default_factory=list)
    leaf_count: int = 0
    time_range: TimeRange = field(default_factory=TimeRange)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "leaf_count": self.leaf_count,
            "time_range": self.time_range.to_dict(),
            "top_nodes": [n.to_dict() for n in self.top_nodes],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DomainEntry":
        return cls(
            domain=data.get("domain", "general"),
            summary=data.get("summary", ""),
            keywords=list(data.get("keywords", [])),
            top_nodes=[ChildRef.from_dict(n) for n in data.get("top_nodes", [])],
            leaf_count=int(data.get("leaf_count", 0)),
            time_range=TimeRange.from_dict(data.get("time_range")),
        )

    def searchable_text(self) -> str:
        return " ".join(
            [self.domain.replace(".", " "), self.summary, " ".join(self.keywords)]
            + [n.searchable_text() for n in self.top_nodes]
        )


@dataclass
class RootIndex:
    """``root.json`` — the trunk and the single entry point of every query."""

    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    title: str = "IHMT — Infinite Hierarchical Memory Tree"
    summary: str = ""
    topics: List[str] = field(default_factory=list)
    domains: Dict[str, DomainEntry] = field(default_factory=dict)
    depth: int = 0
    leaf_count: int = 0
    node_count: int = 0
    time_range: TimeRange = field(default_factory=TimeRange)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "depth": self.depth,
            "leaf_count": self.leaf_count,
            "node_count": self.node_count,
            "time_range": self.time_range.to_dict(),
            "topics": list(self.topics),
            "domains": {k: v.to_dict() for k, v in sorted(self.domains.items())},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RootIndex":
        return cls(
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
            title=data.get("title", "IHMT — Infinite Hierarchical Memory Tree"),
            summary=data.get("summary", ""),
            topics=list(data.get("topics", [])),
            domains={k: DomainEntry.from_dict(v) for k, v in data.get("domains", {}).items()},
            depth=int(data.get("depth", 0)),
            leaf_count=int(data.get("leaf_count", 0)),
            node_count=int(data.get("node_count", 0)),
            time_range=TimeRange.from_dict(data.get("time_range")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class Fact:
    """A dated ``(subject, attribute) -> value`` assertion on the timeline."""

    subject: str
    attribute: str
    value: str
    timestamp: str
    source_leaf_id: Optional[str] = None
    domain: str = "general"
    status: LeafStatus = LeafStatus.ACTIVE
    superseded_by: Optional[str] = None
    valid_from: str = ""
    valid_to: Optional[str] = None
    fact_id: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        """Timeline key grouping every value ever held by this attribute."""
        from .textutils import normalize

        return f"{normalize(self.subject)}::{normalize(self.attribute)}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "attribute": self.attribute,
            "value": self.value,
            "timestamp": self.timestamp,
            "source_leaf_id": self.source_leaf_id,
            "domain": self.domain,
            "status": self.status.value,
            "superseded_by": self.superseded_by,
            "valid_from": self.valid_from or self.timestamp,
            "valid_to": self.valid_to,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Fact":
        return cls(
            subject=data.get("subject", ""),
            attribute=data.get("attribute", ""),
            value=data.get("value", ""),
            timestamp=data.get("timestamp", ""),
            source_leaf_id=data.get("source_leaf_id"),
            domain=data.get("domain", "general"),
            status=LeafStatus(data.get("status", LeafStatus.ACTIVE.value)),
            superseded_by=data.get("superseded_by"),
            valid_from=data.get("valid_from", ""),
            valid_to=data.get("valid_to"),
            fact_id=data.get("fact_id", ""),
            note=data.get("note", ""),
        )


@dataclass
class Contradiction:
    """Two dated assertions about the same attribute that disagree."""

    subject: str
    attribute: str
    old: Fact
    new: Fact

    def render_notice(self) -> str:
        """Transparent, user-facing statement of the contradiction."""
        from .textutils import year_of

        return (
            f"In {year_of(self.old.timestamp)} you said "
            f"{self.subject} {self.attribute} = '{self.old.value}', "
            f"but in {year_of(self.new.timestamp)} you updated to '{self.new.value}'."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "attribute": self.attribute,
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
            "notice": self.render_notice(),
        }
