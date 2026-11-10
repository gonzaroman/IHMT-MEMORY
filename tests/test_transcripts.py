"""Capturing Claude Code transcripts: what is kept, what is dropped, and never twice."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from ihmt.transcripts import (
    ConversationCapture,
    describe_action,
    read_transcript,
    redact,
    render_fragments,
)

SESSION = "0a1b2c3d-1111-2222-3333-444455556666"


def record(kind: str, content: Any, **extra: Any) -> Dict[str, Any]:
    base = {
        "type": kind,
        "timestamp": "2026-11-07T10:00:00.000Z",
        "sessionId": SESSION,
        "cwd": "/Users/someone/Development/web-bolsa",
        "message": {"role": kind, "content": content},
    }
    base.update(extra)
    return base


def write(path: Path, records: List[Dict[str, Any]], mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as handle:
        for item in records:
            handle.write(json.dumps(item) + "\n")


FIRST_EXCHANGE = [
    {"type": "ai-title", "aiTitle": "Portada de la web de bolsa", "sessionId": SESSION},
    record("user", "<system-reminder>ignore me</system-reminder>\nPon el gráfico de velas en la portada"),
    record("assistant", [{"type": "thinking", "thinking": "razonamiento privado"}]),
    record("assistant", [{"type": "text", "text": "Voy a editar la plantilla."}]),
    record(
        "assistant",
        [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/srv/portada.php"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/srv/portada.php", "old_string": "a"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "docker compose up -d", "description": "Arranca"}},
        ],
    ),
    record("user", [{"type": "tool_result", "content": "SALIDA ENORME DEL COMANDO " * 50}]),
    record("assistant", [{"type": "text", "text": "Listo: la portada ya muestra las velas."}]),
    record("user", "texto de un subagente", isSidechain=True),
    {"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "copia interna"}]}},
]


class ReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.transcript = Path(self._tmp.name) / "projects" / "-Users-someone-Development-web-bolsa" / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        write(self.transcript, FIRST_EXCHANGE)

    def test_only_what_was_said_and_done_is_kept(self) -> None:
        piece = read_transcript(self.transcript)

        self.assertEqual([t.role for t in piece.turns], ["user", "assistant"])
        self.assertEqual(piece.turns[0].text, "Pon el gráfico de velas en la portada")
        said = piece.turns[1].text
        self.assertIn("Voy a editar la plantilla.", said)
        self.assertIn("Listo: la portada", said)
        everything = json.dumps([t.__dict__ for t in piece.turns], ensure_ascii=False)
        for dropped in ("razonamiento privado", "SALIDA ENORME", "subagente", "copia interna", "ignore me"):
            self.assertNotIn(dropped, everything)

    def test_actions_are_one_line_each_and_reads_are_not_listed(self) -> None:
        actions = read_transcript(self.transcript).turns[1].actions

        self.assertEqual(actions, ["Edit: /srv/portada.php", "Bash: Arranca"])
        self.assertIsNone(describe_action("Grep", {"pattern": "x"}))
        self.assertEqual(describe_action("mcp__ihmt-memory__save_memory", {"content": "a  b"}), "save_memory: a b")
        self.assertLessEqual(len(describe_action("Bash", {"command": "x" * 900}) or ""), 160)

    def test_session_metadata_comes_from_the_transcript(self) -> None:
        piece = read_transcript(self.transcript)

        self.assertEqual(piece.session_id, SESSION)
        self.assertEqual(piece.title, "Portada de la web de bolsa")
        self.assertTrue(piece.cwd.endswith("web-bolsa"))

    def test_a_line_still_being_written_is_left_for_the_next_read(self) -> None:
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("user", "a medio escribir"))[:-5])
        piece = read_transcript(self.transcript)

        self.assertNotIn("a medio escribir", " ".join(t.text for t in piece.turns))
        self.assertLess(piece.end_offset, self.transcript.stat().st_size)

    def test_fragments_open_with_date_project_and_session(self) -> None:
        piece = read_transcript(self.transcript)
        fragments = render_fragments(piece.turns, project="web-bolsa", session="0a1b2c3d", title=piece.title)

        self.assertEqual(len(fragments), 1)
        timestamp, text = fragments[0]
        self.assertTrue(timestamp.startswith("2026-11-07"))
        self.assertTrue(text.startswith("2026-11-07 · web-bolsa · session 0a1b2c3d · Portada"))
        self.assertIn("  · Edit: /srv/portada.php", text)

    def test_a_question_stays_with_its_answer_when_fragments_split(self) -> None:
        turns = read_transcript(self.transcript).turns * 6
        fragments = render_fragments(turns, project="p", session="s", fragment_tokens=20)

        self.assertGreater(len(fragments), 1)
        for _, text in fragments:
            body = text.split("\n\n", 1)[1]
            self.assertTrue(body.startswith("User:"))
            self.assertIn("Claude:", body)


class RedactionTests(unittest.TestCase):
    def test_credentials_are_replaced(self) -> None:
        samples = [
            "usa esta clave: sk-proj-" + "A1b2C3d4" * 5,
            "token ghp_" + "a1B2c3D4e5" * 4,
            "AKIAIOSFODNN7EXAMPLE en el .env",
            "Authorization: Bearer " + "abc123def456ghi789",
            "DB_PASSWORD=sup3rs3cr3t!",
            'api_key: "0123456789abcdef"',
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:24]):
                cleaned = redact(sample)
                self.assertIn("[REDACTED]", cleaned)
                for secret in ("A1b2C3d4A1b2", "a1B2c3D4e5a1", "IOSFODNN7", "abc123def456", "sup3rs3cr3t", "0123456789abcdef", "MIIEow"):
                    self.assertNotIn(secret, cleaned)

    def test_ordinary_text_is_left_alone(self) -> None:
        for text in (
            "El token de sesión caduca a los 30 minutos.",
            "Cambia la contraseña desde Ajustes, no la guardo.",
            "max_tokens=3000 y branch_factor=8",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_a_pasted_key_never_reaches_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / f"{SESSION}.jsonl"
            key = "sk-" + "Zz9" * 12
            write(transcript, [record("user", f"te pego la clave {key}"), record("assistant", [{"type": "text", "text": "ok"}])])
            capture = ConversationCapture(Path(tmp) / "home")
            capture.capture([transcript])

            stored = "".join(p.read_text(encoding="utf-8") for p in capture.base.rglob("*.txt"))
            self.assertIn("te pego la clave [REDACTED]", stored)
            self.assertNotIn(key, stored)


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.projects = Path(self._tmp.name) / "projects"
        self.transcript = self.projects / "-Users-someone-Development-web-bolsa" / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        write(self.transcript, FIRST_EXCHANGE)
        self.capture = ConversationCapture(self.home, user_label="Gonzalo")

    def leaves(self) -> int:
        # A fresh instance: other instances (other processes, in real use) wrote too.
        return ConversationCapture(self.home).memory.stats()["leaves"]

    def test_a_session_is_stored_in_its_own_tree_under_its_project(self) -> None:
        reports = self.capture.capture_all(self.projects)

        self.assertEqual([(r.project, r.fragments) for r in reports], [("web-bolsa", 1)])
        self.assertFalse((self.home / "ihmt_memory").exists(), "the curated memory is not touched")
        found = self.capture.memory.search("gráfico velas portada", path_prefix="claude-code://web-bolsa/")
        self.assertTrue(found.results)
        best = found.results[0]
        self.assertEqual(best.domain, "conversations.web-bolsa")
        self.assertTrue(best.timestamp.startswith("2026-11-07"))
        self.assertIn("Gonzalo: Pon el gráfico", best.content)

    def test_capturing_again_stores_nothing_new(self) -> None:
        self.capture.capture_all(self.projects)
        before = self.leaves()

        self.assertEqual(ConversationCapture(self.home).capture_all(self.projects), [])
        self.assertEqual(self.leaves(), before)

    def test_only_the_new_part_of_a_growing_session_is_added(self) -> None:
        self.capture.capture_all(self.projects)
        write(
            self.transcript,
            [record("user", "Ahora añade el volumen debajo"), record("assistant", [{"type": "text", "text": "Añadido."}])],
            mode="a",
        )
        reports = ConversationCapture(self.home).capture_all(self.projects)

        self.assertEqual([(r.turns, r.fragments) for r in reports], [(2, 1)])
        self.assertEqual(self.leaves(), 2)
        hit = ConversationCapture(self.home).memory.search("añade volumen debajo").results[0]
        self.assertNotIn("velas", hit.content.split("\n\n", 1)[1])
        self.assertIn("Portada de la web de bolsa", hit.content, "the title learned earlier is remembered")

    def test_a_dry_run_reports_without_writing(self) -> None:
        reports = self.capture.capture_all(self.projects, dry_run=True)

        self.assertEqual(reports[0].fragments, 1)
        self.assertFalse(self.capture.base.exists())

    def test_losing_the_state_does_not_duplicate(self) -> None:
        self.capture.capture_all(self.projects)
        before = self.leaves()
        self.capture.state_path.unlink()

        ConversationCapture(self.home, user_label="Gonzalo").capture_all(self.projects)
        self.assertEqual(self.leaves(), before)

    def test_a_missing_transcript_is_reported_not_raised(self) -> None:
        reports = self.capture.capture([self.projects / "nope.jsonl"])

        self.assertEqual(reports[0].skipped, "no such transcript")

    def test_a_running_capture_makes_the_next_one_step_aside(self) -> None:
        self.capture.base.mkdir(parents=True)
        (self.capture.base / "capture.lock").write_text("", encoding="utf-8")

        reports = self.capture.capture_all(self.projects)
        self.assertTrue(reports[0].skipped)
        self.assertFalse(self.capture.state_path.exists())


class CliTests(unittest.TestCase):
    def test_the_hook_reads_its_payload_and_stays_silent(self) -> None:
        import subprocess
        import sys

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / f"{SESSION}.jsonl"
            write(transcript, FIRST_EXCHANGE)
            home = Path(tmp) / "home"
            done = subprocess.run(
                [sys.executable, str(root / "main.py"), "--path", str(home), "capture", "--hook"],
                input=json.dumps({"transcript_path": str(transcript), "session_id": SESSION}),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

            # The hook detaches where it can, so the store appears a moment later.
            import time

            deadline = time.time() + 30
            capture = ConversationCapture(home)
            while time.time() < deadline and not (capture.state_path.exists() and not (capture.base / "capture.lock").exists()):
                time.sleep(0.1)
            self.assertEqual(capture.memory.stats()["leaves"], 1)


if __name__ == "__main__":
    unittest.main()
