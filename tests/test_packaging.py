"""The PyPI package and the MCP Registry entry must describe the same release."""

from __future__ import annotations

import json
import re
import unittest

from .base import PROJECT_ROOT

SERVER_NAME = "io.github.gonzaroman/ihmt-memory"


def _pyproject_field(field: str) -> str:
    # A regex rather than tomllib, which needs Python 3.11 (IHMT supports 3.10).
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(rf'^{field} = "([^"]+)"', text, re.MULTILINE)
    assert match, f"pyproject.toml has no {field}"
    return match.group(1)


class ReleaseVersionTests(unittest.TestCase):
    """A version left behind in one file would publish a mismatched release."""

    def test_every_file_carries_the_server_version(self) -> None:
        server_py = (PROJECT_ROOT / "mcp_server.py").read_text(encoding="utf-8")
        match = re.search(r'^\s+version="([^"]+)",', server_py, re.MULTILINE)
        self.assertIsNotNone(match, "mcp_server.py must pass version= to the server")
        version = match.group(1)

        server_json = json.loads((PROJECT_ROOT / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(_pyproject_field("version"), version, "pyproject.toml")
        self.assertEqual(server_json["version"], version, "server.json")
        for package in server_json["packages"]:
            self.assertEqual(package["version"], version, "server.json packages")


class RegistryEntryTests(unittest.TestCase):
    """The MCP Registry checks that the PyPI package and the entry name each other."""

    def test_the_entry_points_at_this_package(self) -> None:
        server_json = json.loads((PROJECT_ROOT / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(server_json["name"], SERVER_NAME)
        self.assertLessEqual(len(server_json["description"]), 100, "the Registry rejects longer descriptions")
        (package,) = server_json["packages"]
        self.assertEqual(package["registryType"], "pypi")
        self.assertEqual(package["identifier"], _pyproject_field("name"))

    def test_the_readme_proves_ownership(self) -> None:
        # PyPI shows README.md as the package description; the Registry looks for this line there.
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"mcp-name: {SERVER_NAME}", readme)

    def test_the_command_is_named_like_the_package(self) -> None:
        # `uvx ihmt-memory` and Registry clients run the command named after the package.
        text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'\n{_pyproject_field("name")} = "ihmt_launcher:main"', text)


class LauncherTests(unittest.TestCase):
    """`ihmt-memory --instructions` hands out the same rules as the template file."""

    def test_it_prints_the_instructions_template(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "ihmt_launcher.py"), "--instructions"],
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        template = (PROJECT_ROOT / "templates" / "memory-instructions.md").read_bytes()
        self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), template.replace(b"\r\n", b"\n"))

    def test_capture_is_reachable_without_main_py(self) -> None:
        """The wheel ships no main.py, so the hooks of a PyPI install call the launcher."""
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "ihmt_launcher.py"), "capture", "--all", "--dry-run",
                 "--claude-dir", tmp, "--codex-dir", tmp, "--opencode-dir", tmp],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "IHMT_HOME": tmp},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would store 0 fragments", result.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
