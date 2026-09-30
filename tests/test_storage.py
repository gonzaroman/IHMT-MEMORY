"""Persistence: leaf round-trips, atomic writes, catalog recovery."""

from __future__ import annotations

import json
import unittest

from ihmt.models import DataType, MalformedLeafError, MemoryLeaf, NodeNotFoundError, Span

from .base import StoreTestCase


class LeafFormatTests(StoreTestCase):
    """A leaf file is self-describing and survives a round-trip unchanged."""

    def make_leaf(self, content: str = "línea uno\nlínea dos\n") -> MemoryLeaf:
        return MemoryLeaf(
            leaf_id="L-test-0000-abcdef1234",
            domain="test.domain",
            data_type=DataType.NARRATIVE,
            title="a title with ünïcode",
            content=content,
            source="inline",
            tags=["type:narrative", "domain:test.domain"],
            keywords=["uno", "dos"],
            entities=["Marta"],
            span=Span(1, 2),
            token_estimate=5,
        )

    def test_render_and_parse_round_trip(self) -> None:
        leaf = self.make_leaf()
        parsed = MemoryLeaf.parse(leaf.render())

        self.assertEqual(parsed.leaf_id, leaf.leaf_id)
        self.assertEqual(parsed.content, leaf.content, "content must survive byte-for-byte")
        self.assertEqual(parsed.tags, leaf.tags)
        self.assertEqual(parsed.span.end_line, 2)
        self.assertIs(parsed.data_type, DataType.NARRATIVE)

    def test_content_containing_the_delimiter_is_still_recovered(self) -> None:
        # The header is delimited by the *first* closing marker, so body text
        # that mentions it is not treated as metadata.
        leaf = self.make_leaf(content="a body mentioning IHMT-META>>> in prose\n")
        parsed = MemoryLeaf.parse(leaf.render())
        self.assertEqual(parsed.content, leaf.content)

    def test_the_metadata_header_is_valid_json(self) -> None:
        rendered = self.make_leaf().render()
        header = rendered.split("<<<IHMT-META", 1)[1].split("IHMT-META>>>", 1)[0]
        payload = json.loads(header)

        for field in ("leaf_id", "timestamp", "data_type", "tags", "domain", "parent_id"):
            self.assertIn(field, payload, "the strict header must carry every required field")

    def test_a_corrupt_header_raises_a_typed_error(self) -> None:
        with self.assertRaises(MalformedLeafError):
            MemoryLeaf.parse("no header here\n")
        with self.assertRaises(MalformedLeafError):
            MemoryLeaf.parse("<<<IHMT-META\n{not json}\nIHMT-META>>>\nbody")

    def test_saving_writes_utf8_and_reloads_identically(self) -> None:
        leaf = self.make_leaf(content="acentos: ñáéíóú — em dash\n")
        path = self.memory.store.save_leaf(leaf)

        self.assertTrue(path.exists())
        self.assertEqual(self.memory.get_leaf(leaf.leaf_id).content, leaf.content)
        self.assertIn("layer_0/test.domain", path.as_posix())


class CatalogTests(StoreTestCase):
    """The catalog is an index, and the files remain the source of truth."""

    def test_rebuild_recovers_the_catalog_after_deletion(self) -> None:
        self.ingest_example("narrative_es.txt")
        self.memory.flush()
        before = dict(self.memory.store.catalog.entries)

        self.memory.config.catalog_path.unlink()
        self.memory.store._catalog = None  # force a reload from disk
        rebuilt = self.memory.store.rebuild_catalog()

        self.assertEqual(set(rebuilt.entries), set(before))
        for node_id, entry in rebuilt.entries.items():
            self.assertEqual(entry.parent_id, before[node_id].parent_id, "parent links come from the files")

    def test_unknown_identifiers_raise_node_not_found(self) -> None:
        with self.assertRaises(NodeNotFoundError):
            self.memory.get_leaf("L-does-not-exist")
        with self.assertRaises(NodeNotFoundError):
            self.memory.store.load_node("N9-nope")

    def test_atomic_write_leaves_no_temporary_files_behind(self) -> None:
        self.ingest_example("recipe_paella.md")
        self.memory.flush()
        leftovers = [p.name for p in self.memory.config.memory_dir.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_forced_reinitialization_keeps_leaves_and_rebuilds_the_tree(self) -> None:
        self.ingest_example("journal_personal.txt")
        self.memory.flush()
        leaf_count = self.memory.stats()["leaves"]
        self.assertGreater(leaf_count, 0)

        from ihmt import IHMT

        reset = IHMT.initialize(self.workspace, force=True)
        self.assertEqual(reset.stats()["leaves"], leaf_count, "leaf content is never destroyed")
        self.assertEqual(reset.stats()["nodes"], 0, "derived branches are cleared")

        reset.flush()
        stats = reset.stats()
        self.assertGreater(stats["nodes"], 0, "the tree rebuilds from the surviving leaves")
        for entry in reset.store.catalog.entries.values():
            if entry.parent_id:
                self.assertIn(entry.parent_id, reset.store.catalog.entries, "no dangling parent links")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
