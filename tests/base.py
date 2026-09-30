"""Shared fixtures: a disposable store in a temporary directory."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ihmt import IHMT  # noqa: E402  (path setup must precede the import)

EXAMPLES = PROJECT_ROOT / "examples"


class StoreTestCase(unittest.TestCase):
    """Base case providing a fresh IHMT store per test.

    Defaults to a small branch factor and short leaves so that a handful of
    documents still exercises a multi-layer tree.
    """

    branch_factor: int = 4
    target_tokens: int = 400
    max_tokens: int = 900

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.memory: IHMT = IHMT.initialize(
            self.workspace,
            branch_factor=self.branch_factor,
            target_tokens=self.target_tokens,
            max_tokens=self.max_tokens,
        )
        self.addCleanup(self._tmp.cleanup)

    # ------------------------------------------------------------- helpers
    def example(self, name: str) -> Path:
        """Path to a bundled example document."""
        path = EXAMPLES / name
        self.assertTrue(path.exists(), f"missing example file: {path}")
        return path

    def ingest_example(self, name: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        """Ingest a bundled example and return its report."""
        return self.memory.ingest_file(self.example(name), **kwargs)

    def leaves_of(self, report) -> list:  # type: ignore[no-untyped-def]
        """Load every leaf produced by an ingest report, in order."""
        return [self.memory.get_leaf(leaf_id) for leaf_id in report.leaf_ids]
