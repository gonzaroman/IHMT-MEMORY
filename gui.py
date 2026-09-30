#!/usr/bin/env python3
"""IHMT graphical interface — entry point.

    python3 gui.py                         # opens the browser on the current folder
    python3 gui.py --path ~/my_memory      # works on another memory
    python3 gui.py --port 8765 --no-browser

Starts a local server (``127.0.0.1`` only) and opens a page with four
screens: setup, explore, diagnose and timeline. It installs nothing: it
uses the standard library only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ihmt_gui.server import serve  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="gui.py",
        description="Local graphical interface for the IHMT memory.",
    )
    parser.add_argument(
        "--path",
        default=".",
        help="folder that contains (or will contain) ihmt_memory/ (default: the current one)",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="project the 'this project only' scope acts on (default: the current folder)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to use; 0 lets the system pick a free one (default)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the browser automatically, just print the address",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    return serve(
        Path(args.path).expanduser(),
        Path(args.project).expanduser(),
        port=args.port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
