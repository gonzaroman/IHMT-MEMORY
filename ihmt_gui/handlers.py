"""JSON endpoints of the interface.

Everything here rests on **IHMT's public API**: ``IHMT``, ``MemoryStore`` and
``ConflictResolver``. The core is neither modified nor imported through
private paths.

Security rule running through the module: identifiers coming from the browser
are validated **against the active memory's catalog** before anything is
loaded. A file path is never accepted, so nothing outside the store can
ever be read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ihmt import IHMT, __version__ as IHMT_VERSION
from ihmt.models import IHMTError, NodeKind, NodeNotFoundError
from ihmt.textutils import truncate

from . import folders as folders_mod
from . import setup as setup_mod

#: How many results the diagnose screen returns.
SEARCH_TOP_K = 5

#: Maximum length of a leaf's text sent to the browser.
LEAF_TEXT_LIMIT = 40000


class ApiError(Exception):
    """Error with an HTTP status, serialized as JSON for the interface."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "detail": self.detail}


@dataclass
class GuiApi:
    """Interface state: which memory and which project are active."""

    memory_home: Path
    project_dir: Path
    _memory: Optional[IHMT] = None

    # ----------------------------------------------------------------- memory
    @property
    def memory_exists(self) -> bool:
        """Tell whether the active folder already contains a memory."""
        return folders_mod.has_memory(self.memory_home)

    def memory(self) -> IHMT:
        """Return the IHMT facade, re-reading the disk on every request.

        The re-read matters: the CLI or the MCP server may be writing to the
        same store while the interface is open.
        """
        if not self.memory_exists:
            raise ApiError(409, "no_memory", str(self.memory_home))
        if self._memory is None:
            self._memory = IHMT(self.memory_home)
        else:
            self._memory.store.reload()
            self._memory.resolver.reload()
        return self._memory

    def set_home(self, raw_path: str) -> Dict[str, Any]:
        """Change the active memory folder."""
        if not raw_path or not raw_path.strip():
            raise ApiError(400, "empty_path")
        new_home = Path(raw_path).expanduser()
        try:
            new_home = new_home.resolve()
        except OSError as exc:
            raise ApiError(400, "bad_path", str(exc)) from exc
        self.memory_home = new_home
        self._memory = None
        return self.status()

    def create_memory(self, raw_path: Optional[str] = None) -> Dict[str, Any]:
        """Create the memory in the given folder (or in the active one)."""
        target = Path(raw_path).expanduser() if raw_path else self.memory_home
        try:
            target.mkdir(parents=True, exist_ok=True)
            IHMT.initialize(target)
        except (IHMTError, OSError, ValueError) as exc:
            raise ApiError(400, "create_failed", str(exc)) from exc
        self.memory_home = target.resolve()
        self._memory = None
        return self.status()

    # ----------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        """Active folder, whether it holds a memory, and its counters."""
        data: Dict[str, Any] = {
            "memory_home": str(self.memory_home),
            "project_dir": str(self.project_dir),
            "memory_exists": self.memory_exists,
            "ihmt_version": IHMT_VERSION,
            "native_picker": folders_mod.native_dialog_available(),
        }
        if not self.memory_exists:
            return data

        memory = self.memory()
        stats = memory.stats()
        data["stats"] = stats
        data["total_files"] = stats["leaves"] + stats["nodes"] + 1
        data["config"] = {
            "branch_factor": memory.config.branch_factor,
            "target_tokens": memory.config.target_tokens,
            "beam_width": memory.config.beam_width,
            "backend": memory.config.summarizer_backend,
        }
        return data

    # ------------------------------------------------------------------- tree
    def tree(self, node_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the children of a node, one level per request.

        Without ``node_id`` it returns the trunk's domains. Domains are
        groupings inside ``root.json`` itself, so they are identified with the
        synthetic prefix ``domain:``.
        """
        memory = self.memory()
        root = memory.store.load_root()

        if not node_id:
            children = [
                {
                    "id": f"domain:{name}",
                    "kind": "domain",
                    "title": name,
                    "label": name,
                    "subtitle": truncate(entry.summary, 160),
                    "date": entry.time_range.end[:10],
                    "leaf_count": entry.leaf_count,
                    "has_children": bool(entry.top_nodes),
                }
                for name, entry in sorted(root.domains.items())
            ]
            return {"parent": None, "children": children}

        if node_id.startswith("domain:"):
            name = node_id.split(":", 1)[1]
            entry = root.domains.get(name)
            if entry is None:
                raise ApiError(404, "unknown_domain", name)
            return {
                "parent": node_id,
                "children": [self._ref_to_item(ref) for ref in entry.top_nodes],
            }

        catalog_entry = self._require_entry(memory, node_id)
        if catalog_entry.kind is NodeKind.LEAF:
            return {"parent": node_id, "children": []}
        node = memory.store.load_node(node_id)
        return {"parent": node_id, "children": [self._ref_to_item(ref) for ref in node.children]}

    @staticmethod
    def _ref_to_item(ref: Any) -> Dict[str, Any]:
        """Turn a ``ChildRef`` into a tree row.

        A leaf's title starts with its source file
        (``journal_personal.txt · 2024-07-22``). In a list where every sibling
        comes from the same file, that prefix only uses up the available width
        and pushes out what tells one leaf from another, so a short label for
        drawing the tree is sent as well.
        """
        label = ref.title
        if ref.kind is NodeKind.LEAF and " · " in ref.title:
            label = ref.title.split(" · ", 1)[1]
        return {
            "id": ref.id,
            "kind": ref.kind.value,
            "title": ref.title,
            "label": label,
            "subtitle": truncate(ref.excerpt, 160),
            "date": (ref.timestamp or "")[:10],
            "leaf_count": ref.leaf_count,
            "has_children": ref.kind is NodeKind.NODE,
        }

    @staticmethod
    def _require_entry(memory: IHMT, node_id: str) -> Any:
        """Validate an identifier against the catalog.

        This check is what stops the browser from asking for anything: if the
        identifier is not in the active memory's catalog, nothing is loaded.
        """
        entry = memory.store.catalog.entries.get(node_id)
        if entry is None:
            raise ApiError(404, "unknown_id", node_id)
        return entry

    # ------------------------------------------------------------------- leaf
    def leaf(self, leaf_id: str) -> Dict[str, Any]:
        """Text and metadata of a leaf, with its staleness notices."""
        if not leaf_id:
            raise ApiError(400, "missing_id")
        memory = self.memory()
        entry = self._require_entry(memory, leaf_id)
        if entry.kind is not NodeKind.LEAF:
            raise ApiError(400, "not_a_leaf", leaf_id)

        try:
            leaf = memory.get_leaf(leaf_id)
        except NodeNotFoundError as exc:
            raise ApiError(404, "unknown_id", leaf_id) from exc

        try:
            chain = memory.summarizer.path_to_root(leaf_id)
        except NodeNotFoundError:  # pragma: no cover - catalog out of sync
            chain = [leaf_id, "root"]

        return {
            "id": leaf.leaf_id,
            "title": leaf.title,
            "domain": leaf.domain,
            "data_type": leaf.data_type.value,
            "timestamp": leaf.timestamp,
            "ingested_at": leaf.ingested_at,
            "source": leaf.source,
            "tags": leaf.tags,
            "keywords": leaf.keywords,
            "entities": leaf.entities,
            "span": leaf.span.to_dict(),
            "tokens": leaf.token_estimate,
            "oversized": leaf.oversized,
            "context": leaf.extra.get("context", ""),
            "path": list(reversed(chain)),
            "notices": memory.resolver.notices_for_leaf(leaf),
            "content": leaf.content[:LEAF_TEXT_LIMIT],
            "truncated": len(leaf.content) > LEAF_TEXT_LIMIT,
            "file": memory.store.relative(memory.store.leaf_path(leaf.leaf_id, leaf.domain)),
        }

    # ----------------------------------------------------------------- search
    def search(self, query: str, clue: Optional[str] = None) -> Dict[str, Any]:
        """Search, and also report what it cost: branches opened and path."""
        if not query or not query.strip():
            raise ApiError(400, "empty_query")
        memory = self.memory()
        response = memory.search(query, top_k=SEARCH_TOP_K, clue=clue or None)
        stats = memory.stats()

        return {
            "query": response.query,
            "clue": clue or "",
            "confidence": round(response.confidence, 4),
            "ambiguous": response.ambiguous,
            "node_reads": response.node_reads,
            "leaf_reads": response.leaf_reads,
            "depth": response.depth,
            "total_files": stats["leaves"] + stats["nodes"] + 1,
            "clue_request": response.clue_request.to_dict() if response.clue_request else None,
            "results": [
                {
                    "id": result.leaf_id,
                    "title": result.title,
                    "domain": result.domain,
                    "score": round(result.score, 4),
                    "date": (result.timestamp or "")[:10],
                    "excerpt": truncate(result.content or result.excerpt, 400),
                    "path": result.path,
                    "notices": result.notices,
                }
                for result in response.results
            ],
        }

    # --------------------------------------------------------------- timeline
    def timeline(self) -> Dict[str, Any]:
        """Active state, history and detected contradictions."""
        memory = self.memory()
        resolver = memory.resolver

        grouped: Dict[str, Dict[str, Any]] = {}
        for fact in resolver.facts:
            entry = grouped.setdefault(
                fact.key,
                {"subject": fact.subject, "attribute": fact.attribute, "values": []},
            )
            entry["values"].append(
                {
                    "value": fact.value,
                    "timestamp": fact.timestamp,
                    "date": (fact.timestamp or "")[:10],
                    "status": fact.status.value,
                    "source_leaf_id": fact.source_leaf_id,
                }
            )
        for entry in grouped.values():
            entry["values"].sort(key=lambda v: v["timestamp"])

        return {
            "facts": [grouped[key] for key in sorted(grouped)],
            "conflicts": [conflict.to_dict() for conflict in resolver.detect_conflicts()],
            "fact_count": len(resolver.facts),
        }

    # ---------------------------------------------------------------- folders
    def folders(self, path: Optional[str] = None) -> Dict[str, Any]:
        """Subfolders of a path, for the built-in folder browser."""
        return folders_mod.list_directories(path).to_dict()

    def pick_folder(self, initial: Optional[str] = None) -> Dict[str, Any]:
        """Open the system folder dialog, if available."""
        if not folders_mod.native_dialog_available():
            raise ApiError(501, "no_native_picker")
        chosen = folders_mod.pick_directory_native(initial or str(self.memory_home))
        return {"path": chosen, "cancelled": chosen is None}

    # ------------------------------------------------------------------ setup
    def setup_state(self, refresh: Optional[str] = None) -> Dict[str, Any]:
        """How the MCP server is registered right now.

        Args:
            refresh: With any value, drop the cached answer and ask the CLI
                again. The *Check again* button uses it: if it reused the
                stored answer, pressing it right after approving the server
                would keep showing the previous state.
        """
        if refresh:
            setup_mod.invalidate_cache()
        return setup_mod.detect_state(self.project_dir).to_dict()

    def setup_preview(
        self,
        scope: str,
        memory_home: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the setup plan **without applying it**."""
        try:
            plan = setup_mod.build_plan(
                scope,
                memory_home or self.memory_home,
                project_dir or self.project_dir,
            )
        except ValueError as exc:
            raise ApiError(400, "bad_scope", str(exc)) from exc
        return plan.to_dict()

    def setup_apply(
        self,
        scope: str,
        memory_home: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply the plan. The only operation that writes outside the memory."""
        preview = self.setup_preview(scope, memory_home, project_dir)
        plan = setup_mod.SetupPlan(
            scope=preview["scope"],
            kind=preview["kind"],
            memory_home=preview["memory_home"],
            preview=preview["preview"],
            command=preview["command"],
            file_path=preview["file_path"],
            file_content=preview["file_content"],
            warnings=preview["warnings"],
        )
        result = setup_mod.apply_plan(plan)
        result["plan"] = preview
        result["state"] = self.setup_state()
        return result


def build_api(memory_home: Path | str, project_dir: Path | str) -> GuiApi:
    """Create the interface state for a folder and a project."""
    return GuiApi(
        memory_home=Path(memory_home).expanduser().resolve(),
        project_dir=Path(project_dir).expanduser().resolve(),
    )


#: Read-only endpoints: path -> (method, {name in the URL: parameter}).
#: The mapping is explicit on purpose: URL names are short for convenience and
#: the method's are descriptive; mixing them up was an easy mistake.
READ_ROUTES: Dict[str, Any] = {
    "/api/status": ("status", {}),
    "/api/tree": ("tree", {"id": "node_id"}),
    "/api/leaf": ("leaf", {"id": "leaf_id"}),
    "/api/search": ("search", {"q": "query", "clue": "clue"}),
    "/api/timeline": ("timeline", {}),
    "/api/folders": ("folders", {"path": "path"}),
    "/api/setup/state": ("setup_state", {"refresh": "refresh"}),
}

#: Endpoints that require POST. The last three are the only ones that write.
WRITE_ROUTES: Dict[str, Any] = {
    "/api/folders/pick": ("pick_folder", {"initial": "initial"}),
    "/api/setup/preview": (
        "setup_preview",
        {"scope": "scope", "memory_home": "memory_home", "project_dir": "project_dir"},
    ),
    "/api/home": ("set_home", {"path": "raw_path"}),
    "/api/setup/apply": (
        "setup_apply",
        {"scope": "scope", "memory_home": "memory_home", "project_dir": "project_dir"},
    ),
    "/api/memory/create": ("create_memory", {"path": "raw_path"}),
}

__all__ = ["ApiError", "GuiApi", "build_api", "READ_ROUTES", "WRITE_ROUTES"]
