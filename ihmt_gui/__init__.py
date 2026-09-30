"""Local graphical interface for IHMT.

Starts an HTTP server on ``127.0.0.1`` that serves a static page and a
handful of JSON endpoints built **only on IHMT's public API**. It adds no
dependencies: everything comes from the standard library.

Usage::

    python3 gui.py                      # opens the browser
    python3 gui.py --path ./other_folder
    python3 gui.py --no-browser --port 8765

Two things the interface does that the terminal does not:

* a **setup wizard** that picks the memory folder and explains what
  registering it for one project or for all of them implies;
* a **viewer** for the tree, the timeline and the contradictions, so the
  memory stops being a black box of files.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__", "serve", "GuiServer"]


def __getattr__(name: str):  # pragma: no cover - lazy import
    """Avoid loading the server (and IHMT with it) when importing the package."""
    if name in ("serve", "GuiServer"):
        from .server import GuiServer, serve

        return {"serve": serve, "GuiServer": GuiServer}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
