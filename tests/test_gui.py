"""Backend of the graphical interface, tested without a browser.

It is exercised two ways: calling :class:`GuiApi` directly, and speaking real
HTTP to the server to check the token, the routing and the headers.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ihmt_gui import setup as setup_mod
from ihmt_gui.handlers import ApiError, build_api
from ihmt_gui.server import create_server

from .base import EXAMPLES, StoreTestCase


class ApiTestCase(StoreTestCase):
    """Base with a populated memory and the interface API on top."""

    def setUp(self) -> None:
        super().setUp()
        self.memory.ingest_file(EXAMPLES / "journal_personal.txt")
        self.memory.ingest_file(EXAMPLES / "InventoryService.java")
        self.memory.flush()
        self.api = build_api(self.workspace, self.workspace)


class StatusTests(ApiTestCase):
    """State of the active folder."""

    def test_status_reports_the_memory_and_its_counters(self) -> None:
        status = self.api.status()

        self.assertTrue(status["memory_exists"])
        self.assertEqual(Path(status["memory_home"]), self.workspace.resolve())
        self.assertGreater(status["stats"]["leaves"], 0)
        self.assertEqual(
            status["total_files"], status["stats"]["leaves"] + status["stats"]["nodes"] + 1
        )
        self.assertIn("branch_factor", status["config"])

    def test_a_folder_without_memory_is_reported_as_empty(self) -> None:
        empty = self.workspace / "no_memory"
        empty.mkdir()
        api = build_api(empty, self.workspace)

        self.assertFalse(api.status()["memory_exists"])
        with self.assertRaises(ApiError) as ctx:
            api.tree()
        self.assertEqual(ctx.exception.status, 409)

    def test_creating_a_memory_from_the_interface(self) -> None:
        new_home = self.workspace / "new"
        api = build_api(new_home, self.workspace)

        status = api.create_memory()
        self.assertTrue(status["memory_exists"])
        self.assertTrue((new_home / "ihmt_memory" / "root.json").exists())


class TreeTests(ApiTestCase):
    """Walking the tree, one level per request."""

    def test_the_root_lists_domains(self) -> None:
        children = self.api.tree()["children"]
        domains = {c["title"] for c in children}

        self.assertIn("personal", domains)
        self.assertIn("software.java", domains)
        self.assertTrue(all(c["kind"] == "domain" for c in children))

    def test_descending_reaches_a_leaf(self) -> None:
        domain = next(c for c in self.api.tree()["children"] if c["title"] == "software.java")
        level = self.api.tree(domain["id"])["children"]
        self.assertTrue(level)

        seen, pending, leaf = set(), list(level), None
        while pending and leaf is None:
            current = pending.pop(0)
            if current["id"] in seen:
                continue
            seen.add(current["id"])
            if current["kind"] == "leaf":
                leaf = current
            else:
                pending.extend(self.api.tree(current["id"])["children"])

        self.assertIsNotNone(leaf, "descending from a domain must reach a leaf")
        self.assertEqual(self.api.tree(leaf["id"])["children"], [])

    def test_an_unknown_domain_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.tree("domain:does_not_exist")
        self.assertEqual(ctx.exception.status, 404)


class LeafTests(ApiTestCase):
    """Reading one specific leaf."""

    def first_leaf_id(self) -> str:
        from ihmt.models import NodeKind

        return next(
            e.id
            for e in self.api.memory().store.catalog.entries.values()
            if e.kind is NodeKind.LEAF
        )

    def test_a_leaf_returns_text_metadata_and_path(self) -> None:
        leaf = self.api.leaf(self.first_leaf_id())

        self.assertTrue(leaf["content"])
        self.assertTrue(leaf["domain"])
        self.assertTrue(leaf["timestamp"])
        self.assertEqual(leaf["path"][0], "root")
        self.assertEqual(leaf["path"][-1], leaf["id"])

    def test_a_superseded_leaf_carries_its_notice(self) -> None:
        response = self.api.search("Madrid Lavapiés piso contrato")
        notices = [n for r in response["results"] for n in r["notices"]]

        self.assertTrue(notices, "the Madrid note must come with its correction")
        self.assertTrue(any("Valencia" in notice for notice in notices))

    def test_identifiers_outside_the_catalog_are_refused(self) -> None:
        # This is the defence that stops arbitrary files on disk from being read.
        for attempt in ("../../etc/passwd", "/etc/passwd", "L-made-up-0000-aaaa"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ApiError) as ctx:
                    self.api.leaf(attempt)
                self.assertEqual(ctx.exception.status, 404)

    def test_asking_for_a_branch_as_if_it_were_a_leaf_fails_cleanly(self) -> None:
        from ihmt.models import NodeKind

        node = next(
            e.id
            for e in self.api.memory().store.catalog.entries.values()
            if e.kind is NodeKind.NODE
        )
        with self.assertRaises(ApiError) as ctx:
            self.api.leaf(node)
        self.assertEqual(ctx.exception.status, 400)


class SearchTests(ApiTestCase):
    """Diagnose screen."""

    def test_a_search_reports_results_and_its_cost(self) -> None:
        data = self.api.search("reserveStock reserva de stock")

        self.assertTrue(data["results"])
        self.assertGreater(data["total_files"], data["node_reads"] + data["leaf_reads"])
        self.assertGreaterEqual(data["confidence"], 0)

    def test_an_ambiguous_query_returns_the_candidates(self) -> None:
        data = self.api.search("Luis")

        self.assertTrue(data["ambiguous"])
        self.assertIsNotNone(data["clue_request"])
        self.assertGreaterEqual(len(data["clue_request"]["options"]), 2)

    def test_a_clue_resolves_the_ambiguity(self) -> None:
        data = self.api.search("Luis", clue="vacaciones en Benidorm")

        self.assertIsNone(data["clue_request"])
        self.assertIn("Benidorm", data["results"][0]["excerpt"])

    def test_an_empty_query_is_refused(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.search("   ")
        self.assertEqual(ctx.exception.status, 400)


class TimelineTests(ApiTestCase):
    """Active state, history and contradictions."""

    def test_facts_are_grouped_with_their_history(self) -> None:
        data = self.api.timeline()
        location = next(f for f in data["facts"] if f["attribute"] == "location")
        values = [v["value"] for v in location["values"]]
        statuses = [v["status"] for v in location["values"]]

        self.assertEqual(values, ["Madrid", "Valencia"])
        self.assertEqual(statuses, ["HISTORICAL", "ACTIVE"])

    def test_conflicts_come_with_their_notice(self) -> None:
        data = self.api.timeline()
        self.assertTrue(data["conflicts"])
        self.assertTrue(all(c["notice"] for c in data["conflicts"]))


class FolderTests(ApiTestCase):
    """Folder browser."""

    def test_listing_shows_only_directories(self) -> None:
        (self.workspace / "a_folder").mkdir()
        (self.workspace / "a_file.txt").write_text("x", encoding="utf-8")

        data = self.api.folders(str(self.workspace))
        names = {d["name"] for d in data["directories"]}

        self.assertIn("a_folder", names)
        self.assertNotIn("a_file.txt", names)

    def test_a_folder_with_a_memory_is_flagged(self) -> None:
        # Resolved paths: on macOS /var is a link to /private/var and the
        # listing always returns the canonical form.
        actual_path = self.workspace.resolve()
        data = self.api.folders(str(actual_path.parent))
        current = next((d for d in data["directories"] if d["path"] == str(actual_path)), None)

        self.assertIsNotNone(current, "the working folder must appear in its parent's listing")
        self.assertTrue(current["is_memory"], "and be flagged as an existing memory")

    def test_a_bad_path_falls_back_to_home_instead_of_failing(self) -> None:
        data = self.api.folders("/path/that/does/not/exist/anywhere")
        self.assertTrue(Path(data["path"]).is_dir())


class SetupTests(ApiTestCase):
    """Generating and applying the MCP configuration."""

    def test_the_project_scope_produces_a_valid_mcp_json(self) -> None:
        plan = self.api.setup_preview("project", project_dir=str(self.workspace))
        config = json.loads(plan["file_content"])
        entry = config["mcpServers"][setup_mod.SERVER_NAME]

        self.assertEqual(plan["kind"], "file")
        self.assertTrue(plan["file_path"].endswith(".mcp.json"))
        self.assertEqual(entry["env"]["IHMT_HOME"], str(self.workspace.resolve()))
        self.assertTrue(entry["args"][0].endswith("mcp_server.py"))

    def test_the_user_scope_produces_a_claude_command(self) -> None:
        plan = self.api.setup_preview("user")

        self.assertEqual(plan["kind"], "command")
        self.assertIn("--scope", plan["command"])
        self.assertIn("user", plan["command"])
        self.assertIn(f"IHMT_HOME={self.workspace.resolve()}", plan["command"])
        self.assertIn("mcp", plan["preview"])

    def test_preview_does_not_touch_anything(self) -> None:
        self.api.setup_preview("project", project_dir=str(self.workspace))
        self.assertFalse((self.workspace / ".mcp.json").exists(), "previewing must not write")

    def test_applying_the_project_scope_writes_the_file(self) -> None:
        result = self.api.setup_apply("project", project_dir=str(self.workspace))
        target = self.workspace / ".mcp.json"

        self.assertTrue(result["ok"])
        self.assertTrue(target.exists())
        config = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn(setup_mod.SERVER_NAME, config["mcpServers"])

    def test_applying_keeps_other_servers_already_configured(self) -> None:
        target = self.workspace / ".mcp.json"
        target.write_text(
            json.dumps({"mcpServers": {"other": {"command": "something"}}}), encoding="utf-8"
        )

        self.api.setup_apply("project", project_dir=str(self.workspace))
        config = json.loads(target.read_text(encoding="utf-8"))

        self.assertIn("other", config["mcpServers"], "someone else's configuration must survive")
        self.assertIn(setup_mod.SERVER_NAME, config["mcpServers"])

    def test_the_project_scope_targets_the_chosen_project_not_the_current_one(self) -> None:
        # The "I want this memory only in my calendar app" case: the memory
        # folder and the project folder differ, and the .mcp.json has to end
        # up in the PROJECT, not in the folder the interface was opened from.
        other = self.workspace / "calendar_app"
        other.mkdir()
        memory = self.workspace / "calendar_memory"
        memory.mkdir()

        plan = self.api.setup_preview("project", memory_home=str(memory), project_dir=str(other))
        config = json.loads(plan["file_content"])
        entry = config["mcpServers"][setup_mod.SERVER_NAME]

        self.assertEqual(Path(plan["file_path"]), other.resolve() / ".mcp.json")
        self.assertEqual(entry["env"]["IHMT_HOME"], str(memory.resolve()))

    def test_applying_to_another_project_writes_only_there(self) -> None:
        other = self.workspace / "java_course"
        other.mkdir()

        result = self.api.setup_apply("project", project_dir=str(other))

        self.assertTrue(result["ok"])
        self.assertTrue((other / ".mcp.json").exists(), "it must register in the chosen project")
        self.assertFalse(
            (self.workspace / ".mcp.json").exists(),
            "and must not touch the project the interface was opened from",
        )

    def test_an_unknown_scope_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.setup_preview("global")
        self.assertEqual(ctx.exception.status, 400)

    def test_rechecking_bypasses_the_cache(self) -> None:
        # The "Check again" button exists to re-read after approving the
        # server: if it reused the 15 s cache, it would keep showing the
        # previous state exactly when it matters most.
        calls = []
        original = setup_mod.invalidate_cache

        def spy() -> None:
            calls.append(True)
            original()

        setup_mod.invalidate_cache = spy
        self.addCleanup(lambda: setattr(setup_mod, "invalidate_cache", original))

        self.api.setup_state()
        self.assertEqual(calls, [], "a normal load may use the cache")

        self.api.setup_state(refresh="1")
        self.assertEqual(len(calls), 1, "refreshing must drop what was cached")

    def with_probe(self, probe: "setup_mod.Probe") -> None:
        """Replace the CLI query with a fixed answer."""
        original = setup_mod.probe_server
        setup_mod.probe_server = lambda cli: probe
        setup_mod.invalidate_cache()
        self.addCleanup(setup_mod.invalidate_cache)
        self.addCleanup(lambda: setattr(setup_mod, "probe_server", original))

    def test_a_pending_project_registration_suggests_the_user_scope(self) -> None:
        # A .mcp.json always asks for approval; the user scope does not.
        # Whoever gets stuck approving needs to know that way out exists.
        self.with_probe(setup_mod.Probe(known=True, status="pending", scope="project"))
        state = setup_mod.detect_state(self.workspace)

        self.assertIs(state.verdict, setup_mod.Verdict.PENDING_APPROVAL)
        self.assertIn("user_scope_needs_no_approval", state.notes)

    def test_a_working_user_registration_does_not_nag(self) -> None:
        self.with_probe(setup_mod.Probe(known=True, status="connected", scope="user"))
        state = setup_mod.detect_state(self.workspace)

        self.assertNotIn("user_scope_needs_no_approval", state.notes)

    def test_state_detects_a_registered_project(self) -> None:
        self.api.setup_apply("project", project_dir=str(self.workspace))
        state = self.api.setup_state()

        self.assertTrue(state["project_registered"])
        self.assertEqual(state["registered_home"], str(self.workspace.resolve()))


class HttpTests(ApiTestCase):
    """The real server: token, routing and verbs."""

    def setUp(self) -> None:
        super().setUp()
        self.server = create_server(self.workspace, self.workspace)
        self.server.start_background()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def fetch(self, path: str, *, token: bool = True, method: str = "GET", body=None):
        request = urllib.request.Request(f"{self.base}{path}", method=method)
        if token:
            request.add_header("X-IHMT-Token", self.server.token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps(body).encode("utf-8")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_the_api_requires_the_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/api/status", token=False)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(json.loads(ctx.exception.read())["error"], "bad_token")

    def test_status_over_http(self) -> None:
        code, data = self.fetch("/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(data["memory_exists"])

    def test_the_page_is_served_without_a_token(self) -> None:
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("<title>IHMT", body)

    def test_static_files_cannot_escape_their_folder(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/../../ihmt/config.py", timeout=10)
        self.assertIn(ctx.exception.code, (400, 403, 404))

    def test_write_endpoints_reject_get(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/api/setup/apply")
        self.assertEqual(ctx.exception.code, 405)

    def test_an_unknown_endpoint_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/api/made-up")
        self.assertEqual(ctx.exception.code, 404)

    def test_preview_over_http_does_not_write(self) -> None:
        code, data = self.fetch(
            "/api/setup/preview", method="POST", body={"scope": "project"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(data["kind"], "file")
        self.assertFalse((self.workspace / ".mcp.json").exists())

    def test_endpoints_with_parameters_work_over_http(self) -> None:
        # Covers the browser's real walk: domain -> branch -> leaf, plus a
        # search with a clue. A mismatch between the parameter name in the URL
        # and the function's shows up here as a 400.
        _, domains = self.fetch("/api/tree")
        java = next(d for d in domains["children"] if d["title"] == "software.java")

        _, level = self.fetch(f"/api/tree?id={urllib.parse.quote(java['id'])}")
        self.assertTrue(level["children"])

        leaf = next(
            (c for c in level["children"] if c["kind"] == "leaf"),
            None,
        ) or next(
            c
            for c in self.fetch(
                f"/api/tree?id={urllib.parse.quote(level['children'][0]['id'])}"
            )[1]["children"]
            if c["kind"] == "leaf"
        )

        code, data = self.fetch(f"/api/leaf?id={urllib.parse.quote(leaf['id'])}")
        self.assertEqual(code, 200)
        self.assertTrue(data["content"])

        _, search = self.fetch("/api/search?q=Luis&clue=Benidorm")
        self.assertIn("results", search)

    def test_the_token_is_not_in_the_page(self) -> None:
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as response:
            body = response.read().decode("utf-8")
        self.assertNotIn(self.server.token, body, "the token travels in the URL, not embedded")


class ProbeTests(unittest.TestCase):
    """Reading the output of ``claude mcp get``.

    It works on literal CLI outputs instead of invoking it, so the diagnosis
    can be checked without depending on how this machine is set up.
    """

    PENDING = (
        "ihmt-memory:\n"
        "  Scope: Project config (shared via .mcp.json)\n"
        "  Status: ⏸ Pending approval (run `claude` to approve)\n\n"
        "To remove this server, run: claude mcp remove ihmt-memory -s project"
    )
    CONNECTED = (
        "ihmt-memory:\n"
        "  Scope: User config\n"
        "  Status: ✔ Connected\n"
    )
    ABSENT = (
        'No MCP server named "ihmt-memory". Configured servers: claude.ai Gmail, '
        "claude.ai Google Calendar"
    )

    def test_pending_approval_is_not_read_as_working(self) -> None:
        probe = setup_mod.parse_probe(self.PENDING)

        self.assertTrue(probe.known)
        self.assertEqual(probe.status, "pending")

    def test_the_scope_comes_from_the_cli_not_from_a_guess(self) -> None:
        # The regression that started all this: a PROJECT registration was
        # being announced as "registered for all projects".
        self.assertEqual(setup_mod.parse_probe(self.PENDING).scope, "project")
        self.assertEqual(setup_mod.parse_probe(self.CONNECTED).scope, "user")

    def test_a_user_scope_is_not_mistaken_for_a_project_one(self) -> None:
        # Literal CLI text. It contains "projects" inside the description of a
        # USER scope: searching for the substring "project" gave the opposite
        # of the real scope and the interface announced exactly the reverse.
        output = (
            "ihmt-memory:\n"
            "  Scope: User config (available in all your projects)\n"
            "  Status: ✔ Connected\n"
            "  Type: stdio\n"
        )
        self.assertEqual(setup_mod.parse_probe(output).scope, "user")

    def test_every_scope_wording_is_read_correctly(self) -> None:
        for text, expected in {
            "User config (available in all your projects)": "user",
            "Project config (shared via .mcp.json)": "project",
            "Local config": "local",
            "Something we do not recognize": None,
        }.items():
            with self.subTest(scope=text):
                output = f"ihmt-memory:\n  Scope: {text}\n  Status: ✔ Connected\n"
                self.assertEqual(setup_mod.parse_probe(output).scope, expected)

    def test_an_absent_server_is_detected_despite_exit_code_zero(self) -> None:
        # The CLI answers successfully even when the server does not exist, so
        # detection has to look at the text.
        probe = setup_mod.parse_probe(self.ABSENT)

        self.assertFalse(probe.known)
        self.assertEqual(probe.status, "absent")

    def test_a_connected_server_is_recognized(self) -> None:
        self.assertEqual(setup_mod.parse_probe(self.CONNECTED).status, "connected")

    def test_an_unrecognized_status_never_claims_success(self) -> None:
        probe = setup_mod.parse_probe("ihmt-memory:\n  Status: 🤷 something new\n")

        self.assertEqual(probe.status, "unknown")
        self.assertIn("something new", probe.detail, "the literal text is kept")

    def test_empty_output_is_unavailable(self) -> None:
        self.assertEqual(setup_mod.parse_probe("").status, "unavailable")


class VerdictTests(unittest.TestCase):
    """Combining the signals into a single answer."""

    def interpreter(self, has_mcp: bool = True) -> setup_mod.Interpreter:
        return setup_mod.Interpreter(path="/usr/bin/python3", has_mcp=has_mcp, is_venv=False)

    def test_connected_plus_sdk_is_working(self) -> None:
        probe = setup_mod.Probe(known=True, status="connected", scope="user")
        verdict = setup_mod._decide(probe, self.interpreter(), "/usr/bin/claude")

        self.assertIs(verdict, setup_mod.Verdict.WORKING)

    def test_pending_approval_has_its_own_verdict(self) -> None:
        probe = setup_mod.Probe(known=True, status="pending", scope="project")
        verdict = setup_mod._decide(probe, self.interpreter(), "/usr/bin/claude")

        self.assertIs(verdict, setup_mod.Verdict.PENDING_APPROVAL)

    def test_a_missing_sdk_beats_a_connected_status(self) -> None:
        # Without the package the server does not start, whatever the CLI says.
        probe = setup_mod.Probe(known=True, status="connected", scope="user")
        verdict = setup_mod._decide(probe, self.interpreter(has_mcp=False), "/usr/bin/claude")

        self.assertIs(verdict, setup_mod.Verdict.SDK_MISSING)

    def test_without_the_cli_the_verdict_says_so(self) -> None:
        probe = setup_mod.Probe(known=False, status="unavailable")
        verdict = setup_mod._decide(probe, self.interpreter(), None)

        self.assertIs(verdict, setup_mod.Verdict.CLI_MISSING)

    def test_an_unknown_server_is_not_registered(self) -> None:
        probe = setup_mod.Probe(known=False, status="absent")
        verdict = setup_mod._decide(probe, self.interpreter(), "/usr/bin/claude")

        self.assertIs(verdict, setup_mod.Verdict.NOT_REGISTERED)


class SetupStateShapeTests(ApiTestCase):
    """What the interface receives from the status endpoint."""

    def test_the_payload_carries_a_single_verdict(self) -> None:
        state = self.api.setup_state()

        self.assertIn(state["verdict"], {v.value for v in setup_mod.Verdict})
        self.assertIn("effective_scope", state)
        self.assertIn("detail", state)
        self.assertNotIn("user_registered", state, "the misleading signal no longer exists")

    def test_the_detail_does_not_leak_other_servers(self) -> None:
        # It used to dump `claude mcp list`, which enumerates Gmail, Calendar
        # and whatever else the user has configured.
        detail = self.api.setup_state()["detail"].lower()

        for foreign in ("gmail", "calendar", "google"):
            self.assertNotIn(foreign, detail, f"the detail must not mention {foreign}")


class RoutingTests(unittest.TestCase):
    """The route table must match the API's real signatures."""

    def test_every_declared_parameter_exists_in_its_method(self) -> None:
        import inspect

        from ihmt_gui.handlers import READ_ROUTES, WRITE_ROUTES, GuiApi

        for table, label in ((READ_ROUTES, "GET"), (WRITE_ROUTES, "POST")):
            for route, (name, params) in table.items():
                with self.subTest(route=route, verb=label):
                    method = getattr(GuiApi, name, None)
                    self.assertIsNotNone(method, f"{route} points to a missing method")
                    signature = inspect.signature(method)
                    for source, target in params.items():
                        self.assertIn(
                            target,
                            signature.parameters,
                            f"{route}: '{source}' maps to '{target}', "
                            f"which does not exist in {name}{signature}",
                        )


class StaticAssetTests(unittest.TestCase):
    """The page and its translations must be complete.

    A missing key breaks nothing: the raw identifier just shows on screen,
    which is the kind of bug nobody notices until a user sees it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import re

        cls.static = Path(__file__).resolve().parent.parent / "ihmt_gui" / "static"
        source = (cls.static / "i18n.js").read_text(encoding="utf-8")
        cls.languages = {}
        for language in ("es", "en"):
            block = re.search(rf"\n  {language}: \{{(.*?)\n  \}},?\n", source, re.DOTALL)
            assert block, f"the {language} block is missing from i18n.js"
            cls.languages[language] = set(re.findall(r'"([\w.]+)":', block.group(1)))

    def test_both_languages_define_the_same_keys(self) -> None:
        only_es = self.languages["es"] - self.languages["en"]
        only_en = self.languages["en"] - self.languages["es"]

        self.assertEqual(only_es, set(), "keys only in Spanish")
        self.assertEqual(only_en, set(), "keys only in English")
        self.assertGreater(len(self.languages["en"]), 50)

    def test_every_key_used_in_the_page_exists(self) -> None:
        import re

        html = (self.static / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r'data-i18n(?:-ph)?="([\w.]+)"', html))

        self.assertTrue(used, "the HTML must use the translation system")
        self.assertEqual(used - self.languages["en"], set(), "untranslated keys in the HTML")

    def test_every_key_used_in_the_script_exists(self) -> None:
        import re

        js = (self.static / "app.js").read_text(encoding="utf-8")
        # The (?<![\w.]) avoids catching the tail of other calls, like get("t").
        # The optional tail picks up calls with arguments: t("key", 1, 2).
        used = set(re.findall(r'(?<![\w.])t\("([\w.]+)"(?:\s*,[^)]*)?\)', js))

        self.assertTrue(used)
        self.assertEqual(used - self.languages["en"], set(), "untranslated keys in app.js")

    def test_english_is_the_fallback_language(self) -> None:
        # Documentation and code are in English; a browser in any other
        # language than the two provided must get English, not Spanish.
        source = (self.static / "i18n.js").read_text(encoding="utf-8")
        self.assertIn('if (!STRINGS[currentLang]) currentLang = "en";', source)
        self.assertIn('<html lang="en">', (self.static / "index.html").read_text(encoding="utf-8"))

    def test_the_interface_stays_dark_regardless_of_the_system_theme(self) -> None:
        # Design decision: the interface is always dark. It is easy to revert
        # by accident when tweaking colours, so it is pinned here.
        css = (self.static / "styles.css").read_text(encoding="utf-8")
        html = (self.static / "index.html").read_text(encoding="utf-8")

        self.assertNotIn(
            "prefers-color-scheme",
            css,
            "the stylesheet must not go back to following the system theme",
        )
        self.assertIn("color-scheme: dark", css)
        self.assertIn(
            '<meta name="color-scheme" content="dark">',
            html,
            "without this tag the browser paints a white flash before the CSS",
        )

    def test_the_page_never_injects_html(self) -> None:
        # Stored text is written by anyone: it must be inserted as text, never
        # as markup.
        js = (self.static / "app.js").read_text(encoding="utf-8")
        for number, line in enumerate(js.splitlines(), start=1):
            clean = line.strip()
            if clean.startswith(("*", "//", "/*")):
                continue  # comments may name it
            if "innerHTML" in clean and "is not allowed" not in clean:
                self.fail(f"innerHTML used in app.js:{number}: {clean}")


class NoDependencyTests(unittest.TestCase):
    """The interface must not introduce external dependencies."""

    def test_the_package_only_imports_the_standard_library(self) -> None:
        allowed = {
            "__future__", "argparse", "dataclasses", "enum", "functools", "http", "json",
            "logging", "os", "pathlib", "re", "secrets", "shutil", "subprocess", "sys",
            "threading", "time", "tkinter", "typing", "urllib", "webbrowser",
        }
        root = Path(__file__).resolve().parent.parent / "ihmt_gui"
        for path in root.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                clean = line.strip()
                if not clean.startswith(("import ", "from ")) or clean.startswith("from ."):
                    continue
                module = clean.split()[1].split(".")[0]
                if module in ("ihmt", "ihmt_gui"):
                    continue
                with self.subTest(file=path.name, line=clean):
                    self.assertIn(module, allowed, f"unexpected dependency in {path.name}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
