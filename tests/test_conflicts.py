"""Recency weighting, historical preservation and transparent notices."""

from __future__ import annotations

import unittest

from ihmt.models import LeafStatus

from .base import StoreTestCase


class RecencyTests(StoreTestCase):
    """The newest assertion wins; the older one is kept, not deleted."""

    def record_location(self) -> None:
        self.memory.record_fact("user", "location", "Madrid", timestamp="2024-03-11T00:00:00Z")
        self.memory.record_fact("user", "location", "Valencia", timestamp="2026-02-03T00:00:00Z")

    def test_the_most_recent_value_becomes_active(self) -> None:
        self.record_location()
        active = self.memory.resolver.active_state("user")

        self.assertEqual(active["user::location"].value, "Valencia")

    def test_the_older_value_is_flagged_historical_and_kept(self) -> None:
        self.record_location()
        timeline = self.memory.resolver.timeline("user", "location")

        self.assertEqual([f.value for f in timeline], ["Madrid", "Valencia"])
        self.assertIs(timeline[0].status, LeafStatus.HISTORICAL)
        self.assertEqual(timeline[0].superseded_by, timeline[1].fact_id)
        self.assertEqual(timeline[0].valid_to, timeline[1].timestamp)
        self.assertIs(timeline[1].status, LeafStatus.ACTIVE)

    def test_out_of_order_insertion_still_resolves_by_date(self) -> None:
        self.memory.record_fact("user", "location", "Valencia", timestamp="2026-02-03T00:00:00Z")
        self.memory.record_fact("user", "location", "Madrid", timestamp="2024-03-11T00:00:00Z")

        self.assertEqual(self.memory.resolver.active_state()["user::location"].value, "Valencia")

    def test_repeating_a_value_is_a_confirmation_not_a_contradiction(self) -> None:
        self.memory.record_fact("user", "stack", "Java", timestamp="2024-01-01T00:00:00Z")
        self.memory.record_fact("user", "stack", "Java", timestamp="2025-01-01T00:00:00Z")

        self.assertEqual(self.memory.conflicts(), [])

    def test_the_notice_names_both_years_and_both_values(self) -> None:
        self.record_location()
        notice = self.memory.conflicts()[0].render_notice()

        self.assertIn("2024", notice)
        self.assertIn("2026", notice)
        self.assertIn("Madrid", notice)
        self.assertIn("Valencia", notice)

    def test_timeline_query_answers_what_was_true_back_then(self) -> None:
        self.record_location()
        past = self.memory.resolver.state_at("2024-12-31")
        now = self.memory.resolver.state_at("2026-12-31")

        self.assertEqual(past["user::location"].value, "Madrid")
        self.assertEqual(now["user::location"].value, "Valencia")

    def test_recording_the_same_assertion_twice_is_idempotent(self) -> None:
        self.memory.record_fact("user", "location", "Madrid", timestamp="2024-03-11T00:00:00Z")
        self.memory.record_fact("user", "location", "Madrid", timestamp="2024-03-11T00:00:00Z")

        self.assertEqual(len(self.memory.resolver.facts), 1)

    def test_facts_survive_a_reload_from_disk(self) -> None:
        self.record_location()

        from ihmt import IHMT

        reopened = IHMT(self.workspace)
        self.assertEqual(reopened.resolver.active_state()["user::location"].value, "Valencia")


class ExtractionTests(StoreTestCase):
    """Facts are harvested from real documents during ingestion."""

    def setUp(self) -> None:
        super().setUp()
        self.ingest_example("journal_personal.txt")
        self.memory.flush()

    def test_the_journal_yields_the_expected_contradictions(self) -> None:
        attributes = {c.attribute for c in self.memory.conflicts(subject="user")}

        self.assertIn("location", attributes)
        self.assertIn("stack", attributes)
        self.assertIn("employer", attributes)

    def test_extracted_facts_are_dated_by_the_entry_not_the_ingest(self) -> None:
        timeline = self.memory.resolver.timeline("user", "location")

        self.assertEqual(timeline[0].value, "Madrid")
        self.assertTrue(timeline[0].timestamp.startswith("2024-03-11"))
        self.assertEqual(timeline[-1].value, "Valencia")
        self.assertTrue(timeline[-1].timestamp.startswith("2026-02-03"))

    def test_a_superseded_leaf_is_annotated_with_its_notice(self) -> None:
        historical = self.memory.resolver.timeline("user", "location")[0]
        leaf = self.memory.get_leaf(historical.source_leaf_id)

        self.assertIn("superseded_facts", leaf.extra)
        self.assertIn("Valencia", leaf.extra["superseded_facts"][0]["notice"])

    def test_retrieving_outdated_material_surfaces_the_correction(self) -> None:
        response = self.memory.search("piso Lavapiés contrato Madrid", top_k=3)
        notices = [notice for result in response.results for notice in result.notices]

        self.assertTrue(notices, "an answer built on superseded material must carry its notice")
        self.assertTrue(any("Valencia" in notice for notice in notices))

    def test_clinical_facts_are_attributed_to_the_patient(self) -> None:
        self.ingest_example("clinical_history.txt")
        self.memory.flush()
        conflicts = self.memory.conflicts(subject="Marta Ruiz Belmonte")

        self.assertTrue(conflicts)
        diagnoses = self.memory.resolver.timeline("Marta Ruiz Belmonte", "diagnosis")
        self.assertEqual(diagnoses[-1].value, "migraña sin aura")
        self.assertIs(diagnoses[0].status, LeafStatus.HISTORICAL)

    def test_rebuilding_the_timeline_reproduces_it(self) -> None:
        before = len(self.memory.resolver.facts)
        after = self.memory.resolver.rebuild()

        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
