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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
