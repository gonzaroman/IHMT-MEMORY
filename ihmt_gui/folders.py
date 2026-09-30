"""Choosing the folder where the memory lives.

A browser cannot open the system folder dialog (for security reasons), so
there are two paths and the interface uses whichever is available:

1. **Native dialog** — the backend launches ``tkinter.filedialog`` in a
   *subprocess*. The subprocess is deliberate: Tk cannot run outside the main
   thread, and the main thread here is the HTTP server.
2. **Built-in browser** — if Tk is not installed (common on Linux, where
   ``python3-tk`` is a separate package), the page itself lists folders with
   :func:`list_directories`.

:func:`list_directories` returns **directory names only**: never files or
their contents.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Maximum time to wait for the person to choose in the native dialog.
PICKER_TIMEOUT = 300.0

#: Native dialog script. It runs with ``python -c`` in a clean subprocess,
#: so Tk's event loop never touches the server's.
_PICKER_SCRIPT = """
import sys
import tkinter
import tkinter.filedialog

root = tkinter.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tkinter.TclError:
    pass
initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
chosen = tkinter.filedialog.askdirectory(initialdir=initial, title=sys.argv[2], mustexist=False)
root.destroy()
sys.stdout.write(chosen or "")
"""


@dataclass
class DirectoryListing:
    """Contents of a folder, as the built-in browser sees it."""

    path: str
    parent: Optional[str]
    directories: List[Dict[str, Any]]
    is_memory: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "parent": self.parent,
            "directories": self.directories,
            "is_memory": self.is_memory,
        }


def home() -> Path:
    """Home folder, the built-in browser's starting point."""
    return Path.home()


def has_memory(path: Path) -> bool:
    """Tell whether ``path`` already contains an IHMT memory."""
    return (path / "ihmt_memory" / "root.json").exists()


def list_directories(raw_path: Optional[str] = None, *, limit: int = 500) -> DirectoryListing:
    """List the subfolders of ``raw_path`` for the built-in browser.

    Args:
        raw_path: Folder to list. If missing or nonexistent, the home folder is used.
        limit: Maximum number of entries returned.

    Returns:
        A :class:`DirectoryListing` with the subfolders sorted and a flag on
        each one telling whether it already contains an IHMT memory.
    """
    path = Path(raw_path).expanduser() if raw_path else home()
    try:
        path = path.resolve()
    except OSError:
        path = home()
    if not path.is_dir():
        path = home()

    entries: List[Dict[str, Any]] = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if len(entries) >= limit:
                break
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
            except OSError:  # broken links, permissions
                continue
            entries.append({"name": child.name, "path": str(child), "is_memory": has_memory(child)})
    except PermissionError:
        entries = []

    parent = str(path.parent) if path.parent != path else None
    return DirectoryListing(
        path=str(path), parent=parent, directories=entries, is_memory=has_memory(path)
    )


def native_dialog_available() -> bool:
    """Tell whether the native folder dialog can be used."""
    try:
        import tkinter  # noqa: F401
        import tkinter.filedialog  # noqa: F401
    except Exception:
        return False
    return True


def pick_directory_native(initial: Optional[str] = None, *, title: str = "IHMT") -> Optional[str]:
    """Open the system folder dialog and return what was chosen.

    Args:
        initial: Folder to open the dialog in.
        title: Window title.

    Returns:
        The chosen path, or ``None`` if it was cancelled, if Tk is not
        available or if the dialog failed. Never raises: the interface can
        always fall back to the built-in browser.
    """
    if not native_dialog_available():
        return None

    start = str(Path(initial).expanduser()) if initial else ""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PICKER_SCRIPT, start, title],
            capture_output=True,
            text=True,
            timeout=PICKER_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    chosen = result.stdout.strip()
    return chosen or None
