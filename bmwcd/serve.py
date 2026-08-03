"""A tiny read-only HTTP server for the live map.

The map page normally ships as one self-contained file opened straight off disk,
which is the right shape for something you look at once. It is the wrong shape
for something you leave open while driving: a file:// page cannot fetch a
sibling JSON file, so it has no way to learn that anything changed.

Served over http instead, the page can poll. Two endpoints rather than one:

    /stamp.json   the latest timestamp and row count -- one indexed query
    /data.json    the full export, only fetched once the stamp has moved

Bound to 127.0.0.1 so nothing outside this machine can reach it, GET only, and
serving three fixed routes rather than a directory, so there is no path to
traverse. It holds no credentials and writes nothing.
"""

import json
import threading
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, export
from .config import Config

HOST = "127.0.0.1"

# Browsers ask for /favicon.ico on their own, whatever the page's <link> says,
# and a 404 there is enough for some of them to give up and show the blank
# default. Served from the same mark the page carries inline.
FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<circle cx='32' cy='32' r='30' fill='#0f1115'/>"
    "<path d='M32 4a28 28 0 0 1 28 28H32z' fill='#3b82f6'/>"
    "<path d='M32 32h28a28 28 0 0 1-28 28z' fill='#e6e9ef'/>"
    "<path d='M32 32v28A28 28 0 0 1 4 32z' fill='#3b82f6'/>"
    "<path d='M4 32A28 28 0 0 1 32 4v28z' fill='#e6e9ef'/>"
    "<circle cx='32' cy='32' r='30' fill='none' stroke='#0f1115' stroke-width='5'/>"
    "</svg>"
).encode()


def _stamp(cfg: Config) -> dict:
    """Cheap freshness probe: what is the newest row, and how many are there.

    max(ts) alone is an index-only scan. The count is there to catch a backfill
    that lands rows behind the newest one -- `bmwcd load` replaying a JSONL file
    moves the count without moving the maximum.
    """
    with db.connect(cfg, connect_timeout=3, statement_timeout_ms=4000) as conn:
        row = conn.execute("SELECT max(ts), count(*) FROM telemetry").fetchone()
    latest, rows = (row or (None, 0))
    return {
        "latest": latest.isoformat() if latest else None,
        "rows": rows,
        "at": datetime.now().astimezone().isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    _get_cfg = staticmethod(lambda: None)  # replaced by serve()
    server_version = "bmwcd"
    sys_version = ""

    def log_message(self, *args):  # noqa: A003 - silence the default stderr spam
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The whole point is freshness; a cached /stamp.json would defeat it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(
            json.dumps(payload, separators=(",", ":"), default=str).encode(),
            "application/json; charset=utf-8",
            code,
        )

    def _days(self) -> int | None:
        """How much history to send. ?days=0 means all of it."""
        query = parse_qs(urlparse(self.path).query)
        raw = query.get("days", [None])[0]
        if raw is None:
            return self.cfg.map_days or None
        try:
            return int(raw) or None
        except ValueError:
            return self.cfg.map_days or None

    @property
    def cfg(self) -> Config:
        """The *current* config, resolved per request.

        Not a Config captured when the handler class was built: every settings
        edit in the menu bar (rename, re-auth, retention) replaces that object,
        so a captured one went stale the moment anything was changed and the
        live map kept serving the old vehicle names until the agent restarted.
        """
        return type(self)._get_cfg()

    def _host_allowed(self) -> bool:
        """Only answer to a loopback name.

        Binding to 127.0.0.1 stops off-machine TCP; it does not stop a browser
        on this machine being pointed here by a hostname that resolves to
        127.0.0.1 (DNS rebinding). The port is a fixed default and the payload
        is every fix in the window plus the VINs, so the Host header is the
        check that actually distinguishes "someone here" from "some page".
        """
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.strip("[]") in {"127.0.0.1", "localhost", "::1", ""}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        # An Origin at all means a cross-site page made this request; same-origin
        # navigations and fetches from the map itself do not carry one.
        if not self._host_allowed() or self.headers.get("Origin"):
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                html = export.TEMPLATE.read_text().replace(
                    "/*__DATA__*/null",
                    json.dumps(
                        export.build(self.cfg, self._days()),
                        separators=(",", ":"),
                        default=str,
                    ),
                )
                self._send(html.encode(), "text/html; charset=utf-8")
            elif path == "/data.json":
                self._json(export.build(self.cfg, self._days()))
            elif path == "/stamp.json":
                self._json(_stamp(self.cfg))
            elif path in ("/favicon.ico", "/favicon.svg"):
                self._send(FAVICON, "image/svg+xml")
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass  # the page navigated away mid-response; nothing to report
        except Exception as exc:  # noqa: BLE001 - a request must not kill the server
            self._json({"error": str(exc)[:200]}, 500)

    do_HEAD = do_GET


def serve(cfg: Config | Callable[[], Config], port: int) -> ThreadingHTTPServer:
    """Start the server on a background thread and return it.

    `cfg` may be a callable returning the current Config. Pass one from any
    long-lived process: settings edits replace the Config object rather than
    mutating it, so a handler holding the original serves stale names.
    """
    get_cfg = cfg if callable(cfg) else (lambda: cfg)
    handler = type("BoundHandler", (Handler,), {"_get_cfg": staticmethod(get_cfg)})
    httpd = ThreadingHTTPServer((HOST, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def url_for(httpd: ThreadingHTTPServer) -> str:
    return f"http://{HOST}:{httpd.server_address[1]}/"
