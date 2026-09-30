"""Filesystem persistence for the memory tree.

Every write goes through :meth:`MemoryStore.write_atomic` (write to a temporary
file in the same directory, then ``os.replace``), so a crash can never leave a
half-written node behind. A small catalog (``state/catalog.json``) indexes every
node so that "which items still have no parent?" is O(1) rather than a full
directory scan — and it can always be rebuilt from the files themselves.
"""

from __future__ import annotations

import errno
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import IHMTConfig
from .models import (
    BranchNode,
    MalformedLeafError,
    MemoryLeaf,
    NodeKind,
    NodeNotFoundError,
    RootIndex,
    StoreNotInitializedError,
)
from .textutils import slugify, timestamp_sort_key, utc_now_iso


class _FileLock:
    """Cooperative inter-process lock based on ``O_CREAT | O_EXCL``.

    Guards multi-step catalog updates. Stale locks (from a killed process) are
    broken after ``stale_after`` seconds so a store can never wedge permanently.
    """

    def __init__(self, path: Path, *, timeout: float = 10.0, stale_after: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: Optional[int] = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if self._is_stale():
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not acquire IHMT lock at {self.path}")
                time.sleep(0.05)

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > self.stale_after
        except FileNotFoundError:
            return False

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


@dataclass
class CatalogEntry:
    """Lightweight index record for one node of the tree."""

    id: str
    kind: NodeKind
    layer: int
    domain: str
    path: str  # relative to the memory directory, POSIX separators
    parent_id: Optional[str] = None
    timestamp: str = ""
    title: str = ""
    leaf_count: int = 1
    status: str = "ACTIVE"
    source: str = ""  # origin of a leaf (file path, "mcp://…"); empty for nodes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "layer": self.layer,
            "domain": self.domain,
            "path": self.path,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "title": self.title,
            "leaf_count": self.leaf_count,
            "status": self.status,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CatalogEntry":
        return cls(
            id=data["id"],
            kind=NodeKind(data.get("kind", NodeKind.LEAF.value)),
            layer=int(data.get("layer", 0)),
            domain=data.get("domain", "general"),
            path=data.get("path", ""),
            parent_id=data.get("parent_id"),
            timestamp=data.get("timestamp", ""),
            title=data.get("title", ""),
            leaf_count=int(data.get("leaf_count", 1)),
            status=data.get("status", "ACTIVE"),
            source=data.get("source", ""),
        )


@dataclass
class Catalog:
    """In-memory view of ``state/catalog.json``."""

    entries: Dict[str, CatalogEntry] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "updated_at": self.updated_at or utc_now_iso(),
            "entries": {k: v.to_dict() for k, v in sorted(self.entries.items())},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Catalog":
        return cls(
            entries={k: CatalogEntry.from_dict(v) for k, v in data.get("entries", {}).items()},
            updated_at=data.get("updated_at", ""),
        )


class MemoryStore:
    """All filesystem access to a single IHMT store.

    No other component touches the disk directly; this keeps the on-disk format
    replaceable (e.g. by an object store) behind one interface.
    """

    def __init__(self, config: IHMTConfig) -> None:
        self.config = config
        self._catalog: Optional[Catalog] = None
        self._batch_depth = 0
        self._catalog_dirty = False

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, base_dir: Path | str = ".", *, require_init: bool = True) -> "MemoryStore":
        """Open the store rooted at ``base_dir``.

        Args:
            base_dir: Directory containing ``ihmt_memory``.
            require_init: Raise if the store does not exist yet.

        Raises:
            StoreNotInitializedError: If ``require_init`` and no store is found.
        """
        config = IHMTConfig.load(base_dir)
        store = cls(config)
        if require_init and not store.exists():
            raise StoreNotInitializedError(
                f"no IHMT store at {config.memory_dir}. Run 'python init_ihmt.py' first."
            )
        return store

    def exists(self) -> bool:
        """Whether a store has already been initialized here."""
        return self.config.root_path.exists()

    def initialize(self, *, force: bool = False) -> RootIndex:
        """Create the directory skeleton and an empty ``root.json``.

        Idempotent. With ``force``, the *derived* state is rebuilt from
        scratch — trunk, catalog, fact timeline and every branch layer are
        discarded, and the surviving leaves are detached so that the next
        consolidation rebuilds the whole hierarchy. Leaf content is never
        deleted, so a forced re-init reorganizes memory rather than losing it.
        """
        self.config.validate()
        for path in (self.config.layer0_dir, self.config.layers_dir, self.config.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.config.layer_dir(1).mkdir(parents=True, exist_ok=True)
        self.config.save()

        if force:
            self._reset_derived_state()
        if force or not self.config.root_path.exists():
            root = RootIndex()
            self.save_root(root)
        else:
            root = self.load_root()
        if force or not self.config.catalog_path.exists():
            self.save_catalog()
        if force or not self.config.facts_path.exists():
            self.write_atomic(self.config.facts_path, json.dumps({"facts": []}, indent=2) + "\n")
        return root

    def _reset_derived_state(self) -> None:
        """Delete every branch node and detach all leaves from the tree.

        Everything above layer 0 is derived data: it can always be rebuilt by
        re-running consolidation, so a reset only has to be careful about the
        leaves themselves.
        """
        for layer_dir in sorted(self.config.layers_dir.glob("*")):
            if layer_dir.is_dir():
                for node_file in layer_dir.glob("*.json"):
                    node_file.unlink(missing_ok=True)

        self._catalog = Catalog()
        for path in self.iter_leaf_paths():
            try:
                leaf = self.load_leaf_file(path)
            except MalformedLeafError:
                continue
            if leaf.parent_id is not None:
                leaf.parent_id = None
                self.save_leaf(leaf, index=False)
            self.catalog.entries[leaf.leaf_id] = CatalogEntry(
                id=leaf.leaf_id,
                kind=NodeKind.LEAF,
                layer=0,
                domain=leaf.domain,
                path=self.relative(path),
                parent_id=None,
                timestamp=leaf.timestamp,
                title=leaf.title,
                leaf_count=1,
                status=leaf.status.value,
                source=leaf.source,
            )

    # --------------------------------------------------------------- low-level
    def write_atomic(self, path: Path, text: str) -> None:
        """Durably write ``text`` to ``path``, replacing it in one step."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():  # pragma: no cover - only on a failed write
                tmp.unlink(missing_ok=True)

    def write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        """Write a JSON document atomically, UTF-8, human-readable."""
        self.write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def read_json(self, path: Path) -> Dict[str, Any]:
        """Read a JSON document, raising :class:`NodeNotFoundError` if absent."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise NodeNotFoundError(f"missing file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise NodeNotFoundError(f"corrupt JSON at {path}: {exc}") from exc

    def relative(self, path: Path) -> str:
        """Path relative to the memory directory, using POSIX separators."""
        try:
            return path.relative_to(self.config.memory_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def absolute(self, relative_path: str) -> Path:
        """Inverse of :meth:`relative`."""
        return self.config.memory_dir / relative_path

    # -------------------------------------------------------------- leaf I/O
    def leaf_path(self, leaf_id: str, domain: str) -> Path:
        """Where a leaf of ``domain`` lives on disk."""
        return self.config.layer0_dir / slugify(domain) / f"{leaf_id}.txt"

    def save_leaf(self, leaf: MemoryLeaf, *, index: bool = True) -> Path:
        """Persist a leaf and (by default) index it in the catalog."""
        path = self.leaf_path(leaf.leaf_id, leaf.domain)
        self.write_atomic(path, leaf.render())
        if index:
            self.upsert_entry(
                CatalogEntry(
                    id=leaf.leaf_id,
                    kind=NodeKind.LEAF,
                    layer=0,
                    domain=leaf.domain,
                    path=self.relative(path),
                    parent_id=leaf.parent_id,
                    timestamp=leaf.timestamp,
                    title=leaf.title,
                    leaf_count=1,
                    status=leaf.status.value,
                    source=leaf.source,
                )
            )
        return path

    def load_leaf(self, leaf_id: str) -> MemoryLeaf:
        """Load a leaf by id, falling back to a scan if the catalog is stale."""
        entry = self.catalog.entries.get(leaf_id)
        candidates: List[Path] = []
        if entry and entry.kind is NodeKind.LEAF:
            candidates.append(self.absolute(entry.path))
        candidates.extend(self.config.layer0_dir.glob(f"*/{leaf_id}.txt"))
        for path in candidates:
            if path.exists():
                return self.load_leaf_file(path)
        raise NodeNotFoundError(f"no leaf with id {leaf_id!r}")

    def load_leaf_file(self, path: Path) -> MemoryLeaf:
        """Parse a leaf from an explicit path."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NodeNotFoundError(f"missing leaf file: {path}") from exc
        return MemoryLeaf.parse(text, path_hint=str(path))

    def iter_leaf_paths(self, domain: Optional[str] = None) -> Iterator[Path]:
        """Yield leaf file paths in a stable order, optionally one domain."""
        pattern = f"{slugify(domain)}/*.txt" if domain else "*/*.txt"
        yield from sorted(self.config.layer0_dir.glob(pattern))

    def iter_leaves(self, domain: Optional[str] = None) -> Iterator[MemoryLeaf]:
        """Yield every leaf, skipping (but reporting) corrupt files."""
        for path in self.iter_leaf_paths(domain):
            try:
                yield self.load_leaf_file(path)
            except MalformedLeafError:
                continue

    # -------------------------------------------------------------- node I/O
    def node_path(self, node_id: str, layer: int) -> Path:
        """Where a branch node of ``layer`` lives on disk."""
        return self.config.layer_dir(layer) / f"{node_id}.json"

    def save_node(self, node: BranchNode, *, index: bool = True) -> Path:
        """Persist a branch node and (by default) index it in the catalog."""
        node.updated_at = utc_now_iso()
        path = self.node_path(node.node_id, node.layer)
        self.write_json(path, node.to_dict())
        if index:
            self.upsert_entry(
                CatalogEntry(
                    id=node.node_id,
                    kind=NodeKind.NODE,
                    layer=node.layer,
                    domain=node.domain,
                    path=self.relative(path),
                    parent_id=node.parent_id,
                    timestamp=node.time_range.end or node.updated_at,
                    title=node.title,
                    leaf_count=node.leaf_count,
                )
            )
        return path

    def load_node(self, node_id: str) -> BranchNode:
        """Load a branch node by id, falling back to a scan across layers."""
        entry = self.catalog.entries.get(node_id)
        candidates: List[Path] = []
        if entry and entry.kind is NodeKind.NODE:
            candidates.append(self.absolute(entry.path))
        candidates.extend(self.config.layers_dir.glob(f"*/{node_id}.json"))
        for path in candidates:
            if path.exists():
                return BranchNode.from_dict(self.read_json(path))
        raise NodeNotFoundError(f"no branch node with id {node_id!r}")

    def iter_nodes(self, layer: int) -> Iterator[BranchNode]:
        """Yield every branch node of a layer in a stable order."""
        directory = self.config.layer_dir(layer)
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                yield BranchNode.from_dict(self.read_json(path))
            except NodeNotFoundError:
                continue

    def max_layer(self) -> int:
        """Highest branch layer that currently holds at least one node."""
        if not self.config.layers_dir.exists():
            return 0
        layers = [
            int(p.name)
            for p in self.config.layers_dir.iterdir()
            if p.is_dir() and p.name.isdigit() and any(p.glob("*.json"))
        ]
        return max(layers) if layers else 0

    # ----------------------------------------------------------------- trunk
    def load_root(self) -> RootIndex:
        """Load ``root.json``."""
        if not self.config.root_path.exists():
            raise StoreNotInitializedError(f"missing trunk: {self.config.root_path}")
        return RootIndex.from_dict(self.read_json(self.config.root_path))

    def save_root(self, root: RootIndex) -> None:
        """Persist ``root.json``."""
        root.updated_at = utc_now_iso()
        self.write_json(self.config.root_path, root.to_dict())

    # --------------------------------------------------------------- catalog
    @property
    def catalog(self) -> Catalog:
        """Lazily loaded catalog; rebuilt transparently if missing."""
        if self._catalog is None:
            if self.config.catalog_path.exists():
                self._catalog = Catalog.from_dict(self.read_json(self.config.catalog_path))
            else:
                self._catalog = Catalog()
        return self._catalog

    def reload(self) -> None:
        """Drop cached state so the next read comes from disk.

        A long-lived process (an MCP server, a notebook) must not keep serving
        a catalog that another writer — the CLI, an editor — has since changed.
        """
        self._catalog = None

    def save_catalog(self) -> None:
        """Persist the catalog atomically (deferred while a :meth:`batch` is open)."""
        if self._batch_depth:
            self._catalog_dirty = True
            return
        catalog = self.catalog
        catalog.updated_at = utc_now_iso()
        self.write_json(self.config.catalog_path, catalog.to_dict())
        self._catalog_dirty = False

    @contextmanager
    def batch(self) -> Iterator["MemoryStore"]:
        """Defer catalog writes until the block ends.

        Every leaf and node write updates the catalog, and the catalog is one
        JSON file: writing it after each of thousands of leaves makes a bulk
        ingest quadratic. Inside ``with store.batch():`` the catalog is written
        once, on exit — files are still written one by one, so a crash leaves
        a store that :meth:`rebuild_catalog` can always recover.
        """
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if not self._batch_depth and self._catalog_dirty:
                self.save_catalog()

    def leaves_by_source(self, source: str) -> List[CatalogEntry]:
        """Catalog entries of every leaf ingested from ``source``."""
        return [
            e for e in self.catalog.entries.values() if e.kind is NodeKind.LEAF and e.source == source
        ]

    def delete_leaf(self, leaf_id: str) -> bool:
        """Remove a leaf file and its catalog entry.

        Only meant for *derived* stores such as a project index, whose leaves
        are a cache of files that still exist elsewhere. Long-term memories are
        never deleted — they are demoted to ``HISTORICAL`` instead. Parents that
        referenced the leaf are left as they are; rebuild the tree afterwards.
        """
        entry = self.catalog.entries.pop(leaf_id, None)
        if entry is None:
            return False
        self.absolute(entry.path).unlink(missing_ok=True)
        self.save_catalog()
        return True

    def upsert_entry(self, entry: CatalogEntry) -> None:
        """Insert or update one catalog record and flush it to disk."""
        self.catalog.entries[entry.id] = entry
        self.save_catalog()

    def set_parent(self, child_id: str, parent_id: str) -> None:
        """Attach a child to its parent, updating both the file and the catalog.

        Called by the summarizer *after* the parent node is durably on disk, so
        an interrupted consolidation re-processes the group rather than
        orphaning it.
        """
        entry = self.catalog.entries.get(child_id)
        if entry is None:
            raise NodeNotFoundError(f"{child_id!r} is not in the catalog")
        if entry.kind is NodeKind.LEAF:
            leaf = self.load_leaf(child_id)
            leaf.parent_id = parent_id
            self.save_leaf(leaf, index=False)
        else:
            node = self.load_node(child_id)
            node.parent_id = parent_id
            self.save_node(node, index=False)
        entry.parent_id = parent_id
        self.save_catalog()

    def pending(self, layer: int) -> List[CatalogEntry]:
        """Catalog entries at ``layer`` that have no parent yet.

        These are exactly the items awaiting a Summarization Event, ordered by
        timestamp so that consolidation is chronologically coherent.
        """
        items = [e for e in self.catalog.entries.values() if e.layer == layer and not e.parent_id]
        return sorted(items, key=lambda e: (timestamp_sort_key(e.timestamp), e.id))

    def entries_at(self, layer: int) -> List[CatalogEntry]:
        """Every catalog entry at a given layer, ordered by timestamp."""
        items = [e for e in self.catalog.entries.values() if e.layer == layer]
        return sorted(items, key=lambda e: (timestamp_sort_key(e.timestamp), e.id))

    def rebuild_catalog(self) -> Catalog:
        """Rebuild the catalog by scanning every file on disk.

        The files are the source of truth; the catalog is only an index. This
        makes the store recoverable after manual edits or a partial copy.
        """
        with _FileLock(self.config.state_dir / "catalog.lock"):
            catalog = Catalog()
            for path in self.iter_leaf_paths():
                try:
                    leaf = self.load_leaf_file(path)
                except MalformedLeafError:
                    continue
                catalog.entries[leaf.leaf_id] = CatalogEntry(
                    id=leaf.leaf_id,
                    kind=NodeKind.LEAF,
                    layer=0,
                    domain=leaf.domain,
                    path=self.relative(path),
                    parent_id=leaf.parent_id,
                    timestamp=leaf.timestamp,
                    title=leaf.title,
                    leaf_count=1,
                    status=leaf.status.value,
                    source=leaf.source,
                )
            for layer in range(1, self.max_layer() + 1):
                for node in self.iter_nodes(layer):
                    catalog.entries[node.node_id] = CatalogEntry(
                        id=node.node_id,
                        kind=NodeKind.NODE,
                        layer=node.layer,
                        domain=node.domain,
                        path=self.relative(self.node_path(node.node_id, node.layer)),
                        parent_id=node.parent_id,
                        timestamp=node.time_range.end or node.updated_at,
                        title=node.title,
                        leaf_count=node.leaf_count,
                    )
            self._catalog = catalog
            self.save_catalog()
        return catalog

    # ----------------------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        """Aggregate counters used by the CLI and by the trunk rebuild."""
        entries = self.catalog.entries.values()
        leaves = [e for e in entries if e.kind is NodeKind.LEAF]
        domains: Dict[str, int] = {}
        for leaf in leaves:
            domains[leaf.domain] = domains.get(leaf.domain, 0) + 1
        per_layer = {}
        for layer in range(1, self.max_layer() + 1):
            per_layer[layer] = sum(1 for e in entries if e.layer == layer)
        return {
            "leaves": len(leaves),
            "nodes": sum(1 for e in entries if e.kind is NodeKind.NODE),
            "depth": self.max_layer(),
            "domains": dict(sorted(domains.items())),
            "nodes_per_layer": per_layer,
            "pending_layer_0": len(self.pending(0)),
            "memory_dir": str(self.config.memory_dir),
        }
