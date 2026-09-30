"""Configuration for an IHMT store.

The configuration is persisted inside the memory directory itself
(``ihmt_memory/ihmt.config.json``) so that a store is fully self-describing and
can be relocated as a single directory.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict

MEMORY_DIRNAME = "ihmt_memory"
CONFIG_FILENAME = "ihmt.config.json"
SCHEMA_VERSION = 1


@dataclass
class IHMTConfig:
    """Tunable parameters governing chunking, tree shape and retrieval.

    Attributes:
        base_dir: Directory that *contains* the ``ihmt_memory`` store.
        branch_factor: Number of children accumulated before a Summarization
            Event fires. Also the logarithm base of the retrieval cost.
        target_tokens: Desired size of a layer-0 leaf, in estimated tokens.
        max_tokens: Hard ceiling before a chunk is flagged ``oversized``.
            Never enforced by cutting a syntactic block in half.
        min_tokens: Chunks smaller than this are merged with their neighbour
            when the merge does not violate a block boundary.
        beam_width: Branches kept alive per level during the tree descent.
        confidence_threshold: Minimum normalized score for an answer to be
            returned without asking the user for a clue.
        ambiguity_margin: Minimum score gap between the best and second-best
            candidate branch. A smaller gap is treated as ambiguity.
        max_clue_rounds: Maximum iterations of the interactive clue loop.
        summarizer_backend: ``"heuristic"`` (offline, default) or
            ``"anthropic"`` (optional, requires the ``anthropic`` package).
        summarizer_model: Model identifier used by LLM-backed summarizers.
        excerpt_chars: Length of the child excerpt embedded in a parent node.
        code_chunk_mode: ``"pack"`` (default) merges consecutive code blocks up
            to ``target_tokens``; ``"symbol"`` stores one leaf per class member,
            which is what a project index wants — a lookup then returns one
            method instead of a whole file.
    """

    base_dir: Path = field(default_factory=Path.cwd)
    branch_factor: int = 8
    target_tokens: int = 2000
    max_tokens: int = 3000
    min_tokens: int = 120
    beam_width: int = 3
    confidence_threshold: float = 0.45
    ambiguity_margin: float = 0.18
    max_clue_rounds: int = 3
    summarizer_backend: str = "heuristic"
    summarizer_model: str = "claude-sonnet-5"
    excerpt_chars: int = 320
    code_chunk_mode: str = "pack"
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------ paths
    @property
    def memory_dir(self) -> Path:
        """Root of the on-disk store."""
        return self.base_dir / MEMORY_DIRNAME

    @property
    def layer0_dir(self) -> Path:
        """Directory holding raw ``.txt`` leaves, partitioned by domain."""
        return self.memory_dir / "layer_0"

    @property
    def layers_dir(self) -> Path:
        """Directory holding the numbered branch layers (``1``..``N``)."""
        return self.memory_dir / "layers"

    @property
    def state_dir(self) -> Path:
        """Directory holding the catalog and the fact timeline."""
        return self.memory_dir / "state"

    @property
    def root_path(self) -> Path:
        """Path of ``root.json``, the trunk of the tree."""
        return self.memory_dir / "root.json"

    @property
    def catalog_path(self) -> Path:
        return self.state_dir / "catalog.json"

    @property
    def facts_path(self) -> Path:
        return self.state_dir / "facts.json"

    @property
    def config_path(self) -> Path:
        return self.memory_dir / CONFIG_FILENAME

    def layer_dir(self, layer: int) -> Path:
        """Return the directory for a given branch layer (``layer >= 1``)."""
        if layer < 1:
            raise ValueError(f"branch layers start at 1, got {layer}")
        return self.layers_dir / str(layer)

    # ------------------------------------------------------------- (de)serial
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("base_dir", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base_dir: Path) -> "IHMTConfig":
        """Build a config from a dict, ignoring unknown/legacy keys."""
        known = {f.name for f in fields(cls)} - {"base_dir"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(base_dir=Path(base_dir), **kwargs)

    def save(self) -> None:
        """Persist the configuration next to the store."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.config_path)

    @classmethod
    def load(cls, base_dir: Path | str = ".") -> "IHMTConfig":
        """Load the configuration for ``base_dir``, falling back to defaults."""
        base = Path(base_dir).expanduser().resolve()
        path = base / MEMORY_DIRNAME / CONFIG_FILENAME
        if not path.exists():
            return cls(base_dir=base)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise RuntimeError(f"corrupt IHMT config at {path}: {exc}") from exc
        return cls.from_dict(data, base)

    def validate(self) -> None:
        """Raise :class:`ValueError` if the parameters are incoherent."""
        if self.branch_factor < 2:
            raise ValueError("branch_factor must be >= 2 for the tree to converge")
        if self.target_tokens <= 0 or self.max_tokens < self.target_tokens:
            raise ValueError("require 0 < target_tokens <= max_tokens")
        if self.beam_width < 1:
            raise ValueError("beam_width must be >= 1")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must lie in [0, 1]")
        if self.code_chunk_mode not in ("pack", "symbol"):
            raise ValueError('code_chunk_mode must be "pack" or "symbol"')
