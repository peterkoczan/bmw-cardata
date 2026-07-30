"""macOS menu bar status and control for the streaming service.

Shows at a glance whether the subscriber and the database are up, how long ago
the car last said anything, and offers start/stop/restart without dropping to a
terminal. Control goes through launchd rather than signalling the process
directly, so the supervisor's own view of the world stays correct.
"""

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import rumps

from . import config, db

LABEL = "nl.koczan.bmw-cardata.stream"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
POLL_SECONDS = 10

# A car that has said nothing for this long is probably just parked, but it is
# the number worth eyeballing, so surface it rather than hiding it.
STALE_AFTER = 6 * 3600


def _sh(*args) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def _stream_pid() -> int | None:
    """PID from launchctl, or None if loaded-but-stopped / not loaded at all."""
    code, out = _sh("launchctl", "list")
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2] == LABEL:
            return int(parts[0]) if parts[0].isdigit() else None
    return None


def _is_loaded() -> bool:
    code, out = _sh("launchctl", "list")
    return code == 0 and any(line.endswith(LABEL) for line in out.splitlines())


def _human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


class Status:
    def __init__(self):
        self.stream_pid: int | None = None
        self.loaded = False
        self.db_up = False
        self.db_error = ""
        self.last_message: datetime | None = None
        self.rows = 0
        self.keys = 0

    @property
    def silent_for(self) -> float | None:
        if self.last_message is None:
            return None
        return (datetime.now(timezone.utc) - self.last_message).total_seconds()

    @property
    def glyph(self) -> str:
        if self.stream_pid is None:
            return "🔴"
        if not self.db_up:
            return "🟠"
        silent = self.silent_for
        if silent is None or silent > STALE_AFTER:
            return "🟡"
        return "🟢"


def poll(cfg) -> Status:
    st = Status()
    st.stream_pid = _stream_pid()
    st.loaded = _is_loaded()
    try:
        with db.connect(cfg) as conn:
            row = conn.execute(
                "SELECT max(ts), count(*), count(DISTINCT key) FROM telemetry"
            ).fetchone()
        st.db_up = True
        st.last_message = row[0].astimezone(timezone.utc) if row and row[0] else None
        st.rows = row[1] if row else 0
        st.keys = row[2] if row else 0
    except Exception as exc:  # noqa: BLE001 - the indicator must never crash
        st.db_up = False
        st.db_error = str(exc).strip().splitlines()[0][:80] if str(exc) else "unreachable"
    return st


class App(rumps.App):
    def __init__(self):
        super().__init__("BMW", title="⚪", quit_button=None)
        self.cfg = config.load()

        self.item_stream = rumps.MenuItem("Stream: …")
        self.item_db = rumps.MenuItem("Database: …")
        self.item_last = rumps.MenuItem("Last message: …")
        self.item_rows = rumps.MenuItem("Rows: …")
        self.menu = [
            self.item_stream,
            self.item_db,
            self.item_last,
            self.item_rows,
            None,
            rumps.MenuItem("Restart stream", callback=self.restart),
            rumps.MenuItem("Stop stream", callback=self.stop),
            rumps.MenuItem("Start stream", callback=self.start),
            None,
            rumps.MenuItem("Rebuild map", callback=self.rebuild_map),
            rumps.MenuItem("Open map", callback=self.open_map),
            rumps.MenuItem("Open log", callback=self.open_log),
            None,
            rumps.MenuItem("Quit indicator", callback=rumps.quit_application),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, POLL_SECONDS).start()

    # ---- display ----------------------------------------------------------

    def refresh(self, _):
        st = poll(self.cfg)
        self.title = st.glyph

        if st.stream_pid is not None:
            self.item_stream.title = f"Stream: running (pid {st.stream_pid})"
        elif st.loaded:
            self.item_stream.title = "Stream: loaded but not running"
        else:
            self.item_stream.title = "Stream: stopped"

        self.item_db.title = (
            "Database: up" if st.db_up else f"Database: down — {st.db_error}"
        )

        silent = st.silent_for
        self.item_last.title = (
            f"Last message: {_human_age(silent)}" if silent is not None
            else "Last message: none recorded"
        )
        self.item_rows.title = f"Rows: {st.rows:,} · {st.keys} keys"

    # ---- actions ----------------------------------------------------------

    def _after(self, title: str, code: int, out: str):
        # Reflect reality immediately rather than waiting for the next tick.
        self.refresh(None)
        if code != 0:
            rumps.notification("BMW CarData", title, out[:200] or "failed")

    def restart(self, _):
        # kickstart -k only works on a loaded job; bootstrap it first if needed.
        if not _is_loaded():
            return self.start(None)
        code, out = _sh("launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}")
        self._after("Restart failed", code, out)

    def stop(self, _):
        code, out = _sh("launchctl", "bootout", f"{DOMAIN}/{LABEL}")
        self._after("Stop failed", code, out)

    def start(self, _):
        if not PLIST.exists():
            return rumps.notification(
                "BMW CarData", "No agent installed", "Run ./launchd/install.sh"
            )
        code, out = _sh("launchctl", "bootstrap", DOMAIN, str(PLIST))
        self._after("Start failed", code, out)

    def rebuild_map(self, _):
        from . import export

        try:
            out, _data = export.render(self.cfg)
            rumps.notification("BMW CarData", "Map rebuilt", str(out))
        except Exception as exc:  # noqa: BLE001
            rumps.notification("BMW CarData", "Map rebuild failed", str(exc)[:200])

    def open_map(self, _):
        page = self.cfg.data_dir / "viz" / "map.html"
        if not page.exists():
            return self.rebuild_map(None)
        _sh("open", str(page))

    def open_log(self, _):
        log = self.cfg.data_dir / "logs" / "stream.log"
        if log.exists():
            _sh("open", "-a", "Console", str(log))


def run() -> None:
    App().run()
