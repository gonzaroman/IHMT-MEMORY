"""Tests for the project index and the retrieval features it relies on.

Covers symbol-level chunking, checksum sync (add / change / remove), the map,
scoped lookups, stale-leaf handling on re-ingest and batched catalog writes.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from .base import StoreTestCase

from ihmt import IHMT, IHMTConfig, ProjectIndex  # noqa: E402  (base sets up the path)
from ihmt.chunkers.code import CodeAwareChunker  # noqa: E402
from ihmt.semantic_navigator import looks_like_code, looks_like_test  # noqa: E402

PEDIDO = """\
package demo.domain;

import java.util.ArrayList;
import java.util.List;

public class Pedido {

    private final List<String> lineas = new ArrayList<>();
    private String estado = "BORRADOR";

    public void anadirLinea(String producto, int cantidad) {
        if (!"BORRADOR".equals(estado)) {
            throw new IllegalStateException("Solo se pueden anadir lineas a un pedido en borrador");
        }
        for (int i = 0; i < cantidad; i++) {
            lineas.add(producto);
        }
    }

    public void confirmar() {
        if (lineas.isEmpty()) {
            throw new IllegalStateException("No se puede confirmar un pedido vacio: importe minimo no alcanzado");
        }
        estado = "CONFIRMADO";
    }

    public void cancelar() {
        if ("CANCELADO".equals(estado)) {
            throw new IllegalStateException("El pedido ya esta cancelado y no se puede cancelar otra vez");
        }
        estado = "CANCELADO";
    }
}
"""

ADAPTER = """\
package demo.adapter.out.persistence;

public class PedidoJpaAdapter {

    public void guardar(Object pedido) {
        System.out.println("guardando el pedido en la tabla de pedidos con JPA " + pedido);
        System.out.println("una segunda linea para que el metodo tenga cierto tamano " + pedido);
    }

    public Object buscarPorId(String id) {
        System.out.println("buscando el pedido por su identificador en la base de datos " + id);
        return null;
    }
}
"""

PEDIDO_TEST = """\
package demo.domain;

public class PedidoTest {

    void confirmar_un_pedido_vacio_falla() {
        Pedido pedido = new Pedido();
        try { pedido.confirmar(); } catch (IllegalStateException esperado) { }
    }
}
"""


class ProjectFixture(unittest.TestCase):
    """A tiny Java project plus an index cache, both in temporary directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.project = base / "shop"
        self.cache = base / "cache"
        self.write("src/main/java/demo/domain/Pedido.java", PEDIDO)
        self.write("src/main/java/demo/adapter/out/persistence/PedidoJpaAdapter.java", ADAPTER)
        self.write("src/test/java/demo/domain/PedidoTest.java", PEDIDO_TEST)
        self.write("target/classes/Generado.java", "class Generado {}\n")
        self.write("out/Tmp.java", "class Tmp {}\n")
        self.index = ProjectIndex.for_project(self.project, self.cache)

    def write(self, relative: str, text: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def touch_later(self, path: Path, text: str) -> None:
        """Rewrite a file making sure its mtime moves, even on coarse filesystems."""
        path.write_text(text, encoding="utf-8")
        later = time.time() + 5
        os.utime(path, (later, later))


class SymbolChunkingTests(unittest.TestCase):
    def test_symbol_mode_gives_one_leaf_per_method_and_reconstructs_the_file(self) -> None:
        config = IHMTConfig(target_tokens=60, max_tokens=3000, min_tokens=1, code_chunk_mode="symbol")
        chunks = CodeAwareChunker(config).chunk(PEDIDO, source="Pedido.java")

        self.assertEqual("".join(c.text for c in chunks), PEDIDO)
        titles = [c.title for c in chunks]
        for method in ("Pedido.anadirLinea", "Pedido.confirmar", "Pedido.cancelar"):
            self.assertIn(method, titles)

    def test_pack_mode_is_unchanged_by_default(self) -> None:
        config = IHMTConfig(target_tokens=2000, max_tokens=3000)
        chunks = CodeAwareChunker(config).chunk(PEDIDO, source="Pedido.java")
        self.assertEqual("".join(c.text for c in chunks), PEDIDO)
        self.assertLessEqual(len(chunks), 2)

    def test_an_unknown_chunk_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IHMTConfig(code_chunk_mode="words").validate()


class PathHeuristicsTests(unittest.TestCase):
    def test_tests_and_code_are_recognized(self) -> None:
        self.assertTrue(looks_like_test("src/test/java/a/PedidoTest.java"))
        self.assertTrue(looks_like_test("tests/test_navigator.py"))
        self.assertTrue(looks_like_test("web/app.spec.ts"))
        self.assertFalse(looks_like_test("src/main/java/a/Pedido.java"))
        self.assertTrue(looks_like_code("ihmt/api.py"))
        self.assertFalse(looks_like_code("notes/diario.txt"))


class ProjectSyncTests(ProjectFixture):
    def test_first_sync_indexes_sources_and_skips_build_output(self) -> None:
        report = self.index.sync()

        self.assertEqual(report.added, 3)
        project_map = self.index.map(detail="files")
        self.assertIn("Pedido.java", project_map)
        self.assertIn("PedidoJpaAdapter.java", project_map)  # adapter/out is a package, not build output
        self.assertNotIn("Generado.java", project_map)  # target/ is skipped at any depth
        self.assertNotIn("Tmp.java", project_map)  # out/ is skipped at the root

    def test_a_second_sync_with_no_changes_writes_nothing(self) -> None:
        self.index.sync()
        report = self.index.sync()
        self.assertEqual(report.touched, 0)

    def test_a_changed_file_is_reindexed_and_old_code_is_never_served(self) -> None:
        self.index.sync()
        path = self.project / "src/main/java/demo/domain/Pedido.java"
        self.touch_later(path, PEDIDO.replace("confirmar()", "aprobar()"))

        report = self.index.sync()
        self.assertEqual(report.changed, 1)

        result = self.index.find("Pedido aprobar pedido vacio")
        self.assertEqual(result.status, "found")
        self.assertIn("aprobar", result.hits[0].content)
        symbols = self.index.map(detail="symbols")
        self.assertIn("aprobar", symbols)
        self.assertNotIn("confirmar", symbols)

    def test_a_removed_file_disappears_from_map_and_lookups(self) -> None:
        self.index.sync()
        (self.project / "src/main/java/demo/adapter/out/persistence/PedidoJpaAdapter.java").unlink()

        report = self.index.sync()
        self.assertEqual(report.removed, 1)
        self.assertNotIn("PedidoJpaAdapter", self.index.map())
        result = self.index.find("PedidoJpaAdapter guardar buscarPorId")
        for hit in result.hits:
            self.assertNotIn("PedidoJpaAdapter", hit.source)


class ProjectLookupTests(ProjectFixture):
    def setUp(self) -> None:
        super().setUp()
        self.index.sync()

    def test_find_returns_just_the_method_with_its_line_range(self) -> None:
        result = self.index.find("Pedido confirmar importe minimo")

        self.assertEqual(result.status, "found")
        hit = result.hits[0]
        self.assertTrue(hit.source.endswith("Pedido.java"))
        self.assertIn("confirmar", hit.title)
        self.assertNotIn("cancelar()", hit.content)
        rendered = hit.render()
        self.assertTrue(rendered.startswith(f"{hit.source}:{hit.start_line}-{hit.end_line}"))

    def test_scope_main_excludes_tests_and_scope_test_keeps_only_them(self) -> None:
        main = self.index.find("confirmar pedido vacio", scope="main")
        tests = self.index.find("confirmar pedido vacio", scope="test")

        for hit in main.hits:
            self.assertNotIn("/test/", hit.source)
        self.assertTrue(tests.hits and all("/test/" in hit.source for hit in tests.hits))

    def test_naming_the_file_prefers_it(self) -> None:
        result = self.index.find("PedidoJpaAdapter guardar pedido")
        self.assertEqual(result.status, "found")
        self.assertTrue(result.hits[0].source.endswith("PedidoJpaAdapter.java"))

    def test_the_symbol_map_lists_methods_with_lines(self) -> None:
        symbols = self.index.map(detail="symbols")
        self.assertRegex(symbols, r"confirmar \d+-\d+")
        self.assertIn("~", symbols)

    def test_a_query_that_names_a_symbol_is_resolved_directly(self) -> None:
        self.write("src/main/java/demo/v1/Pedido.java", PEDIDO.replace("package demo.domain;", "package demo.v1;"))
        self.index.sync()
        result = self.index.find("Pedido.cancelar")

        self.assertEqual(result.status, "found")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.hits[0].title, "Pedido.cancelar")
        self.assertNotIn("/v1/", result.hits[0].source)  # the legacy copy loses

    def test_the_symbol_map_does_not_list_markup_fragments(self) -> None:
        self.write("docs/guia.html", "<h1>Guía</h1>\n<p>Texto</p>\n" * 40)
        self.write("docs/notas.md", "# Notas\n\nUna nota.\n" * 20)
        self.index.sync()
        line = next(l for l in self.index.map(detail="symbols").splitlines() if "guia.html" in l)
        self.assertNotIn(":", line.split("~", 1)[1])


class StaleLeafTests(StoreTestCase):
    def test_reingesting_a_changed_file_demotes_its_previous_version(self) -> None:
        path = self.workspace / "Nota.java"
        path.write_text(PEDIDO, encoding="utf-8")
        first = self.memory.ingest_file(path)
        path.write_text(PEDIDO.replace("confirmar", "aprobar"), encoding="utf-8")
        second = self.memory.ingest_file(path)

        self.assertGreater(second.superseded, 0)
        response = self.memory.search("confirmar pedido vacio")
        for result in response.results:
            self.assertNotIn(result.leaf_id, set(first.leaf_ids) - set(second.leaf_ids))
        old = set(first.leaf_ids) - set(second.leaf_ids)
        self.assertTrue(all(self.memory.get_leaf(i).status.value == "HISTORICAL" for i in old))

    def test_reingesting_an_unchanged_file_demotes_nothing(self) -> None:
        path = self.workspace / "Nota.java"
        path.write_text(PEDIDO, encoding="utf-8")
        self.memory.ingest_file(path)
        again = self.memory.ingest_file(path)
        self.assertEqual(again.superseded, 0)

    def test_notes_saved_as_text_never_demote_each_other(self) -> None:
        self.memory.ingest_text("2026-01-01: Primera nota sobre el proyecto de pedidos.", source="mcp://claude-code")
        second = self.memory.ingest_text("2026-01-02: Segunda nota distinta.", source="mcp://claude-code")
        self.assertEqual(second.superseded, 0)


class CatalogBatchTests(StoreTestCase):
    def test_a_batch_writes_the_catalog_once_and_keeps_every_entry(self) -> None:
        store = self.memory.store
        writes = []
        original = store.write_json

        def counting(path, payload):  # type: ignore[no-untyped-def]
            if path == store.config.catalog_path:
                writes.append(path)
            return original(path, payload)

        store.write_json = counting  # type: ignore[method-assign]
        memory = IHMT(self.workspace, auto_consolidate=False)
        memory.store.write_json = counting  # type: ignore[method-assign]
        report = memory.ingest_text(PEDIDO * 3, source="Grande.java")

        self.assertGreater(report.chunks, 1)
        self.assertLessEqual(len(writes), 2)
        memory.store.reload()
        for leaf_id in report.leaf_ids:
            self.assertIn(leaf_id, memory.store.catalog.entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
