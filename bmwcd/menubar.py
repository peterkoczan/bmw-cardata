"""macOS menu bar status and control for the streaming service.

Shows at a glance whether the subscriber and the database are up, how long ago
the car last said anything, and offers the whole setup path — BMW portal
instructions, authorisation, retention — without dropping to a terminal.

Control goes through launchd rather than signalling the process directly, so the
supervisor's own view of the job stays correct.
"""

import os
import subprocess
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import rumps

from . import auth, config, db

LABEL = "nl.koczan.bmw-cardata.stream"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
POLL_SECONDS = 10

# A car silent this long is probably just parked, but it is the number worth
# eyeballing, so surface it rather than hiding it.
STALE_AFTER = 6 * 3600

PORTAL_STEPS = """Set this up in the My BMW portal first (about 3 minutes).

1. Sign in at bmw.<your country> → account menu → Vehicle overview.
2. Under your car, open BMW CarData, and accept the terms.
   Note: accepting also commits you to changes coming into force
   over the following six weeks.
3. Click "Create CarData client". Copy the Client ID it shows.
4. Turn ON both toggles:
      • Request access to the CarData API
      • CarData stream
   Without the stream toggle, sign-in succeeds and MQTT then
   rejects you with nothing useful in the error.
5. Click "Configure data stream", tick the attributes you want
   (ticking everything is fine), then "Submit and start stream".
   Repeat per vehicle — the stream is configured per VIN.

Do NOT click "Authenticate device" in the portal. This app runs
that step itself.

Now paste the Client ID below."""


def _sh(*args) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def _launchctl_jobs() -> dict[str, str]:
    """label -> pid column, from `launchctl list`."""
    code, out = _sh("launchctl", "list")
    jobs = {}
    if code != 0:
        return jobs
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            jobs[parts[2]] = parts[0]
    return jobs


def _stream_pid() -> int | None:
    pid = _launchctl_jobs().get(LABEL)
    return int(pid) if pid and pid.isdigit() else None


def _is_loaded() -> bool:
    return LABEL in _launchctl_jobs()


def _copy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


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
        self.configured = False
        self.authorised = False
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
        if not self.configured or not self.authorised:
            return "⚙️"
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
    st.configured = cfg is not None
    st.authorised = config.TOKEN_PATH.exists()
    st.stream_pid = _stream_pid()
    st.loaded = _is_loaded()
    if cfg is None:
        return st
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
        first = str(exc).strip().splitlines()
        st.db_error = first[0][:70] if first else "unreachable"
    return st


class App(rumps.App):
    def __init__(self):
        super().__init__("BMW", title="⚙️", quit_button=None)
        self.cfg = self._load_config()

        self.item_stream = rumps.MenuItem("Stream: …")
        self.item_db = rumps.MenuItem("Database: …")
        self.item_last = rumps.MenuItem("Last message: …")
        self.item_rows = rumps.MenuItem("Rows: …")
        self.item_retention = rumps.MenuItem("Retention…", callback=self.set_retention)
        self.menu = [
            self.item_stream,
            self.item_db,
            self.item_last,
            self.item_rows,
            None,
            rumps.MenuItem("Open map", callback=self.open_map),
            None,
            rumps.MenuItem("Set up / re-authorise…", callback=self.setup),
            self.item_retention,
            None,
            rumps.MenuItem("Restart stream", callback=self.restart),
            rumps.MenuItem("Stop stream", callback=self.stop),
            rumps.MenuItem("Start stream", callback=self.start),
            None,
            rumps.MenuItem("Open log", callback=self.open_log),
            rumps.MenuItem("Quit indicator", callback=self.quit),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, POLL_SECONDS).start()

    @staticmethod
    def _load_config():
        # Must tolerate no config at all: the agent is installed before setup has
        # been run, and crashing here would crash-loop under KeepAlive.
        try:
            return config.load() if config.exists() else None
        except Exception:  # noqa: BLE001
            return None

    # ---- display ----------------------------------------------------------

    def refresh(self, _):
        st = poll(self.cfg)
        self.title = st.glyph

        if not st.configured:
            self.item_stream.title = "Not set up — use “Set up / re-authorise…”"
        elif not st.authorised:
            self.item_stream.title = "Not authorised — use “Set up / re-authorise…”"
        elif st.stream_pid is not None:
            self.item_stream.title = f"Stream: running (pid {st.stream_pid})"
        elif st.loaded:
            self.item_stream.title = "Stream: loaded but not running"
        else:
            self.item_stream.title = "Stream: stopped"

        self.item_db.title = (
            "Database: up" if st.db_up
            else f"Database: down — {st.db_error}" if st.configured
            else "Database: —"
        )
        silent = st.silent_for
        self.item_last.title = (
            f"Last message: {_human_age(silent)}" if silent is not None
            else "Last message: none recorded"
        )
        self.item_rows.title = f"Rows: {st.rows:,} · {st.keys} keys"
        self.item_retention.title = (
            f"Retention: {self.cfg.retention_days} days…" if self.cfg else "Retention…"
        )

    def _note(self, title: str, message: str = ""):
        try:
            rumps.notification("BMW CarData", title, message[:200])
        except Exception:  # noqa: BLE001 - notifications need a bundled app
            rumps.alert(title=title, message=message[:400] or " ")

    # ---- setup ------------------------------------------------------------

    def setup(self, _):
        current = self.cfg.client_id if self.cfg else ""
        if current.startswith("0000"):
            current = ""
        window = rumps.Window(
            message=PORTAL_STEPS,
            title="BMW CarData setup",
            default_text=current,
            ok="Authorise",
            cancel="Cancel",
            dimensions=(340, 24),
        )
        response = window.run()
        if not response.clicked:
            return
        client_id = response.text.strip()
        if not client_id:
            return self._note("No Client ID entered")

        config.ensure()
        config.set_value("client_id", client_id)
        self.cfg = self._load_config()
        if self.cfg is None:
            return self._note("Could not read config.toml")

        store = auth.TokenStore(config.TOKEN_PATH, self.cfg.client_id)
        try:
            body, verifier = auth.request_device_code(store)
        except Exception as exc:  # noqa: BLE001
            return self._note("Could not start authorisation", str(exc))

        code = body["user_code"]
        uri = auth.verification_uri(body)
        _copy(code)
        webbrowser.open(uri)

        # Poll in the background so the code stays on screen while BMW waits for
        # the approval. The window below is modal; the thread keeps working.
        result = {}

        def worker():
            try:
                result["tokens"] = auth.poll_for_tokens(store, body, verifier)
            except BaseException as exc:  # noqa: BLE001 - SystemExit included
                result["error"] = str(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        rumps.alert(
            title="Approve in the browser",
            message=(
                f"Code {code}  (already copied to your clipboard)\n\n"
                "A browser window is open at BMW's device-link page.\n"
                "Sign in, paste the code, and approve.\n\n"
                "Finish the browser steps completely, then click OK here."
            ),
            ok="OK",
        )

        thread.join(timeout=120)
        if "tokens" in result:
            self.cfg = self._load_config()
            self._note("Authorised", f"GCID {result['tokens'].gcid}")
            self.restart(None)
        elif "error" in result:
            self._note("Authorisation failed", result["error"])
        else:
            self._note("Still waiting", "Approval not detected yet; check the log.")
        self.refresh(None)

    def set_retention(self, _):
        if self.cfg is None:
            return self._note("Not set up yet")
        window = rumps.Window(
            message=(
                "How many days of telemetry to keep in the database.\n\n"
                "Raw JSONL is kept for twice this by default, so the database "
                "can be rebuilt from disk if needed.\n\n"
                "Applied nightly at 04:00, or immediately if you confirm below."
            ),
            title="Retention",
            default_text=str(self.cfg.retention_days),
            ok="Save",
            cancel="Cancel",
            dimensions=(80, 24),
        )
        response = window.run()
        if not response.clicked:
            return
        try:
            days = int(response.text.strip())
            if days < 1:
                raise ValueError
        except ValueError:
            return self._note("Retention unchanged", "Enter a whole number of days.")

        config.set_value("retention_days", days)
        self.cfg = self._load_config()
        self.refresh(None)

        if rumps.alert(
            title="Apply now?",
            message=f"Keep {days} days. Delete anything older right away?",
            ok="Delete now",
            cancel="Later",
        ):
            try:
                deleted = db.prune(self.cfg)
                self._note("Retention applied", f"{deleted} row(s) deleted")
            except Exception as exc:  # noqa: BLE001
                self._note("Prune failed", str(exc))

    # ---- actions ----------------------------------------------------------

    def _after(self, title: str, code: int, out: str):
        self.refresh(None)  # reflect reality now, not at the next tick
        if code != 0:
            self._note(title, out or "failed")

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
            return self._note("No agent installed", "Run ./launchd/install.sh")
        code, out = _sh("launchctl", "bootstrap", DOMAIN, str(PLIST))
        self._after("Start failed", code, out)

    def open_map(self, _):
        """Rebuild from what is in the database right now, then open it."""
        if self.cfg is None:
            return self._note("Not set up yet")
        from . import export

        try:
            page, _data = export.render(self.cfg)
        except Exception as exc:  # noqa: BLE001
            return self._note("Could not build the map", str(exc))
        _sh("open", str(page))

    def open_log(self, _):
        base = self.cfg.data_dir if self.cfg else config.ROOT / "data"
        log = base / "logs" / "stream.log"
        if log.exists():
            _sh("open", "-a", "Console", str(log))
        else:
            self._note("No log yet", str(log))

    def quit(self, _):
        # KeepAlive would resurrect us immediately, so bootout the agent rather
        # than just exiting -- otherwise "Quit" does nothing visible.
        _sh("launchctl", "bootout", f"{DOMAIN}/nl.koczan.bmw-cardata.menubar")
        rumps.quit_application()


def run() -> None:
    App().run()
