"""Summarization events, upward propagation and trunk maintenance."""

from __future__ import annotations

import unittest

from ihmt.models import NodeKind

from .base import StoreTestCase


class ConsolidationTests(StoreTestCase):
    """Layer 0 -> layer 1 -> ... -> root."""

    branch_factor = 3

    def seed(self, count: int = 9, domain: str = "notes") -> None:
        """Ingest ``count`` small documents into one domain."""
        for index in range(count):
            self.memory.ingest_text(
                f"Nota número {index}. Tema recurrente: inventario y almacén, apunte {index}.",
                source=f"note-{index}.txt",
                domain=domain,
            )

    def test_a_summarization_event_fires_at_the_branch_factor(self) -> None:
        # auto_consolidate is on, so events fire during ingestion.
        self.seed(count=self.branch_factor)
        stats = self.memory.stats()

        self.assertEqual(stats["nodes"], 1, "exactly one branch node after the first full group")
        self.assertEqual(stats["pending_layer_0"], 0)

    def test_a_partial_group_waits_until_flushed(self) -> None:
        self.seed(count=self.branch_factor - 1)
        self.assertEqual(self.memory.stats()["nodes"], 0, "an incomplete group must not be promoted")

        report = self.memory.flush()
        self.assertTrue(report.changed)
        self.assertEqual(self.memory.stats()["nodes"], 1)
        self.assertEqual(report.events[0].trigger, "flush")

    def test_summaries_propagate_upward_layer_by_layer(self) -> None:
        self.seed(count=self.branch_factor**2)
        self.memory.flush()
        stats = self.memory.stats()

        self.assertGreaterEqual(stats["depth"], 2, "nine leaves with branch factor 3 need two layers")
        self.assertGreaterEqual(stats["nodes_per_layer"].get(1, 0), 3)
        self.assertGreaterEqual(stats["nodes_per_layer"].get(2, 0), 1)

    def test_every_leaf_is_reachable_from_the_root(self) -> None:
        self.seed(count=7)
        self.ingest_example("recipe_paella.md")
        self.memory.flush()

        root = self.memory.store.load_root()
        reachable = set()

        def walk(ref_id: str) -> None:
            reachable.add(ref_id)
            entry = self.memory.store.catalog.entries.get(ref_id)
            if entry is None or entry.kind is NodeKind.LEAF:
                return
            for child in self.memory.store.load_node(ref_id).children:
                walk(child.id)

        for domain in root.domains.values():
            for ref in domain.top_nodes:
                walk(ref.id)

        leaves = {e.id for e in self.memory.store.catalog.entries.values() if e.kind is NodeKind.LEAF}
        self.assertTrue(leaves, "the store should contain leaves")
        self.assertEqual(leaves - reachable, set(), "every leaf must hang under the trunk")

    def test_nothing_is_unreachable_when_several_layers_have_leftovers(self) -> None:
        # Without a flush, every layer leaves up to branch_factor-1 parentless
        # items. With a deep tree that adds up to more than a fixed cap would
        # hold, and any trimming at the trunk would push material out of the
        # root's reach: invisible to search, the interface and the MCP server.
        for index in range(120):
            self.memory.ingest_text(
                f"Note {index} about inventory and the warehouse, with enough text.",
                source=f"note-{index}.txt",
                domain="work",
            )
        self.memory.consolidate()  # no force: leaves partial groups behind

        orphans = [e for e in self.memory.store.catalog.entries.values() if not e.parent_id]
        layers = {e.layer for e in orphans}
        self.assertGreater(len(layers), 1, "the case only makes sense with leftovers in several layers")

        root = self.memory.store.load_root()
        referenced = {ref.id for ref in root.domains["work"].top_nodes}
        self.assertEqual(
            {e.id for e in orphans},
            referenced,
            "the trunk must reference EVERYTHING that has no parent",
        )

        reachable: set[str] = set()

        def descend(node_id: str) -> None:
            if node_id in reachable:
                return
            reachable.add(node_id)
            entry = self.memory.store.catalog.entries.get(node_id)
            if entry is None or entry.kind is NodeKind.LEAF:
                return
            for child in self.memory.store.load_node(node_id).children:
                descend(child.id)

        for ref in root.domains["work"].top_nodes:
            descend(ref.id)

        leaves = {
            e.id for e in self.memory.store.catalog.entries.values() if e.kind is NodeKind.LEAF
        }
        self.assertEqual(leaves - reachable, set(), "no leaf may be left outside the tree")

    def test_consolidation_is_idempotent(self) -> None:
        self.seed(count=6)
        self.memory.flush()
        before = self.memory.stats()

        second = self.memory.consolidate()
        self.assertFalse(second.changed, "a second pass with no new material must be a no-op")
        self.assertEqual(self.memory.stats(), before)

    def test_children_know_their_parent_and_parents_list_their_children(self) -> None:
        self.seed(count=self.branch_factor)
        node = next(self.memory.store.iter_nodes(1))

        for child in node.children:
            entry = self.memory.store.catalog.entries[child.id]
            self.assertEqual(entry.parent_id, node.node_id)
            self.assertEqual(self.memory.get_leaf(child.id).parent_id, node.node_id)

    def test_a_parent_carries_enough_signal_to_be_ranked_without_opening_children(self) -> None:
        self.seed(count=self.branch_factor)
        node = next(self.memory.store.iter_nodes(1))

        self.assertTrue(node.summary, "a branch must summarize its children")
        self.assertTrue(node.keywords)
        self.assertTrue(all(child.title for child in node.children))
        self.assertTrue(all(child.excerpt for child in node.children))
        self.assertEqual(node.leaf_count, self.branch_factor)

    def test_the_trunk_reports_domains_topics_and_counters(self) -> None:
        self.seed(count=4, domain="notes")
        self.ingest_example("clinical_history.txt")
        self.memory.flush()
        root = self.memory.store.load_root()

        self.assertIn("notes", root.domains)
        self.assertIn("medicine.clinical", root.domains)
        self.assertGreater(root.leaf_count, 0)
        self.assertTrue(root.topics)
        self.assertTrue(root.summary)
        self.assertTrue(root.time_range.start)

    def test_path_to_root_is_a_complete_chain(self) -> None:
        self.seed(count=self.branch_factor**2)
        self.memory.flush()
        leaf_id = next(
            e.id for e in self.memory.store.catalog.entries.values() if e.kind is NodeKind.LEAF
        )
        chain = self.memory.summarizer.path_to_root(leaf_id)

        self.assertEqual(chain[0], leaf_id)
        self.assertEqual(chain[-1], "root")
        self.assertGreaterEqual(len(chain), 3, "leaf -> branch -> ... -> root")

    def test_summaries_are_deterministic_with_the_offline_backend(self) -> None:
        self.seed(count=self.branch_factor)
        first = next(self.memory.store.iter_nodes(1))

        from ihmt.recursive_summarizer import RecursiveSummarizer
        from ihmt.summarizers import SummaryInput

        children = [
            SummaryInput(id=c.id, title=c.title, text=c.excerpt, keywords=c.keywords, tags=c.tags, timestamp=c.timestamp)
            for c in first.children
        ]
        backend = RecursiveSummarizer(self.memory.store).backend
        again = backend.summarize(children, domain=first.domain, layer=first.layer)

        self.assertEqual(again.summary, first.summary)
        self.assertEqual(again.keywords, first.keywords)


class ReingestionTests(StoreTestCase):
    """Re-ingesting the same document must not duplicate or detach memory."""

    def test_reingesting_a_file_is_stable(self) -> None:
        first = self.ingest_example("recipe_paella.md")
        self.memory.flush()
        stats = self.memory.stats()

        second = self.ingest_example("recipe_paella.md")
        self.assertEqual(first.leaf_ids, second.leaf_ids, "leaf ids are content-derived and stable")
        self.assertEqual(self.memory.stats()["leaves"], stats["leaves"], "no duplicate leaves")

        for leaf_id in second.leaf_ids:
            parent = self.memory.get_leaf(leaf_id).parent_id
            if parent:
                self.assertIn(parent, self.memory.store.catalog.entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
