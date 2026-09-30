"""Documentation that agents and people copy from must stay consistent."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class InstructionsTemplateTests(unittest.TestCase):
    """GUIDE.md shows the usage instructions inline; INSTALL.md has agents copy the file.

    Both must carry the same text, or people and agents would install different rules.
    """

    def test_the_guide_and_the_template_file_carry_the_same_text(self) -> None:
        guide = (ROOT / "GUIDE.md").read_text(encoding="utf-8")
        block = re.search(r"```markdown\n(# Memory: use IHMT\n.*?)```", guide, re.DOTALL)
        self.assertIsNotNone(block, "GUIDE.md must show the instructions template in a markdown block")

        template = (ROOT / "templates" / "memory-instructions.md").read_text(encoding="utf-8")
        self.assertEqual(block.group(1), template)

    def test_install_md_points_to_files_that_exist(self) -> None:
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([\w./-]+\.md)\)", install):
            with self.subTest(link=target):
                self.assertTrue((ROOT / target).exists(), f"INSTALL.md links to a missing {target}")


class InstallDetectorTests(unittest.TestCase):
    """INSTALL.md step 2 has agents run an inline script to find an existing installation."""

    @classmethod
    def setUpClass(cls) -> None:
        install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        script = re.search(r"<PY> - <<'PY'\n(.*?)\nPY\n", install, re.DOTALL)
        assert script, "INSTALL.md must contain the detection script"
        cls.script = script.group(1)

    def run_detector(self, home: Path) -> str:
        import os
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", self.script],
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_an_empty_home_has_no_installation(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as home:
            self.assertIn("No existing IHMT installation found.", self.run_detector(Path(home)))

    def test_it_finds_a_registration_and_its_memory(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo = home / "tools" / "ihmt"
            (repo / "ihmt").mkdir(parents=True)
            (repo / "ihmt" / "api.py").write_text("", encoding="utf-8")
            (repo / "mcp_server.py").write_text("", encoding="utf-8")
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {"ihmt-memory": {
                "command": str(repo / ".venv/bin/python"),
                "args": [str(repo / "mcp_server.py")],
                "env": {"IHMT_HOME": str(home / "mymem")},
            }}}), encoding="utf-8")
            config = home / ".config" / "opencode"
            config.mkdir(parents=True)
            (config / "opencode.jsonc").write_text(
                '{\n  // another server that happens to use the same file name\n'
                '  "mcp": {"other": {"command": ["python", "/elsewhere/mcp_server.py"]}}\n}\n',
                encoding="utf-8",
            )

            output = self.run_detector(home)

        self.assertIn(".claude.json", output)
        self.assertIn(str(repo), output)
        self.assertIn(str(home / "mymem"), output)
        self.assertNotIn("opencode", output, "a mcp_server.py without IHMT's code is not IHMT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
