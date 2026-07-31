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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, export
from .config import Config

HOST = "127.0.0.1"


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
    cfg: Config = None  # set by serve()
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

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        # Ignore any query string: it is only ever a cache-buster.
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                html = export.TEMPLATE.read_text().replace(
                    "/*__DATA__*/null",
                    json.dumps(
                        export.build(self.cfg), separators=(",", ":"), default=str
                    ),
                )
                self._send(html.encode(), "text/html; charset=utf-8")
            elif path == "/data.json":
                self._json(export.build(self.cfg))
            elif path == "/stamp.json":
                self._json(_stamp(self.cfg))
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass  # the page navigated away mid-response; nothing to report
        except Exception as exc:  # noqa: BLE001 - a request must not kill the server
            self._json({"error": str(exc)[:200]}, 500)

    do_HEAD = do_GET


def serve(cfg: Config, port: int) -> ThreadingHTTPServer:
    """Start the server on a background thread and return it."""
    handler = type("BoundHandler", (Handler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer((HOST, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def url_for(httpd: ThreadingHTTPServer) -> str:
    return f"http://{HOST}:{httpd.server_address[1]}/"
