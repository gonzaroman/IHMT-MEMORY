"""Tests for the MCP wrapper.

Skipped automatically when the MCP SDK is not installed, so the core suite stays
dependency-free::

    python -m unittest discover -v          # skips these
    .venv/bin/python -m unittest discover    # runs them
"""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path

from .base import PROJECT_ROOT

try:  # the SDK is an optional extra
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_MCP = False


@unittest.skipUnless(HAS_MCP, "MCP SDK not installed (pip install 'mcp[cli]')")
class McpToolTests(unittest.TestCase):
    """The two published tools, exercised through the server object."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        # The server reads IHMT_HOME at import time, so point it at a fresh
        # directory and reload the module for each test.
        self._previous_home = os.environ.get("IHMT_HOME")
        os.environ["IHMT_HOME"] = self._tmp.name
        self.addCleanup(self._restore_home)

        import mcp_server

        self.srv = importlib.reload(mcp_server)

    def _restore_home(self) -> None:
        if self._previous_home is None:
            os.environ.pop("IHMT_HOME", None)
        else:
            os.environ["IHMT_HOME"] = self._previous_home

    # ------------------------------------------------------------ discovery
    def test_both_tools_are_published_with_usable_schemas(self) -> None:
        tools = {t.name: t for t in asyncio.run(self.srv.server.list_tools())}

        self.assertEqual(
            set(tools),
            {
                "search_memory", "save_memory",
                "project_map", "find_code", "read_file",
                "note", "recall", "digest_output",
            },
        )

        search = tools["search_memory"].input_schema
        self.assertEqual(search["required"], ["query"])
        self.assertIn("clue", search["properties"])

        save = tools["save_memory"].input_schema
        self.assertEqual(save["required"], ["content"])
        self.assertEqual(save["properties"]["domain"]["default"], "general")
        self.assertEqual(save["properties"]["content_type"]["default"], "auto")

        for tool in tools.values():
            self.assertTrue(tool.description and len(tool.description) > 100, "tools need usable descriptions")

    def test_the_store_is_created_on_first_use(self) -> None:
        self.srv.save_memory("Una nota cualquiera que merece recordarse.")
        self.assertTrue((Path(self._tmp.name) / "ihmt_memory" / "root.json").exists())

    # ------------------------------------------------------------ save_memory
    def test_saving_reports_classification_and_growth(self) -> None:
        output = self.srv.save_memory(
            "public class Foo {\n    void bar() {\n        return;\n    }\n}\n"
        )
        self.assertIn("CODE", output)
        self.assertIn("Memory now holds", output)

    def test_an_explicit_domain_and_type_are_honoured(self) -> None:
        self.srv.save_memory("Cualquier texto.", domain="mi.proyecto", content_type="narrative")
        memory = self.srv.get_memory()
        self.assertIn("mi.proyecto", memory.stats()["domains"])

    def test_an_unknown_content_type_is_rejected_with_the_valid_list(self) -> None:
        output = self.srv.save_memory("x", content_type="poetry")
        self.assertIn("Unknown content_type", output)
        self.assertIn("narrative", output)

    def test_empty_content_is_refused(self) -> None:
        self.assertIn("Nothing to save", self.srv.save_memory("   \n "))

    def test_a_superseding_save_reports_the_contradiction(self) -> None:
        self.srv.save_memory("2024-03-11: Vivo en Madrid, en Lavapiés.")
        output = self.srv.save_memory("2026-02-03: Me he mudado a Valencia.")

        self.assertIn("updates something remembered earlier", output)
        self.assertIn("Madrid", output)
        self.assertIn("Valencia", output)

    # ---------------------------------------------------------- search_memory
    def test_search_returns_the_stored_text_and_its_date(self) -> None:
        self.srv.save_memory("2026-06-18: Migramos a Go y Postgres el backend de facturación.")
        output = self.srv.search_memory("migración backend Go Postgres")

        self.assertIn("Go y Postgres", output)
        self.assertIn("2026-06-18", output)

    def test_search_on_an_empty_store_says_so(self) -> None:
        self.assertIn("No memory found", self.srv.search_memory("cualquier cosa"))

    def test_an_unrelated_query_reports_nothing_rather_than_guessing(self) -> None:
        self.srv.save_memory("2024-07-22: Vacaciones en Benidorm con Luis, mi primo.")
        output = self.srv.search_memory("kubernetes helm charts rollout")

        self.assertIn("No memory found", output)
        self.assertNotIn("Benidorm", output)

    def test_an_ambiguous_query_asks_for_a_clue_instead_of_guessing(self) -> None:
        self.srv.save_memory("2024-07-22: Vacaciones en Benidorm con Luis, mi primo.")
        self.srv.save_memory("2024-11-30: Luis Marín revisó el pull request de reserveStock.")
        self.srv.save_memory("2025-01-15: Comida familiar con mi primo Luis y los niños.")

        output = self.srv.search_memory("Luis")
        self.assertTrue(output.startswith("AMBIGUOUS"))
        self.assertIn("clue", output)

    def test_a_clue_resolves_the_ambiguity(self) -> None:
        self.srv.save_memory("2024-07-22: Vacaciones en Benidorm con Luis, mi primo.")
        self.srv.save_memory("2024-11-30: Luis Marín revisó el pull request de reserveStock.")
        self.srv.save_memory("2025-01-15: Comida familiar con mi primo Luis y los niños.")

        output = self.srv.search_memory("Luis", clue="vacaciones en Benidorm")
        self.assertFalse(output.startswith("AMBIGUOUS"))
        self.assertIn("Benidorm", output)

    def test_outdated_matches_carry_their_correction(self) -> None:
        self.srv.save_memory("2024-03-11: Vivo en Madrid, en el barrio de Lavapiés.")
        self.srv.save_memory("2026-02-03: Me he mudado a Valencia, a Ruzafa.")

        output = self.srv.search_memory("Madrid Lavapiés piso")
        self.assertIn("OUTDATED", output)
        self.assertIn("Valencia", output)

    def test_an_empty_query_is_refused(self) -> None:
        self.assertIn("non-empty", self.srv.search_memory("  "))


@unittest.skipUnless(HAS_MCP, "MCP SDK not installed (pip install 'mcp[cli]')")
class McpTokenSavingToolTests(unittest.TestCase):
    """The compact search format, the project tools and the session tools."""

    JAVA = (
        "package demo;\n\npublic class Pedido {\n\n"
        "    public void confirmar() {\n"
        "        if (lineas == 0) { throw new IllegalStateException(\"importe minimo no alcanzado en el pedido\"); }\n"
        "        estado = \"CONFIRMADO\";\n    }\n\n"
        "    public void cancelar() {\n"
        "        if (cancelado) { throw new IllegalStateException(\"el pedido ya estaba cancelado antes\"); }\n"
        "        cancelado = true;\n    }\n"
        "    private int lineas;\n    private boolean cancelado;\n    private String estado;\n}\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._previous_home = os.environ.get("IHMT_HOME")
        os.environ["IHMT_HOME"] = str(Path(self._tmp.name) / "home")
        self.addCleanup(self._restore_home)
        import mcp_server

        self.srv = importlib.reload(mcp_server)
        self.project = Path(self._tmp.name) / "shop"
        source = self.project / "src" / "main" / "java" / "demo" / "Pedido.java"
        source.parent.mkdir(parents=True)
        source.write_text(self.JAVA, encoding="utf-8")
        self.source = source

    def _restore_home(self) -> None:
        if self._previous_home is None:
            os.environ.pop("IHMT_HOME", None)
        else:
            os.environ["IHMT_HOME"] = self._previous_home

    # ------------------------------------------------------ compact search
    def test_compact_search_keeps_date_and_drops_bookkeeping(self) -> None:
        self.srv.save_memory("2026-06-18: Migramos a Go y Postgres el backend de facturación.")
        compact = self.srv.search_memory("migración backend Go Postgres")
        full = self.srv.search_memory("migración backend Go Postgres", detail="full")

        self.assertIn("2026-06-18", compact)
        self.assertIn("Go y Postgres", compact)
        self.assertNotIn("path:", compact)
        self.assertIn("path:", full)
        self.assertLess(len(compact), len(full))

    def test_an_unknown_detail_is_refused(self) -> None:
        self.assertIn("detail must be", self.srv.search_memory("algo", detail="huge"))

    # ------------------------------------------------------- project tools
    def test_project_map_lists_files_and_symbols(self) -> None:
        files = self.srv.project_map(str(self.project))
        symbols = self.srv.project_map(str(self.project), detail="symbols")

        self.assertIn("Pedido.java", files)
        self.assertIn("confirmar", symbols)
        self.assertLess(len(files), len(symbols))
        self.assertTrue((Path(self._tmp.name) / "home" / "ihmt_projects").is_dir())

    def test_find_code_returns_the_method_with_file_and_lines(self) -> None:
        output = self.srv.find_code("Pedido confirmar importe minimo", str(self.project))

        self.assertRegex(output.splitlines()[0], r"^src/main/java/demo/Pedido\.java:\d+-\d+")
        self.assertIn("CONFIRMADO", output)

    def test_find_code_sees_edits_immediately(self) -> None:
        self.srv.find_code("Pedido confirmar", str(self.project))
        self.source.write_text(self.JAVA.replace("confirmar", "aprobar"), encoding="utf-8")
        later = __import__("time").time() + 5
        os.utime(self.source, (later, later))

        output = self.srv.find_code("Pedido aprobar importe minimo", str(self.project))
        self.assertIn("aprobar", output)

    def test_find_code_validates_its_arguments(self) -> None:
        self.assertIn("scope must be", self.srv.find_code("x", str(self.project), scope="everything"))
        self.assertIn("non-empty", self.srv.find_code("  ", str(self.project)))

    def test_read_file_answers_unchanged_then_a_diff(self) -> None:
        first = self.srv.read_file(str(self.source))
        again = self.srv.read_file(str(self.source))
        self.source.write_text(self.JAVA.replace("CONFIRMADO", "APROBADO"), encoding="utf-8")
        changed = self.srv.read_file(str(self.source))
        forced = self.srv.read_file(str(self.source), force=True)

        self.assertIn("public class Pedido", first)
        self.assertTrue(again.startswith("UNCHANGED"))
        self.assertTrue(changed.startswith("CHANGED"))
        self.assertIn("+        estado = \"APROBADO\";", changed)
        self.assertIn("public class Pedido", forced)
        self.assertLess(len(again), len(first) / 2)

    def test_read_file_can_return_a_line_range(self) -> None:
        output = self.srv.read_file(str(self.source), start_line=3, end_line=5)
        self.assertTrue(output.startswith("Pedido.java:3-5"))
        self.assertIn("public class Pedido", output)
        self.assertNotIn("private int lineas", output)

    # ------------------------------------------------------- session tools
    def test_notes_are_recalled_and_stay_out_of_long_term_memory(self) -> None:
        self.srv.note("La regla del importe minimo vive en Pedido.confirmar, Pedido.java:5-8.")
        recalled = self.srv.recall("importe minimo Pedido confirmar")
        long_term = self.srv.search_memory("importe minimo Pedido confirmar")

        self.assertIn("Pedido.java:5-8", recalled)
        self.assertIn("No memory found", long_term)

    def test_digest_output_keeps_the_signal_and_stores_the_rest(self) -> None:
        log = "\n".join(
            ["[INFO] Scanning for projects..."]
            + [f"[INFO] compiling file {i}" for i in range(300)]
            + ["[ERROR] PedidoTest.confirmar:42 expected CONFIRMADO but was BORRADOR", "[INFO] Tests run: 26, Failures: 1"]
            + [f"[INFO] trailing line {i}" for i in range(5)]
        )
        digest = self.srv.digest_output(log, label="maven-build")

        self.assertIn("PedidoTest.confirmar:42", digest)
        self.assertIn("Tests run: 26", digest)
        self.assertNotIn("compiling file 150", digest)
        self.assertLess(len(digest), len(log) / 5)
        self.assertIn("compiling file 150", self.srv.recall("maven-build compiling file 150"))


@unittest.skipUnless(HAS_MCP, "MCP SDK not installed (pip install 'mcp[cli]')")
class McpProtocolTests(unittest.TestCase):
    """A real client speaking JSON-RPC to the server over stdio."""

    def test_a_client_can_connect_list_and_call_tools(self) -> None:
        import sys

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise(home: str) -> tuple:
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(PROJECT_ROOT / "mcp_server.py")],
                env={**os.environ, "IHMT_HOME": home},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    saved = await session.call_tool(
                        "save_memory",
                        {"content": "2026-06-18: Migramos a Go y Postgres el backend."},
                    )
                    found = await session.call_tool(
                        "search_memory", {"query": "migración Go Postgres backend"}
                    )
                    return [t.name for t in listed.tools], saved, found

        with tempfile.TemporaryDirectory() as home:
            names, saved, found = asyncio.run(exercise(home))

        self.assertIn("save_memory", names)
        self.assertIn("search_memory", names)
        self.assertIn("find_code", names)
        self.assertFalse(saved.is_error)
        self.assertIn("Saved 1 memory", saved.content[0].text)
        self.assertFalse(found.is_error)
        self.assertIn("Go y Postgres", found.content[0].text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
