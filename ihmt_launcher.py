#!/usr/bin/env python3
"""Entry point of the ``ihmt-memory`` command installed from PyPI.

    uvx ihmt-memory                   # start the MCP server on stdio
    uvx ihmt-memory --instructions    # print the rules to append to CLAUDE.md / AGENTS.md
    uvx ihmt-memory capture --all     # store Claude Code conversations (see GUIDE.md 5.6)

Installed as a package, ``mcp_server.py`` lives in site-packages, so its default
``IHMT_HOME`` (the folder holding the code) would put the memory where the next
upgrade deletes it. This launcher defaults ``IHMT_HOME`` to ``~/.ihmt`` instead,
then runs the server unchanged. A clone of the repository keeps running
``mcp_server.py`` directly and is not affected.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

TEMPLATE = "memory-instructions.md"


def instructions() -> str:
    """Return the usage rules, from the installed package or from a clone."""
    try:  # installed: the wheel ships the template inside the ihmt package
        from importlib.resources import files

        packaged = files("ihmt").joinpath("templates", TEMPLATE)
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8")
    except (ImportError, OSError):  # pragma: no cover - depends on the install
        pass
    return (Path(__file__).resolve().parent / "templates" / TEMPLATE).read_text(encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    """Print the instructions, or start the server with ``~/.ihmt`` as the default store."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--instructions" in args:
        # Bytes, not text: a Windows console encoding cannot print the template's dashes.
        sys.stdout.buffer.write(instructions().encode("utf-8"))
        sys.stdout.flush()
        return 0

    if args and args[0] == "capture":
        # Stdlib only, and before the server import: a session hook runs this.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ihmt.transcripts import cli

        return cli(args[1:], Path.home() / ".ihmt")

    # mcp_server reads IHMT_HOME when it is imported, so set the default first.
    os.environ.setdefault("IHMT_HOME", str(Path.home() / ".ihmt"))
    import mcp_server

    return mcp_server.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
