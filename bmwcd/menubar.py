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

SETUP_GUIDE_URL = "https://peterkoczan.github.io/bmw-cardata/setup.html"

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

Full walkthrough with the gotchas:
https://peterkoczan.github.io/bmw-cardata/setup.html

Now paste the Client ID below."""


def _sh(*args, timeout: float = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


# kickstart -k and bootout wait on the job, and the stream agent has a
# ThrottleInterval of 30s, so launchd will not bring it back before then.
# Fifteen seconds meant a restart that was working perfectly reported "timed
# out" every time.
JOB_CONTROL_TIMEOUT = 45


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

# How often to repeat "stream down" while it stays down. Once was not enough:
# the notification that mattered was missed, and nothing said it again for a day.
DOWN_REMINDER_SECONDS = 3600


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


def _read_heartbeat(cfg) -> dict:
    """The stream's own view of itself: state, age, and when the car last spoke.

    Everything the indicator needs on its fast tick comes from this one small
    file, so the common case costs a read of a few hundred bytes instead of a
    round trip to Postgres.
    """
    try:
        body = json.loads((cfg.data_dir / "stream.status").read_text())
        return {
            "state": body.get("state", ""),
            "detail": body.get("detail", ""),
            "age": time.time() - float(body["at"]),
            "last_message": body.get("last_message"),
            "messages": body.get("messages") or 0,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "", "detail": "", "age": None, "last_message": None, "messages": 0}


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
    beat = _read_heartbeat(cfg)
    st.stream_state = beat["state"]
    st.stream_detail = beat["detail"]
    st.heartbeat_age = beat["age"]
    # From the file, not the database. The stream stamps this the moment it
    # stores a message, so the blink is as prompt as it ever was while the fast
    # tick no longer touches Postgres at all.
    if beat["last_message"]:
        st.last_message = datetime.fromtimestamp(beat["last_message"], timezone.utc)

    # The row counts are the only thing left that needs a query, and they are
    # cosmetic, so they ride the slow cadence. Short timeouts because this runs
    # on the UI thread and an unbounded wait freezes the indicator itself.
    if not with_counts:
        # Believe the heartbeat about the database too: the stream reports
        # "connected" only while it is storing what it receives.
        st.db_up = st.stream_state in {"connected", ""} or st.db_up
        return st
    try:
        with db.connect(cfg, connect_timeout=3, statement_timeout_ms=4000) as conn:
            row = conn.execute(
                "SELECT max(ts), count(*), count(DISTINCT key) FROM telemetry"
            ).fetchone()
            st.rows, st.keys = (row[1], row[2]) if row else (0, 0)
        st.db_up = True
        # The database is authoritative when we have asked it; the heartbeat is
        # only a stand-in between asks.
        if row and row[0]:
            st.last_message = row[0].astimezone(timezone.utc)
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
            rumps.MenuItem("Setup guide (web)", callback=self.open_guide),
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
        # Serving from launch, so a map tab left open overnight picks straight
        # up again. Never fatal: no map is a far smaller problem than no
        # indicator, and the port may simply be taken by an older copy.
        try:
            self._ensure_server()
        except Exception as exc:  # noqa: BLE001
            print(f"[menubar] map server not started: {exc}")

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
        # Report the outcome of a start/stop/restart, which ran on its own
        # thread. Done here so the notification is raised from the UI thread.
        pending = getattr(self, "_pending", None)
        if pending is not None:
            self._pending = None
            title, code, out = pending
            if code != 0:
                self._note(title, out or "failed")

        # Row/key counts are seq scans; only refresh them every few minutes.
        self._ticks = getattr(self, "_ticks", 0) + 1
        st = poll(self.cfg, with_counts=self._ticks % 30 == 1)
        if not st.db_up or st.rows:
            self._last_counts = (st.rows, st.keys)
        else:
            st.rows, st.keys = getattr(self, "_last_counts", (0, 0))

        self._show(st)

        # Blink when the car says something. The glyph otherwise sits green for
        # hours whether the stream is delivering or merely connected, and those
        # look identical until you go and read the log.
        seen = getattr(self, "_last_seen_message", None)
        if seen is not None and st.last_message is not None and st.last_message > seen:
            self._blink()
        self._last_seen_message = st.last_message

        # Tell the user it is broken, not merely that it broke. This only fired
        # on a transition observed by *this* process, so an indicator that
        # started up with the stream already down said nothing at all and just
        # sat red -- and restarting the indicator wiped the memory that would
        # have made it speak. The stream was dead for 29 hours behind exactly
        # that gap. Now: say it once on discovery, then keep saying it hourly,
        # because a notification missed while away is a notification wasted.
        if st.authorised and not st.connected:
            since = getattr(self, "_down_since", None) or time.monotonic()
            self._down_since = since
            last = getattr(self, "_down_notified", None)
            if last is None or time.monotonic() - last >= DOWN_REMINDER_SECONDS:
                self._down_notified = time.monotonic()
                down_for = _human_age(time.monotonic() - since) if last else "just now"
                self._note("Stream down", f"{st.stream_summary} ({down_for})")
        else:
            self._down_since = None
            self._down_notified = None

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

        # Rebuilding a menu is Cocoa work on the UI thread and this runs from
        # __init__, where an exception takes the whole indicator down and
        # KeepAlive then crash-loops it. A missing submenu is a far smaller
        # problem than no menu bar item at all.
        try:
            # rumps only creates the backing NSMenu when something is first added
            # to a submenu, and clear() dereferences it unguarded -- so clearing
            # one that has never held an item raises. len() reads the dict, not
            # the NSMenu, so it is safe on a fresh item.
            if len(self.item_rename):
                self.item_rename.clear()
            if not found:
                self.item_rename.add(rumps.MenuItem("Nothing has streamed yet"))
                return
            for vin, title in zip(found, titles):
                item = rumps.MenuItem(title, callback=self._rename_vehicle)
                # rumps identifies a clicked item by its title, which is exactly
                # what this dialog lets you change; carry the VIN itself instead.
                item.vin = vin
                self.item_rename.add(item)
        except Exception:  # noqa: BLE001 - the indicator must never crash
            self._vins, self._vin_titles = [], None  # retry on the next tick

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
        # Track the true state even mid-blink, so the blink settles back to
        # whatever the status is by the time it finishes rather than to whatever
        # it was when it started.
        self._resting_colour = st.colour
        # A blink in progress owns the icon. Without this the next poll would
        # paint the steady colour straight back over it.
        if getattr(self, "_blinks_left", 0):
            return
        self._set_icon(st.colour)
        if not (ICONS / f"status-{st.colour}.png").exists():
            self.title = st.glyph

    def _set_icon(self, colour: str) -> None:
        icon = ICONS / f"status-{colour}.png"
        if not icon.exists():
            return
        if getattr(self, "_icon_path", None) != str(icon):
            self.icon = str(icon)
            self._icon_path = str(icon)
        self.title = ""

    def _blink(self) -> None:
        """Flash the icon brighter for a moment, then settle back.

        Driven by its own short timer rather than the 10s status poll: a blink
        that lasts until the next poll is not a blink, it is a colour change.
        """
        if not (ICONS / "status-flash.png").exists():
            return
        self._resting_colour = getattr(self, "_resting_colour", "green")
        already_blinking = getattr(self, "_blinks_left", 0) > 0
        self._blinks_left = 4  # on, off, on, off
        if already_blinking:
            return  # let the running timer carry the extra flashes
        self._blink_timer = rumps.Timer(self._blink_tick, 0.22)
        self._blink_timer.start()

    def _blink_tick(self, _timer) -> None:
        left = getattr(self, "_blinks_left", 0)
        if left <= 0:
            self._blinks_left = 0
            try:
                self._blink_timer.stop()
            except Exception:  # noqa: BLE001 - never take the indicator down
                pass
            self._set_icon(getattr(self, "_resting_colour", "green"))
            return
        self._blinks_left = left - 1
        # Odd counts are the lit half of the cycle.
        self._set_icon("flash" if left % 2 == 0 else getattr(self, "_resting_colour", "green"))

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
        if expect_pid:
            # launchd has not necessarily spawned it yet. Reporting immediately
            # would show "stopped" right after a successful start, inviting a
            # second click that then fails with "already bootstrapped".
            #
            # The wait runs even when launchctl reported an error, because that
            # is exactly when the answer matters: the stream agent throttles for
            # 30s, kickstart blocks for the duration, and a restart that was
            # working perfectly used to come back as "timed out".
            deadline = time.monotonic() + JOB_CONTROL_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(0.5)
                jobs = _launchctl_jobs()
                if jobs and jobs.get(LABEL, "-").isdigit():
                    code, out = 0, ""  # running; whatever launchctl said, it worked
                    break
        self._pending = (title, code, out)  # picked up by the next refresh tick

    def _job_command(self, title: str, args: list[str], expect_pid: bool = False):
        """Run a launchctl command off the UI thread.

        subprocess.run and the verification wait both block for tens of seconds
        against a throttled job, and on the main thread that freezes the menu
        bar itself -- the one part of this app whose entire job is to stay
        responsive.
        """
        def worker():
            code, out = _sh(*args, timeout=JOB_CONTROL_TIMEOUT)
            self._after(title, code, out, expect_pid=expect_pid)

        threading.Thread(target=worker, daemon=True).start()

    def restart(self, _):
        # kickstart -k only works on a loaded job; bootstrap it first if needed.
        if not _is_loaded():
            return self.start(None)
        self._job_command(
            "Restart failed",
            ["launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}"],
            expect_pid=True,
        )

    def stop(self, _):
        self._job_command("Stop failed", ["launchctl", "bootout", f"{DOMAIN}/{LABEL}"])

    def start(self, _):
        if not PLIST.exists():
            return self._note("No agent installed", "Run ./launchd/install.sh")
        self._job_command(
            "Start failed",
            ["launchctl", "bootstrap", DOMAIN, str(PLIST)],
            expect_pid=True,
        )

    def _ensure_server(self):
        """Start the local map server, once, and keep it.

        Started when the indicator starts rather than on first use. Lazy was the
        original design and it produced a quiet trap: a map tab left open from
        yesterday keeps showing yesterday, because there is nothing on the port
        to poll until someone happens to click Open map. A loopback socket that
        is always there costs nothing and removes the whole question.

        Set map_port = 0 in config.toml to turn serving off entirely.
        """
        if getattr(self, "_httpd", None) is not None:
            return self._httpd
        if not self.cfg or not self.cfg.map_port:
            return None
        from . import serve

        self._httpd = serve.serve(self.cfg, self.cfg.map_port)
        return self._httpd

    def open_map(self, _):
        """Open the live map, falling back to a static file if serving fails."""
        if self.cfg is None:
            return self._note("Not set up yet")
        from . import export, serve

        try:
            webbrowser.open(serve.url_for(self._ensure_server()))
            return
        except OSError as exc:
            # Port already taken, most likely by an older copy of this app.
            self._note("Live map unavailable", f"{exc}. Opening a static copy.")
        except Exception as exc:  # noqa: BLE001
            self._note("Live map unavailable", str(exc)[:120])

        try:
            page, _data = export.render(self.cfg)
        except Exception as exc:  # noqa: BLE001
            return self._note("Could not build the map", str(exc))
        _sh("open", str(page))

    def open_guide(self, _):
        """The written walkthrough, which has room for the parts a dialog cannot."""
        webbrowser.open(SETUP_GUIDE_URL)

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
