"""Local HTTP server for the interface.

Listens **only** on ``127.0.0.1`` and protects all of ``/api/*`` with a random
token generated at startup, carried in the URL that opens in the browser.
Static files (HTML, CSS, JS) are served without the token because they hold
no data: the data sits behind the API.

Defences, and why they are there:

* **Token** — stops any other process or tab on the machine from querying your
  memory just because it knows the port.
* **``Host`` and ``Origin`` checks** — prevent *DNS rebinding*, through which
  an external website could make your browser talk to this server.
* **``POST`` required for writes** — no action that changes anything can be
  triggered by a plain navigation.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .handlers import READ_ROUTES, WRITE_ROUTES, ApiError, GuiApi, build_api

logger = logging.getLogger("ihmt.gui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: MIME types of the little we serve.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

#: Header the page sends the token in.
TOKEN_HEADER = "X-IHMT-Token"

#: Maximum size of a POST body (ours are tiny).
MAX_BODY = 256 * 1024


class GuiRequestHandler(BaseHTTPRequestHandler):
    """Minimal router: static files on one side, ``/api/*`` on the other."""

    server_version = "IHMT-GUI"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- helpers
    @property
    def api(self) -> GuiApi:
        return self.server.api  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the access log except in debug mode."""
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, code: str, detail: str = "") -> None:
        self._send_json(status, {"error": code, "detail": detail})

    # --------------------------------------------------------------- security
    def _host_is_local(self) -> bool:
        """Reject requests that are not addressed to the loopback interface."""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parts = urlparse(origin)
            if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _token_ok(self, query: Dict[str, list]) -> bool:
        """Check the token, which may come in a header or in the URL."""
        received = self.headers.get(TOKEN_HEADER) or (query.get("t") or [""])[0]
        return secrets.compare_digest(received, self.token)

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802
        parts = urlparse(self.path)
        query = parse_qs(parts.query)

        if not self._host_is_local():
            self._send_error_json(HTTPStatus.FORBIDDEN, "bad_host")
            return

        if not parts.path.startswith("/api/"):
            self._serve_static(parts.path)
            return

        if not self._token_ok(query):
            self._send_error_json(HTTPStatus.FORBIDDEN, "bad_token")
            return

        route = READ_ROUTES.get(parts.path)
        if route is None:
            if parts.path in WRITE_ROUTES:
                self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "post_required")
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "unknown_endpoint", parts.path)
            return

        name, params = route
        arguments = {
            target: (query.get(source) or [None])[0] for source, target in params.items()
        }
        self._dispatch(name, arguments)

    def do_POST(self) -> None:  # noqa: N802
        parts = urlparse(self.path)
        query = parse_qs(parts.query)

        if not self._host_is_local():
            self._send_error_json(HTTPStatus.FORBIDDEN, "bad_host")
            return
        if not self._token_ok(query):
            self._send_error_json(HTTPStatus.FORBIDDEN, "bad_token")
            return

        route = WRITE_ROUTES.get(parts.path)
        if route is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown_endpoint", parts.path)
            return

        body = self._read_body()
        if body is None:
            return

        name, params = route
        arguments = {target: body.get(source) for source, target in params.items()}
        self._dispatch(name, arguments)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _read_body(self) -> Optional[Dict[str, Any]]:
        """Read and validate the JSON body of a POST."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad_length")
            return None
        if length > MAX_BODY:
            self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large")
            return None
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad_json")
            return None
        if not isinstance(data, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad_json")
            return None
        return data

    def _dispatch(self, name: str, arguments: Dict[str, Any]) -> None:
        """Call the :class:`GuiApi` method and serialize whatever it returns."""
        method = getattr(self.api, name, None)
        if method is None:  # pragma: no cover - malformed route table
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown_endpoint", name)
            return
        clean = {k: v for k, v in arguments.items() if v is not None}
        try:
            self._send_json(HTTPStatus.OK, method(**clean))
        except ApiError as exc:
            self._send_json(exc.status, exc.to_dict())
        except TypeError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad_arguments", str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("failure in %s", name)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(exc))

    # ----------------------------------------------------------------- static
    def _serve_static(self, path: str) -> None:
        """Serve the page. Never lets a request out of ``static/``."""
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            self._send_error_json(HTTPStatus.FORBIDDEN, "outside_static")
            return
        if not target.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", name)
            return
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


class GuiServer(ThreadingHTTPServer):
    """Local server holding the interface state and its token."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, api: GuiApi, *, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), GuiRequestHandler)
        self.api = api
        self.token = secrets.token_urlsafe(24)

    @property
    def port(self) -> int:
        """Port actually assigned (useful when 0 was requested)."""
        return self.server_address[1]

    @property
    def url(self) -> str:
        """Full URL, token included, ready to open in the browser."""
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start_background(self) -> threading.Thread:
        """Start in a separate thread (used by the tests)."""
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread


def create_server(
    memory_home: Path | str,
    project_dir: Path | str,
    *,
    port: int = 0,
) -> GuiServer:
    """Build the server without starting it."""
    return GuiServer(build_api(memory_home, project_dir), port=port)


def serve(
    memory_home: Path | str,
    project_dir: Path | str,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> int:
    """Start the interface and block until interrupted.

    Args:
        memory_home: Folder that contains (or will contain) ``ihmt_memory``.
        project_dir: Project the "this project only" scope acts on.
        port: Port; ``0`` lets the system pick a free one.
        open_browser: Open the browser automatically.

    Returns:
        Process exit code.
    """
    try:
        server = create_server(memory_home, project_dir, port=port)
    except OSError as exc:
        print(f"could not open port {port}: {exc}", file=sys.stderr)
        return 1

    # Explicit flush: if output goes to a file or a pipe, the buffer would hold
    # back the URL and whoever launched the process would not know where to go.
    print(f"IHMT · interface at {server.url}", flush=True)
    print("   (reachable from this machine only; Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(server.url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ninterface stopped")
    finally:
        server.shutdown()
        server.server_close()
    return 0
