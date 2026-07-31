"""macOS menu bar status and control for the streaming service.

Shows at a glance whether the subscriber and the database are up, how long ago
the car last said anything, and offers the whole setup path — BMW portal
instructions, authorisation, retention — without dropping to a terminal.

Control goes through launchd rather than signalling the process directly, so the
supervisor's own view of the job stays correct.
"""

import json
import os
import subprocess
import threading
import time
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

ICONS = Path(__file__).resolve().parent.parent / "assets" / "icons"

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


def _launchctl_jobs() -> dict[str, str] | None:
    """label -> pid column, or None if launchctl itself could not be read.

    None is distinct from {} on purpose: a launchctl that timed out means we do
    not know the state, which should read as unknown rather than as stopped.
    """
    code, out = _sh("launchctl", "list")
    if code != 0:
        return None
    jobs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            jobs[parts[2]] = parts[0]
    return jobs


def _is_loaded() -> bool:
    jobs = _launchctl_jobs()
    return bool(jobs) and LABEL in jobs


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


# A heartbeat older than this means the subscriber is not maintaining it, even
# if the process is alive. It refreshes every 30s while connected.
HEARTBEAT_STALE = 120


class Status:
    def __init__(self):
        self.configured = False
        self.authorised = False
        self.known = True          # could we read launchctl at all?
        self.stream_pid: int | None = None
        self.loaded = False
        self.stream_state = ""     # from the heartbeat file
        self.stream_detail = ""
        self.heartbeat_age: float | None = None
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
    def connected(self) -> bool:
        """Actually subscribed, not merely running.

        A live PID proves nothing: backoff loops, auth retry loops and DNS
        failures all keep the process alive with the stream down.
        """
        if self.stream_state != "connected":
            return False
        return self.heartbeat_age is not None and self.heartbeat_age < HEARTBEAT_STALE

    @property
    def glyph(self) -> str:
        if not self.configured or not self.authorised:
            return "⚙️"
        if not self.known:
            return "⚪"
        if self.stream_pid is None or not self.connected:
            return "🔴"
        if not self.db_up:
            return "🟠"
        silent = self.silent_for
        if silent is None or silent > STALE_AFTER:
            return "🟡"
        return "🟢"

    @property
    def colour(self) -> str:
        """Icon variant name, parallel to `glyph`."""
        return {
            "🟢": "green", "🟡": "yellow", "🟠": "orange",
            "🔴": "red", "⚪": "grey", "⚙️": "grey",
        }.get(self.glyph, "grey")

    @property
    def stream_summary(self) -> str:
        if not self.configured:
            return "Not set up — use “Set up / re-authorise…”"
        if not self.authorised:
            return "Not authorised — use “Set up / re-authorise…”"
        if not self.known:
            return "Stream: unknown (launchctl unavailable)"
        if self.stream_pid is None:
            return "Stream: stopped" if not self.loaded else "Stream: loaded, not running"
        if self.connected:
            return f"Stream: connected (pid {self.stream_pid})"
        if self.stream_state and self.heartbeat_age is not None:
            detail = f" — {self.stream_detail}" if self.stream_detail else ""
            return f"Stream: {self.stream_state}{detail}"
        return f"Stream: running but not connected (pid {self.stream_pid})"


def _read_heartbeat(cfg) -> tuple[str, str, float | None]:
    try:
        body = json.loads((cfg.data_dir / "stream.status").read_text())
        return body.get("state", ""), body.get("detail", ""), time.time() - float(body["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return "", "", None


def poll(cfg, with_counts: bool = True) -> Status:
    st = Status()
    st.configured = cfg is not None
    st.authorised = config.TOKEN_PATH.exists()

    jobs = _launchctl_jobs()
    st.known = jobs is not None
    if jobs is not None:
        st.loaded = LABEL in jobs
        pid = jobs.get(LABEL)
        st.stream_pid = int(pid) if pid and pid.isdigit() else None

    if cfg is None:
        return st
    st.stream_state, st.stream_detail, st.heartbeat_age = _read_heartbeat(cfg)

    # Short timeouts: this runs on the UI thread, and an unbounded wait here
    # freezes the menu bar item itself.
    try:
        with db.connect(cfg, connect_timeout=3, statement_timeout_ms=4000) as conn:
            if with_counts:
                row = conn.execute(
                    "SELECT max(ts), count(*), count(DISTINCT key) FROM telemetry"
                ).fetchone()
                st.rows, st.keys = (row[1], row[2]) if row else (0, 0)
            else:
                # max(ts) alone is an index-only scan; the counts are seq scans,
                # so they run on a slower cadence.
                row = conn.execute("SELECT max(ts) FROM telemetry").fetchone()
        st.db_up = True
        st.last_message = row[0].astimezone(timezone.utc) if row and row[0] else None
    except Exception as exc:  # noqa: BLE001 - the indicator must never crash
        st.db_up = False
        first = str(exc).strip().splitlines()
        st.db_error = first[0][:70] if first else "unreachable"
    return st


class App(rumps.App):
    def __init__(self):
        super().__init__("BMW", title="", quit_button=None)
        self.cfg = self._load_config()

        self.item_stream = rumps.MenuItem("Stream: …")
        self.item_db = rumps.MenuItem("Database: …")
        self.item_last = rumps.MenuItem("Last message: …")
        self.item_rows = rumps.MenuItem("Rows: …")
        self.item_retention = rumps.MenuItem("Retention…", callback=self.set_retention)
        self.item_rename = rumps.MenuItem("Rename vehicle")
        self._vins: list[str] = []
        self.menu = [
            self.item_stream,
            self.item_db,
            self.item_last,
            self.item_rows,
            None,
            rumps.MenuItem("Open map", callback=self.open_map),
            None,
            rumps.MenuItem("Set up / re-authorise…", callback=self.setup),
            self.item_rename,
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
        # Row/key counts are seq scans; only refresh them every few minutes.
        self._ticks = getattr(self, "_ticks", 0) + 1
        st = poll(self.cfg, with_counts=self._ticks % 30 == 1)
        if not st.db_up or st.rows:
            self._last_counts = (st.rows, st.keys)
        else:
            st.rows, st.keys = getattr(self, "_last_counts", (0, 0))

        self._show(st)

        # Tell the user the moment it breaks. A red glyph nobody is looking at
        # is worth no more than no glyph, and the data is unrecoverable.
        was = getattr(self, "_was_connected", None)
        if was is True and st.authorised and not st.connected:
            self._note("Stream down", st.stream_summary)
        self._was_connected = st.connected if st.authorised else None

        self.item_stream.title = st.stream_summary

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
        if st.db_up:
            self._refresh_vehicles()

    # ---- vehicles ---------------------------------------------------------

    def label_for(self, vin: str) -> str:
        """What to call this car: the configured name, else the VIN tail."""
        return (self.cfg.names.get(vin) if self.cfg else None) or vin[-6:]

    def _refresh_vehicles(self) -> None:
        """Keep the rename submenu in step with whatever has actually streamed.

        The VIN list is a grouped scan, so it rides the same slow cadence as the
        row counts. A car added in the BMW portal shows up here within a few
        minutes of its first message, with no config editing and no restart.
        """
        if self.cfg is None:
            return
        if self._ticks % 30 == 1 or not getattr(self, "_vins_loaded", False):
            try:
                found = db.vins(self.cfg)
            except Exception:  # noqa: BLE001 - the indicator must never crash
                return
            self._vins_loaded = True
        else:
            found = self._vins

        titles = [f"{self.label_for(v)} — {v}" for v in found]
        if found == self._vins and titles == getattr(self, "_vin_titles", None):
            return
        self._vins, self._vin_titles = found, titles

        self.item_rename.clear()
        if not found:
            self.item_rename.add(rumps.MenuItem("Nothing has streamed yet"))
            return
        for vin, title in zip(found, titles):
            item = rumps.MenuItem(title, callback=self._rename_vehicle)
            # rumps identifies a clicked item by its title, which is exactly what
            # this dialog lets you change; carry the VIN itself instead.
            item.vin = vin
            self.item_rename.add(item)

    def _rename_vehicle(self, sender) -> None:
        vin = getattr(sender, "vin", None)
        if vin is None or self.cfg is None:
            return
        current = self.cfg.names.get(vin, "") if self.cfg else ""
        window = rumps.Window(
            message=(
                f"What to call {vin} on the map.\n\n"
                "BMW does not stream a model name, so this is the only place a "
                "car gets a readable label.\n\n"
                f"Leave it empty to go back to the VIN tail ({vin[-6:]})."
            ),
            title="Rename vehicle",
            default_text=current,
            ok="Save",
            cancel="Cancel",
            dimensions=(220, 24),
        )
        response = window.run()
        if not response.clicked:
            return

        name = " ".join(response.text.split())
        try:
            config.set_name(vin, name)
        except OSError as exc:
            return self._note("Could not save the name", str(exc))
        self.cfg = self._load_config()
        if self.cfg is None:
            return self._note("Could not re-read config.toml")

        self._vins_loaded = False  # force the submenu titles to rebuild
        self.refresh(None)
        self._note("Renamed", f"{vin} is now {self.label_for(vin)}")

    def _show(self, st: "Status") -> None:
        """Coloured roundel when the icons exist, emoji when they do not.

        The icons are committed, so this normally takes the first branch. The
        fallback covers a checkout where they were deleted or regenerated badly
        -- an indicator that shows nothing at all is worse than one showing a
        coloured dot.
        """
        icon = ICONS / f"status-{st.colour}.png"
        if icon.exists():
            if getattr(self, "_icon_path", None) != str(icon):
                self.icon = str(icon)
                self._icon_path = str(icon)
            self.title = ""
        else:
            self.title = st.glyph

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

    def _after(self, title: str, code: int, out: str, expect_pid: bool = False):
        if code == 0 and expect_pid:
            # launchd has not necessarily spawned it yet. Refreshing immediately
            # would show "stopped" right after a successful start, inviting a
            # second click that then fails with "already bootstrapped".
            for _ in range(6):
                time.sleep(0.5)
                jobs = _launchctl_jobs()
                if jobs and jobs.get(LABEL, "-").isdigit():
                    break
        self.refresh(None)  # reflect reality now, not at the next tick
        if code != 0:
            self._note(title, out or "failed")

    def restart(self, _):
        # kickstart -k only works on a loaded job; bootstrap it first if needed.
        if not _is_loaded():
            return self.start(None)
        code, out = _sh("launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}")
        self._after("Restart failed", code, out, expect_pid=True)

    def stop(self, _):
        code, out = _sh("launchctl", "bootout", f"{DOMAIN}/{LABEL}")
        self._after("Stop failed", code, out)

    def start(self, _):
        if not PLIST.exists():
            return self._note("No agent installed", "Run ./launchd/install.sh")
        code, out = _sh("launchctl", "bootstrap", DOMAIN, str(PLIST))
        self._after("Start failed", code, out, expect_pid=True)

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


def _present_as_accessory() -> None:
    """No Dock icon, and our own icon where one is still shown.

    A menu bar utility has no business in the Dock or the ⌘-Tab switcher, and
    without an explicit icon everything falls back to the generic Python rocket.
    """
    try:
        from AppKit import NSApplication, NSImage

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
        icon = ICONS / "app.png"
        if icon.exists():
            image = NSImage.alloc().initWithContentsOfFile_(str(icon))
            if image:
                app.setApplicationIconImage_(image)
    except Exception:  # noqa: BLE001 - cosmetic only, never block startup
        pass


def run() -> None:
    _present_as_accessory()
    App().run()
