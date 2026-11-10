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
import re
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
                "search_memory", "save_memory", "mark_outdated",
                "project_map", "find_code", "read_file",
                "note", "recall", "digest_output",
            },
        )

        search = tools["search_memory"].input_schema
        self.assertEqual(search["required"], ["query"])
        self.assertIn("clue", search["properties"])

        save = tools["save_memory"].input_schema
        self.assertEqual(save["required"], ["content"])
        self.assertIn("replaces", save["properties"])
        self.assertEqual(save["properties"]["domain"]["default"], "general")
        self.assertEqual(save["properties"]["content_type"]["default"], "auto")

        for tool in tools.values():
            self.assertTrue(tool.description and len(tool.description) > 100, "tools need usable descriptions")

    def test_the_store_is_created_on_first_use(self) -> None:
        self.srv.save_memory("Una nota cualquiera que merece recordarse.")
        self.assertTrue((Path(self._tmp.name) / "ihmt_memory" / "root.json").exists())

    def test_parallel_calls_on_a_new_store_all_succeed(self) -> None:
        # The SDK runs each call in a worker thread, and agents often call tools in parallel.
        from concurrent.futures import ThreadPoolExecutor

        def call(n: int) -> str:
            if n % 2:
                return self.srv.search_memory(f"nota paralela {n} proyecto Orión")
            return self.srv.save_memory(f"2026-10-15: nota paralela número {n} sobre el proyecto Orión.")

        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(call, range(24)))  # an exception in any call re-raises here

        for n, output in enumerate(outputs):
            with self.subTest(call=n):
                self.assertNotIn("unavailable", output)
                if n % 2 == 0:
                    self.assertIn("Saved 1 memory", output)

    # ------------------------------------------------------------ save_memory
    def test_saving_reports_classification_and_the_new_id(self) -> None:
        output = self.srv.save_memory(
            "public class Foo {\n    void bar() {\n        return;\n    }\n}\n"
        )
        self.assertIn("CODE", output)
        self.assertRegex(output, r"\(id L-[\w.-]+\)")
        self.assertEqual(len(output.splitlines()), 1, "a plain save answers in one line")

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

    def test_a_query_sharing_one_stray_word_reports_not_found(self) -> None:
        self.srv.save_memory("2024-07-22: Vacaciones en Benidorm con Luis, mi primo.")
        self.srv.save_memory("2025-01-15: Comida familiar con mi primo Luis y los niños.")

        output = self.srv.search_memory("interfaz gráfica navegador pantallas Luis diagnóstico cronología")
        self.assertTrue(output.startswith("NOT FOUND"), output)
        self.assertNotIn("AMBIGUOUS", output)

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
        self.assertIn("Ruzafa", output.split("\n\n")[0], "the current version comes first")
        self.assertIn("This is the current version", output)

    def test_replaces_flags_the_corrected_memory_and_answers_with_the_new_one(self) -> None:
        self.srv.save_memory("2026-10-09: RainDesktop 1.3 adds the Mafia mode; installed in Applications.")
        self.srv.save_memory("2026-10-08: The office printer needs a new toner cartridge.")
        output = self.srv.save_memory(
            "2026-10-10: There is no RainDesktop 1.3: the Mafia mode ships in 1.2.",
            replaces="RainDesktop 1.3 Mafia mode",
        )
        self.assertIn("now flagged OUTDATED", output)

        answer = self.srv.search_memory("RainDesktop 1.3 Mafia mode installed")
        self.assertIn("ships in 1.2", answer.split("\n\n")[0], "the correction comes first")
        self.assertIn("OUTDATED", answer)

    def test_replaces_with_several_candidates_flags_none_and_lists_them(self) -> None:
        self.srv.save_memory("2026-10-01: Backups run nightly to the NAS at 02:00.")
        self.srv.save_memory("2026-10-02: Backups run nightly to the NAS at 03:00 after the router change.")
        output = self.srv.save_memory("2026-10-10: Backups now run to S3, not to the NAS.", replaces="backups NAS nightly")

        self.assertIn("none was flagged", output)
        self.assertIn("mark_outdated", output)
        old_id = re.findall(r"^  (L-\S+) ·", output, re.MULTILINE)[0]
        new_id = re.search(r'new_id="(L-[^"]+)"', output).group(1)

        flagged = self.srv.mark_outdated(old_id, new_id)
        self.assertIn("Flagged as outdated", flagged)
        self.assertIn("OUTDATED", self.srv.search_memory("backups NAS nightly 02:00"))

    def test_replaces_naming_nothing_stored_says_so(self) -> None:
        self.srv.save_memory("2026-10-01: The team standup moved to 10:00.")
        output = self.srv.save_memory("2026-10-10: Kubernetes cluster upgraded.", replaces="helm chart rollout")

        self.assertIn("nothing was flagged", output)

    def test_replaces_flags_the_memory_whose_text_matches_not_a_related_one(self) -> None:
        self.srv.save_memory(
            "2026-01-14: Decided on PostgreSQL over SQLite because bookings need row-level locking when "
            "two students grab the last seat on a boat."
        )
        self.srv.save_memory("2026-03-02: Staging database runs PostgreSQL 15 on a shared cluster.")
        for filler in ("Stripe webhooks are idempotent.", "Backups go to S3 nightly.", "Money is stored in cents."):
            self.srv.save_memory(f"2026-04-01: {filler}")
        output = self.srv.save_memory(
            "2026-08-20: Staging upgraded to PostgreSQL 17; 15 is gone.",
            replaces="staging database PostgreSQL version",
        )

        self.assertIn("now flagged OUTDATED", output)
        self.assertIn("Staging database runs PostgreSQL 15", output)
        self.assertNotIn("row-level locking", output)

    def test_a_clear_correction_prefix_flags_its_predecessor(self) -> None:
        self.srv.save_memory("2026-03-02: Staging database runs PostgreSQL 15 on a shared cluster.")
        output = self.srv.save_memory("2026-08-20: CORRECTION: staging database runs PostgreSQL 17 now.")

        self.assertIn("now flagged OUTDATED", output)

    def test_mark_outdated_with_an_unknown_id_explains_itself(self) -> None:
        self.assertIn("Could not flag", self.srv.mark_outdated("L-nope", "L-neither"))

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

    # ------------------------------------------- phrasings and date filter
    def test_another_phrasing_in_the_same_call_finds_what_the_first_misses(self) -> None:
        self.srv.save_memory("2026-06-18: Migramos a Go y Postgres el backend de facturación.")
        alone = self.srv.search_memory("billing service rewrite language database")
        together = self.srv.search_memory(
            "billing service rewrite language database", also=["migración backend facturación Go Postgres"]
        )

        self.assertNotIn("Go y Postgres", alone)
        self.assertIn("Go y Postgres", together)

    def test_since_leaves_out_older_memories(self) -> None:
        self.srv.save_memory("2026-03-02: La versión publicada de RainDesktop es la 1.2, con panel de lluvia.")
        self.srv.save_memory("2026-10-05: La versión publicada de RainDesktop es la 1.4, con panel de nieve.")

        recent = self.srv.search_memory("versión publicada RainDesktop", since="2026-10-01")
        self.assertIn("1.4", recent)
        self.assertNotIn("1.2", recent)
        self.assertIn("No memory found", self.srv.search_memory("versión publicada RainDesktop", since="2027-01-01"))
        self.assertIn("since must be", self.srv.search_memory("versión RainDesktop", since="octubre"))

    # ------------------------------------------------------- conversations
    def _capture_session(self, project: str, said: str) -> None:
        import json

        from ihmt.transcripts import ConversationCapture

        transcript = Path(self._tmp.name) / f"{project}.jsonl"
        records = [
            {"type": "user", "timestamp": "2026-11-07T10:00:00Z", "sessionId": f"{project}-session",
             "cwd": f"/work/{project}", "message": {"role": "user", "content": said}},
            {"type": "assistant", "timestamp": "2026-11-07T10:00:05Z", "sessionId": f"{project}-session",
             "cwd": f"/work/{project}", "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Hecho, lo dejo anotado en el plan."}]}},
        ]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        ConversationCapture(os.environ["IHMT_HOME"]).capture([transcript])

    def test_conversations_are_searched_only_when_asked(self) -> None:
        self.assertIn("No conversations", self.srv.search_memory("cualquier cosa", scope="conversations"))
        self._capture_session("web-bolsa", "Cambia el gráfico de velas japonesas por uno de líneas en la portada")
        self.srv.save_memory("2026-06-18: Migramos a Go y Postgres el backend de facturación.")

        found = self.srv.search_memory("gráfico velas japonesas portada", scope="conversations")
        self.assertIn("velas japonesas", found)
        self.assertIn("2026-11-07", found)
        self.assertNotIn("Cambia el gráfico", self.srv.search_memory("gráfico velas japonesas portada"))

    def test_a_project_narrows_the_conversations(self) -> None:
        self._capture_session("web-bolsa", "Cambia el gráfico de velas japonesas por uno de líneas")
        self._capture_session("raindesktop", "Cambia el gráfico de velas japonesas del panel de lluvia")

        only = self.srv.search_memory("gráfico velas japonesas", scope="conversations", project="raindesktop")
        self.assertIn("panel de lluvia", only)
        other = self.srv.search_memory("gráfico velas japonesas", scope="conversations", project="nadie")
        self.assertIn("Nothing like that", other)
        self.assertIn("scope must be", self.srv.search_memory("algo", scope="todo"))
        self.assertIn("only applies", self.srv.search_memory("algo", project="web-bolsa"))

    def test_project_map_leaves_tests_out_unless_asked(self) -> None:
        test_file = self.project / "src" / "test" / "java" / "demo" / "PedidoTest.java"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("package demo;\nclass PedidoTest {\n    void confirma() {\n    }\n}\n", encoding="utf-8")

        self.assertNotIn("PedidoTest", self.srv.project_map(str(self.project)))
        self.assertIn("1 test files left out", self.srv.project_map(str(self.project)))
        self.assertIn("PedidoTest", self.srv.project_map(str(self.project), scope="all"))

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
                # The server sweeps the agents' sessions when it starts: point it at
                # empty folders, never at the real ones of whoever runs the tests.
                env={**os.environ, "IHMT_HOME": home, "CLAUDE_CONFIG_DIR": home, "CODEX_HOME": home,
                     "OPENCODE_DATA_DIR": home},
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
