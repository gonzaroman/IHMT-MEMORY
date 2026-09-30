"""Block-integrity guarantees of the type-aware splitters.

The central invariant of IHMT: chunking reorganizes a document, it never
mutilates it. Every test here is a statement of that invariant for one material
type.
"""

from __future__ import annotations

import ast
import unittest

from ihmt.chunkers import BraceScanner, CodeAwareChunker, NarrativeChunker, ProcessChunker, TemporalChunker
from ihmt.chunkers.narrative import split_sentences_exact
from ihmt.chunkers.temporal import extract_date
from ihmt.config import IHMTConfig
from ihmt.models import DataType

from .base import EXAMPLES, StoreTestCase


class CodeChunkingTests(StoreTestCase):
    """Java and Python are cut on syntax, never on character offsets."""

    def test_java_chunks_reconstruct_the_source_byte_for_byte(self) -> None:
        source = self.example("InventoryService.java").read_text(encoding="utf-8")
        chunker = CodeAwareChunker(self.memory.config, language="java")
        chunks = chunker.chunk(source, source="InventoryService.java")

        self.assertGreater(len(chunks), 1, "the file should split into several chunks")
        self.assertEqual("".join(c.text for c in chunks), source)

    def test_no_java_chunk_boundary_falls_inside_a_method_body(self) -> None:
        source = self.example("InventoryService.java").read_text(encoding="utf-8")
        chunks = CodeAwareChunker(self.memory.config, language="java").chunk(
            source, source="InventoryService.java"
        )
        depths = BraceScanner(source).depth_at_line_ends()

        for chunk in chunks[:-1]:
            depth = depths.get(chunk.end_line, 0)
            self.assertLessEqual(
                depth,
                1,
                f"boundary at line {chunk.end_line} sits at brace depth {depth} "
                f"— inside a method body",
            )

    def test_method_signatures_and_their_bodies_stay_together(self) -> None:
        source = self.example("InventoryService.java").read_text(encoding="utf-8")
        chunks = CodeAwareChunker(self.memory.config, language="java").chunk(
            source, source="InventoryService.java"
        )
        for chunk in chunks:
            # Within one chunk, depth may open the enclosing class (+1) or close
            # it (-1), but a method opened inside must also close inside.
            self.assertIn(
                chunk.text.count("{") - chunk.text.count("}"),
                (-1, 0, 1),
                f"unbalanced chunk: {chunk.title}",
            )

    def test_python_chunks_reconstruct_and_stay_parseable(self) -> None:
        source = self.example("pricing_engine.py").read_text(encoding="utf-8")
        chunks = CodeAwareChunker(self.memory.config, language="python").chunk(
            source, source="pricing_engine.py"
        )

        self.assertEqual("".join(c.text for c in chunks), source)
        for chunk in chunks:
            # Every chunk is a run of complete top-level statements, so each one
            # parses on its own.
            ast.parse(chunk.text)

    def test_a_class_larger_than_the_budget_splits_per_method_not_mid_block(self) -> None:
        config = IHMTConfig(base_dir=self.workspace, target_tokens=60, max_tokens=120)
        source = self.example("InventoryService.java").read_text(encoding="utf-8")
        chunks = CodeAwareChunker(config, language="java").chunk(source, source="InventoryService.java")

        self.assertEqual("".join(c.text for c in chunks), source)
        self.assertGreater(len(chunks), 4)
        titles = " ".join(c.title for c in chunks)
        self.assertIn("reserveStock", titles, "members should be addressable after a container split")

    def test_an_indivisible_oversized_block_is_kept_whole_and_flagged(self) -> None:
        config = IHMTConfig(base_dir=self.workspace, target_tokens=10, max_tokens=20)
        source = self.example("InventoryService.java").read_text(encoding="utf-8")
        chunks = CodeAwareChunker(config, language="java").chunk(source, source="InventoryService.java")

        oversized = [c for c in chunks if c.oversized]
        self.assertTrue(oversized, "a 20-token ceiling must produce oversized blocks")
        self.assertEqual("".join(c.text for c in chunks), source, "oversized blocks are still never cut")
        self.assertIn("exceeds max_tokens", oversized[0].meta.get("oversized_reason", ""))

    def test_broken_syntax_degrades_gracefully_instead_of_raising(self) -> None:
        broken = "def ok():\n    return 1\n\ndef broken(:\n    oops\n"
        chunks = CodeAwareChunker(self.memory.config, language="python").chunk(broken, source="broken.py")

        self.assertTrue(chunks)
        self.assertEqual("".join(c.text for c in chunks), broken)

    def test_braces_inside_strings_and_comments_do_not_fool_the_scanner(self) -> None:
        source = (
            "package demo;\n"
            "public class Tricky {\n"
            '    private String a = "a { not a block";\n'
            "    // } neither is this\n"
            "    /* nor { this one */\n"
            "    void method() {\n"
            '        System.out.println("}");\n'
            "    }\n"
            "}\n"
        )
        ends = BraceScanner(source).unit_end_lines()
        self.assertEqual(ends[-1], 9, "the class must close on its real closing brace")


class NarrativeChunkingTests(StoreTestCase):
    """Prose is cut at paragraphs and scenes."""

    def test_narrative_chunks_reconstruct_the_source(self) -> None:
        source = self.example("narrative_es.txt").read_text(encoding="utf-8")
        chunks = NarrativeChunker(self.memory.config).chunk(source, source="narrative_es.txt")

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(c.text for c in chunks), source)

    def test_a_scene_marker_always_starts_a_new_chunk(self) -> None:
        source = self.example("narrative_es.txt").read_text(encoding="utf-8")
        chunks = NarrativeChunker(self.memory.config).chunk(source, source="narrative_es.txt")

        openings = [c.text.strip().splitlines()[0].strip() for c in chunks if c.text.strip()]
        self.assertTrue(
            any(opening.startswith("Capítulo 2") for opening in openings),
            f"a chapter heading should open a chunk; got {openings}",
        )

    def test_paragraphs_are_atomic_under_a_generous_budget(self) -> None:
        source = "Uno. Dos.\n\nTres. Cuatro.\n\nCinco.\n"
        chunks = NarrativeChunker(self.memory.config).chunk(source, source="p.txt")

        self.assertEqual(len(chunks), 1, "short paragraphs pack into one leaf")
        self.assertEqual(chunks[0].text, source)

    def test_sentence_split_is_lossless(self) -> None:
        text = "Primera frase. Segunda frase; tercera. ¿Y una pregunta? Sí.\n"
        self.assertEqual("".join(split_sentences_exact(text)), text)


class TemporalChunkingTests(StoreTestCase):
    """Journals and clinical notes are cut at dated entries."""

    def test_each_journal_date_becomes_its_own_leaf(self) -> None:
        source = self.example("journal_personal.txt").read_text(encoding="utf-8")
        chunks = TemporalChunker(self.memory.config).chunk(source, source="journal.txt")

        self.assertEqual("".join(c.text for c in chunks), source)
        stamps = [c.timestamp[:10] for c in chunks if c.timestamp]
        self.assertEqual(len(stamps), len(set(stamps)), "a leaf must not mix two dates")
        self.assertIn("2024-07-22", stamps)
        self.assertIn("2026-02-03", stamps)

    def test_the_entry_date_becomes_the_leaf_timestamp(self) -> None:
        report = self.ingest_example("journal_personal.txt")
        stamps = {leaf.timestamp[:10] for leaf in self.leaves_of(report)}

        self.assertIn("2024-03-11", stamps)
        self.assertNotIn("", stamps)

    def test_clinical_encounters_are_not_merged(self) -> None:
        source = self.example("clinical_history.txt").read_text(encoding="utf-8")
        chunks = TemporalChunker(self.memory.config, clinical=True).chunk(source, source="hc.txt")

        self.assertEqual("".join(c.text for c in chunks), source)
        self.assertGreaterEqual(len(chunks), 4, "four encounters, four leaves")

    def test_date_parsing_handles_both_locales(self) -> None:
        cases = {
            "consulta del 2024-05-14": "2024-05-14",
            "el 22 de julio de 2024 fuimos": "2024-07-22",
            "on August 30, 2026 we": "2026-08-30",
            "fecha 03/02/2026 (día primero)": "2026-02-03",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual((extract_date(text) or "")[:10], expected)

    def test_text_without_dates_still_chunks(self) -> None:
        chunks = TemporalChunker(self.memory.config).chunk("sin fechas\n\nsegundo párrafo\n", source="x.txt")
        self.assertTrue(chunks)


class ProcessChunkingTests(StoreTestCase):
    """Procedures keep their steps attached to their headings."""

    def test_recipe_sections_reconstruct_and_keep_lists_intact(self) -> None:
        source = self.example("recipe_paella.md").read_text(encoding="utf-8")
        chunks = ProcessChunker(self.memory.config).chunk(source, source="recipe.md")

        self.assertEqual("".join(c.text for c in chunks), source)
        ingredient_chunks = [c for c in chunks if "600 g de arroz bomba" in c.text]
        self.assertEqual(len(ingredient_chunks), 1)
        self.assertIn("2,4 litros de caldo", ingredient_chunks[0].text, "the list must not be split")


class ChunkerSelectionTests(StoreTestCase):
    """The right splitter is chosen for the right material."""

    def test_every_data_type_maps_to_a_working_chunker(self) -> None:
        from ihmt.chunkers import get_chunker

        for data_type in DataType:
            with self.subTest(data_type=data_type):
                chunker = get_chunker(data_type, self.memory.config, source="sample.txt")
                chunks = chunker.chunk("Primera línea.\n\nSegunda línea.\n", source="sample.txt")
                self.assertEqual("".join(c.text for c in chunks), "Primera línea.\n\nSegunda línea.\n")

    def test_empty_input_produces_no_chunks(self) -> None:
        from ihmt.chunkers import get_chunker

        chunker = get_chunker(DataType.GENERIC, self.memory.config)
        self.assertEqual(chunker.chunk("   \n\n  "), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
