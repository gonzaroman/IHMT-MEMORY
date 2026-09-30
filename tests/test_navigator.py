"""Tree-walking retrieval, logarithmic cost and the interactive clue loop."""

from __future__ import annotations

import unittest
from typing import List, Optional

from ihmt.semantic_navigator import ClueRequest

from .base import StoreTestCase


class DescentTests(StoreTestCase):
    """The navigator walks root -> branch -> leaf instead of scanning."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("InventoryService.java", "narrative_es.txt", "journal_personal.txt", "recipe_paella.md"):
            self.ingest_example(name)
        self.memory.flush()

    def test_a_specific_query_finds_the_right_leaf(self) -> None:
        response = self.memory.search("reserveStock soft hold quantity", top_k=3)

        self.assertTrue(response.results)
        self.assertIn("reserveStock", response.best.content)
        self.assertEqual(response.best.domain, "software.java")

    def test_the_result_carries_the_path_it_was_found_through(self) -> None:
        response = self.memory.search("reserveStock soft hold quantity")
        path = response.best.path

        self.assertEqual(path[0], "root")
        self.assertEqual(path[-1], response.best.leaf_id)
        self.assertGreaterEqual(len(path), 3, "root -> branch -> leaf at minimum")

    def test_retrieval_opens_far_fewer_files_than_a_scan(self) -> None:
        stats = self.memory.stats()
        response = self.memory.search("socarrat paella reposo", top_k=3)

        total_files = stats["leaves"] + stats["nodes"] + 1
        self.assertTrue(response.results)
        self.assertLess(
            response.node_reads + response.leaf_reads,
            total_files / 2,
            "the descent must not degenerate into a scan",
        )

    def test_node_reads_stay_bounded_by_beam_times_depth(self) -> None:
        response = self.memory.search("Marta cuaderno inventario", top_k=3)
        ceiling = 1 + self.memory.config.beam_width * (self.memory.store.max_layer() + 1)

        self.assertLessEqual(response.node_reads, ceiling)

    def test_a_query_in_another_language_reaches_its_own_domain(self) -> None:
        response = self.memory.search("cómo se hace el socarrat de la paella", top_k=2)

        self.assertTrue(response.results)
        self.assertEqual(response.best.domain, "process.cooking")

    def test_an_empty_query_returns_nothing_rather_than_everything(self) -> None:
        response = self.memory.search("   ")
        self.assertEqual(response.results, [])

    def test_a_query_matching_nothing_returns_nothing(self) -> None:
        # Returning zero-scoring leaves would dress "nothing is stored about
        # this" up as either a confident answer or a pointless disambiguation.
        response = self.memory.search("zxqwv kubernetes helm unmatched")

        self.assertEqual(response.results, [])
        self.assertLess(response.confidence, self.memory.config.confidence_threshold)
        self.assertIsNone(response.clue_request)


class ClueLoopTests(StoreTestCase):
    """An ambiguous query asks for a clue instead of guessing."""

    def setUp(self) -> None:
        super().setUp()
        self.ingest_example("journal_personal.txt")
        self.ingest_example("narrative_es.txt")
        self.memory.flush()

    def test_a_bare_first_name_is_reported_as_ambiguous(self) -> None:
        response = self.memory.search("Luis", top_k=4)

        self.assertTrue(response.ambiguous)
        self.assertIsNotNone(response.clue_request)
        self.assertGreaterEqual(len(response.clue_request.options), 2)
        self.assertIn("Luis", response.clue_request.prompt())

    def test_the_clue_loop_resolves_the_ambiguity(self) -> None:
        asked: List[ClueRequest] = []

        def provider(request: ClueRequest) -> str:
            asked.append(request)
            return "vacaciones en Benidorm"

        response = self.memory.ask("Luis", provider, top_k=3)

        self.assertEqual(len(asked), 1, "the user should be asked exactly once")
        self.assertFalse(response.needs_clue, "the clue must resolve the query")
        self.assertIn("Benidorm", response.best.content)
        self.assertEqual(response.clues_used, ["vacaciones en Benidorm"])

    def test_the_joint_query_beats_either_term_alone(self) -> None:
        joint = self.memory.navigator.cross_reference("Luis", "vacaciones en Benidorm")
        self.assertIn("Benidorm", joint.best.content)
        self.assertIn("+", joint.query)

    def test_declining_to_give_a_clue_ends_the_loop_honestly(self) -> None:
        def refuse(request: ClueRequest) -> Optional[str]:
            return None

        response = self.memory.ask("Luis", refuse)

        self.assertTrue(response.needs_clue, "without a clue the answer stays flagged as ambiguous")
        self.assertEqual(response.clues_used, [])

    def test_repeating_the_same_clue_does_not_loop_forever(self) -> None:
        calls = {"n": 0}

        def repeat(request: ClueRequest) -> str:
            calls["n"] += 1
            return "Luis"

        self.memory.ask("Luis", repeat, max_rounds=5)
        self.assertLessEqual(calls["n"], 2, "a clue that adds nothing must stop the loop")

    def test_outline_renders_the_hierarchy(self) -> None:
        lines = self.memory.outline(max_depth=2)
        self.assertTrue(lines[0].startswith("root.json"))
        self.assertGreater(len(lines), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
