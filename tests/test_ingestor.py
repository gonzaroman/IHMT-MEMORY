"""Type/domain detection, leaf metadata and the CLI surface."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from ihmt.detectors import DomainDetector
from ihmt.models import DataType, IHMTError

from .base import EXAMPLES, StoreTestCase


class DetectionTests(StoreTestCase):
    """Material is classified before it is split."""

    def setUp(self) -> None:
        super().setUp()
        self.detector = DomainDetector()

    def test_each_example_is_classified_as_expected(self) -> None:
        expectations = {
            "InventoryService.java": (DataType.CODE, "software.java"),
            "pricing_engine.py": (DataType.CODE, "software.python"),
            "narrative_es.txt": (DataType.NARRATIVE, "literature.narrative"),
            "journal_personal.txt": (DataType.PERSONAL, "personal"),
            "clinical_history.txt": (DataType.CLINICAL, "medicine.clinical"),
            "recipe_paella.md": (DataType.PROCESS, "process.cooking"),
        }
        for name, (data_type, domain) in expectations.items():
            with self.subTest(name=name):
                path = EXAMPLES / name
                detection = self.detector.detect(path.read_text(encoding="utf-8"), source=str(path))
                self.assertIs(detection.data_type, data_type)
                self.assertEqual(detection.domain, domain)

    def test_code_is_recognized_without_a_filename(self) -> None:
        snippet = "public class Foo {\n    void bar() {\n        return;\n    }\n}\n"
        detection = self.detector.detect(snippet)
        self.assertIs(detection.data_type, DataType.CODE)

    def test_explicit_overrides_win(self) -> None:
        detection = self.detector.detect(
            "cualquier cosa", source="x.java", data_type=DataType.NARRATIVE, domain="mi.dominio"
        )
        self.assertIs(detection.data_type, DataType.NARRATIVE)
        self.assertEqual(detection.domain, "mi.dominio")
        self.assertEqual(detection.confidence, 1.0)

    def test_unclassifiable_text_falls_back_to_generic(self) -> None:
        detection = self.detector.detect("xyz abc\n\nqwe rty\n", source="mystery.dat")
        self.assertIs(detection.data_type, DataType.GENERIC)
        self.assertTrue(detection.domain)


class IngestionTests(StoreTestCase):
    """Every leaf carries the metadata the retrieval layer relies on."""

    def test_leaf_metadata_is_complete(self) -> None:
        report = self.ingest_example("journal_personal.txt")
        for leaf in self.leaves_of(report):
            self.assertTrue(leaf.leaf_id)
            self.assertTrue(leaf.timestamp)
            self.assertTrue(leaf.domain)
            self.assertTrue(leaf.title)
            self.assertTrue(leaf.tags)
            self.assertTrue(leaf.checksum.startswith("sha256:"))
            self.assertGreater(leaf.token_estimate, 0)
            self.assertGreaterEqual(leaf.span.end_line, leaf.span.start_line)
            self.assertIn(f"type:{leaf.data_type.value.lower()}", leaf.tags)

    def test_leaves_land_in_a_directory_named_after_their_domain(self) -> None:
        report = self.ingest_example("recipe_paella.md")
        for leaf_id in report.leaf_ids:
            path = self.memory.store.leaf_path(leaf_id, report.domain)
            self.assertTrue(path.exists())
            self.assertEqual(path.parent.name, report.domain)

    def test_an_extracted_method_records_its_class_context(self) -> None:
        report = self.ingest_example("InventoryService.java")
        contexts = [leaf.extra.get("context", "") for leaf in self.leaves_of(report)]
        self.assertTrue(
            any("class InventoryService" in context for context in contexts),
            "a method lifted out of a class must remember its enclosing header",
        )

    def test_custom_tags_and_domain_are_applied(self) -> None:
        report = self.memory.ingest_text(
            "Contenido de prueba para el dominio propio.",
            source="custom.txt",
            domain="mi.proyecto",
            tags=["sprint-4"],
        )
        leaf = self.memory.get_leaf(report.leaf_ids[0])
        self.assertEqual(leaf.domain, "mi.proyecto")
        self.assertIn("sprint-4", leaf.tags)

    def test_ingesting_a_directory_skips_binaries_and_vcs(self) -> None:
        reports = self.memory.ingest_directory(EXAMPLES)
        self.assertGreaterEqual(len(reports), 6)
        self.assertTrue(all(r.chunks > 0 for r in reports))

    def test_a_missing_file_raises_a_typed_error(self) -> None:
        with self.assertRaises(IHMTError):
            self.memory.ingest_file(self.workspace / "nope.txt")

    def test_empty_input_produces_no_leaves(self) -> None:
        report = self.memory.ingest_text("   \n", source="empty.txt")
        self.assertEqual(report.chunks, 0)


class CliTests(StoreTestCase):
    """The CLI wires the components together end to end."""

    def run_cli(self, argv: list[str]) -> str:
        import main as cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(argv)
        self.assertEqual(code, 0, buffer.getvalue())
        return buffer.getvalue()

    def test_ingest_search_and_conflicts_from_the_command_line(self) -> None:
        base = ["--path", str(self.workspace)]
        self.run_cli(base + ["ingest", str(EXAMPLES / "journal_personal.txt")])
        self.run_cli(base + ["consolidate", "--force"])

        search = self.run_cli(base + ["search", "vacaciones Benidorm"])
        self.assertIn("Benidorm", search)

        conflicts = self.run_cli(base + ["conflicts"])
        self.assertIn("Valencia", conflicts)

        tree = self.run_cli(base + ["tree"])
        self.assertIn("root.json", tree)

        stats = self.run_cli(base + ["stats", "--json"])
        self.assertIn('"leaves"', stats)

    def test_the_clue_loop_is_reachable_non_interactively(self) -> None:
        base = ["--path", str(self.workspace)]
        self.run_cli(base + ["ingest", str(EXAMPLES / "journal_personal.txt")])
        self.run_cli(base + ["consolidate", "--force"])

        output = self.run_cli(base + ["ask", "Luis", "--clue", "vacaciones en Benidorm"])
        self.assertIn("Benidorm", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
