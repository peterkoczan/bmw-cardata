"""MQTT subscriber for the CarData stream -> raw JSONL on disk and Postgres.

Raw JSONL is written first and is the source of truth: the feed is forward-only,
so anything not captured at the moment it arrives is gone for good.
"""

import fcntl
import json
import os
import queue
import signal
import socket
import ssl
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import db, logs
from .auth import REFRESH_MARGIN, AuthRetryable, TokenStore, Tokens
from .config import Config

def _log(message: str) -> None:
    """Every line timestamped.

    Reconstructing a sleep/wake incident from bare `[mqtt] reconnecting in 300s`
    lines meant correlating against `pmset -g log` by hand. Cheap to fix.
    """
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


HOST = "customer.streaming-cardata.bmwgroup.com"
PORT = 9000
KEEPALIVE = 30  # BMW requires <= 30s
CONNECT_TIMEOUT = 20  # no CONNACK by now means a wedged handshake


class RawSink:
    """Append-only daily JSONL. Flushed per message; nothing buffered on exit."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir / "raw"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._day: date | None = None
        self._fh = None

    def _roll(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today == self._day and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        self._fh = (self.dir / f"cardata-{today.isoformat()}.jsonl").open("a")
        self._day = today

    def write(self, topic: str, payload: bytes) -> dict | str:
        self._roll()
        try:
            body = json.loads(payload)
        except ValueError:
            body = payload.decode("utf-8", "replace")
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "topic": topic,
            "payload": body,
        }
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()
        return body

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _topics(cfg: Config, gcid: str) -> list[str]:
    """A single wildcard subscription, `{gcid}/+`.

    The id_token's dynamic_scopes claim carries the broker ACL as a regex --
    `read:streaming/*/{gcid}.*` -- so a topic must begin with the GCID. The
    portal's connection panel shows the bare VIN as "Thema", but that is only
    the topic's second component; subscribing to it returns 0x87 Not authorized
    and BMW then drops the whole connection rather than refusing that one
    subscription. One bad topic therefore takes the good ones with it.

    `{gcid}/+` covers every VIN in one subscription that cannot be partially
    refused, so adding a car in the portal needs no change here and a VIN with
    no stream configured cannot poison the connection. The per-message topic is
    recorded anyway, so routing by VIN is unaffected.
    """
    return [f"{gcid}/+"]


def _summarise(body) -> str:
    """One terse line per message so a live tail is actually readable."""
    if not isinstance(body, dict):
        return str(body)[:160]
    data = body.get("data")
    if isinstance(data, dict):
        return " ".join(
            f"{k}={v.get('value') if isinstance(v, dict) else v}" for k, v in data.items()
        )[:200]
    return json.dumps(body, separators=(",", ":"))[:200]


class DbSink:
    """Best-effort Postgres writer, drained on its own thread.

    The raw JSONL is the source of truth, so a database that is down, slow or
    mid-restart must never cost us a message. It must also never block: writing
    inline on paho's network thread means a stalled Postgres delays PINGREQ past
    the 30s keep-alive and BMW drops the connection -- a database problem
    becoming a data-loss problem.

    So `write` only enqueues. The queue is bounded: an unbounded one just trades
    a stall for unbounded memory growth. On overflow we drop and count, because
    the JSONL still has everything and `bmwcd load` repairs the gap.
    """

    def __init__(self, cfg: Config, maxsize: int = 10_000):
        from . import catalogue as cat

        self.cfg = cfg
        self.catalogue = cat.load(cfg)  # empty until `bmwcd catalogue` is run
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.conn = None
        self.degraded = False
        self.dropped = 0
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

    def write(self, payload: dict) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                _log(f"[db] queue full, {self.dropped} message(s) not stored "
                      f"(raw JSONL intact; repair with: bmwcd load)")

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if self.conn is None or self.conn.closed:
                    self.conn = db.connect(self.cfg)
                db.write(self.conn, payload, self.catalogue)
                if self.degraded:
                    _log("[db] reconnected")
                    self.degraded = False
            except Exception as exc:  # noqa: BLE001 - never let the DB break capture
                if not self.degraded:
                    _log(f"[db] unavailable, raw capture continues: {exc}")
                    self.degraded = True
                self.conn = None
                time.sleep(1.0)  # do not spin against a down database
            finally:
                self.queue.task_done()

    def close(self, drain_seconds: float = 10.0) -> None:
        """Drain what is queued before stopping, then report anything left.

        Setting the stop flag first made the worker abandon the queue instantly
        and silently -- measured at 1999 of 2000 rows discarded with `dropped`
        still reading 0. The raw JSONL still holds them, but nothing surfaced
        the gap, so it stayed invisible until someone noticed missing rows.
        """
        deadline = time.monotonic() + drain_seconds
        while not self.queue.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        left = self.queue.qsize()
        self._stop.set()
        self._worker.join(timeout=5)
        if left:
            _log(f"[db] {left} message(s) still queued at shutdown; "
                  f"raw JSONL is intact — repair with: bmwcd load")
        # Connection ownership stays with the worker; only close it here if the
        # worker is genuinely gone, otherwise we yank it mid-insert.
        if not self._worker.is_alive() and self.conn is not None and not self.conn.closed:
            self.conn.close()


# MQTT v5 reason codes worth pausing longer for. Reconnecting fast against
# "quota exceeded" is how you stay quota-exceeded.
REASON_DELAY = {
    151: 60,  # Quota exceeded
    135: 30,  # Not authorized
    128: 20,  # Unspecified error
    133: 20,  # Server busy
}
BASE_BACKOFF = 5
MAX_BACKOFF = 300
EXTENDED_AFTER = 10  # consecutive failures before backing off much harder
EXTENDED_BACKOFF = 600


OFFLINE_RETRY = 15.0
PRODUCTIVE_AFTER = 60.0  # a session this long did real work, whatever ended it


def _have_network() -> bool:
    """Can we even resolve BMW's broker?

    Deliberately DNS-only: it touches nothing of BMW's, and it cleanly separates
    "this laptop has no network" from "BMW is refusing us". A closed lid produces
    a run of failed connects that never left the machine, and letting those climb
    the backoff ladder means waiting minutes after the network is genuinely back.
    """
    try:
        socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def _backoff(failures: int) -> float:
    if failures >= EXTENDED_AFTER:
        return EXTENDED_BACKOFF
    return min(BASE_BACKOFF * (2 ** max(0, failures - 1)), MAX_BACKOFF)


STATUS_FILE = "stream.status"


def _status(cfg: Config, state: str, detail: str = "") -> None:
    """Heartbeat for the menu bar indicator.

    A live PID proves only that Python is running. The failure modes that
    actually happen -- backoff after repeated refusals, an auth retry loop, DNS
    failure -- all leave the process alive and the stream down, and without this
    the indicator would show "streaming" throughout, then fade to "quiet" after
    six hours, which is exactly how a parked car looks. Then a broken stream and
    a parked car are indistinguishable, and that is the one distinction the
    indicator exists to make.
    """
    try:
        payload = {"state": state, "detail": detail, "at": time.time()}
        path = cfg.data_dir / STATUS_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError:
        pass  # never let status reporting break capture


def _wait_for_new_tokens(limit: float = 3600.0, tick: float = 15.0) -> bool:
    """Block until tokens.json changes, or `limit` elapses.

    Cheap local stat rather than retrying against BMW, so a dead credential
    costs at most one request an hour instead of one every thirty seconds.
    """
    from .config import TOKEN_PATH

    def stamp():
        try:
            return TOKEN_PATH.stat().st_mtime
        except OSError:
            return None

    start, before = time.monotonic(), stamp()
    while time.monotonic() - start < limit:
        time.sleep(tick)
        if stamp() != before:
            _log("[auth] credentials changed; retrying")
            return True
    return False


def _single_instance(cfg: Config):
    """Refuse to start if another streamer already holds the lock.

    BMW allows one connection per GCID, so a second process does not merely
    duplicate work -- the two evict each other in a loop and neither stays
    connected. Easy to trigger by running `bmwcd stream` by hand while the
    launchd agent is up.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(cfg.data_dir / "stream.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            "Another bmwcd stream is already running (data/stream.lock).\n"
            "Check: launchctl list | grep bmw-cardata"
        )
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle  # keep referenced; closing releases the lock


def run(cfg: Config, store: TokenStore) -> None:
    lock = _single_instance(cfg)  # noqa: F841 - held for the process lifetime

    # launchd stops us with SIGTERM (bootout, kickstart -k, the menu bar's Stop
    # and Restart). Python's default handler exits without unwinding, so the
    # finally block below -- which drains the DB queue and flushes the sink --
    # never ran in production. Raise instead, so shutdown is orderly.
    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    sink = RawSink(cfg.data_dir)
    dbsink = DbSink(cfg)
    print(f"Raw sink: {sink.dir}")
    print(f"Database: {cfg.dsn}")
    failures = 0
    try:
        while True:
            try:
                tokens = store.fresh()
            except AuthRetryable as exc:
                # Transient: a blip at refresh time must not kill the process
                # and hand launchd a crash loop against BMW's auth endpoint.
                failures += 1
                delay = _backoff(failures)
                _log(f"[auth] {exc}; retrying in {delay:.0f}s")
                _status(cfg, "auth_retry", f"{exc} (retry in {delay:.0f}s)")
                time.sleep(delay)
                continue

            except SystemExit as exc:
                # An expired refresh token is fatal to *this* attempt but must
                # not exit: KeepAlive would respawn us every 30s forever,
                # hammering BMW's token endpoint ~2,880 times a day with a dead
                # credential while launchctl still showed a healthy PID.
                #
                # Park instead, and watch tokens.json -- re-authorising from the
                # menu bar rewrites it, so the stream heals by itself within
                # seconds instead of needing a manual restart.
                _log(f"[auth] {exc}")
                _status(cfg, "needs_auth", str(exc).splitlines()[0][:120])
                if not _wait_for_new_tokens():
                    _log("[auth] still no new credentials; retrying anyway")
                continue

            _assert_gcid(cfg, tokens)
            ok, reason, productive = _session(cfg, tokens, sink, dbsink)
            if ok:
                failures = 0
                continue

            if not _have_network():
                # Nothing reached BMW, so this is not evidence of anything being
                # wrong with us or them. Retry steadily without burning the
                # ladder, so the moment the lid opens we reconnect promptly.
                _log(f"[mqtt] no network; retrying in {OFFLINE_RETRY:.0f}s")
                _status(cfg, "offline", "waiting for network")
                time.sleep(OFFLINE_RETRY)
                continue

            if productive:
                # It connected, subscribed and ran for a while. Whatever ended it
                # is a fresh incident, not the seventh step of an escalation --
                # otherwise an hourly drop eventually pins the retry at 10 min.
                failures = 0
            failures += 1
            # max, not `or`: `or` used the mapped constant *instead of* the
            # backoff, so a persistent 135 or 151 retried at a flat 30s/60s
            # forever -- 2,880 connects a day against a credential that will
            # never be accepted, and EXTENDED_BACKOFF unreachable for exactly
            # the four codes it exists for. The constant is a floor, not a cap.
            delay = max(REASON_DELAY.get(reason, 0), _backoff(failures))
            _log(f"[mqtt] reconnecting in {delay:.0f}s (failure {failures})")
            _status(cfg, "reconnecting", f"failure {failures}, retry in {delay:.0f}s")
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        _status(cfg, "stopped")
        sink.close()
        dbsink.close()


def _assert_gcid(cfg: Config, tokens: Tokens) -> None:
    if cfg.expected_gcid and cfg.expected_gcid != tokens.gcid:
        raise SystemExit(
            f"GCID mismatch: config says {cfg.expected_gcid}, "
            f"token says {tokens.gcid}. Check the portal value."
        )


def _productive(state) -> bool:
    """Did this session subscribe and then survive long enough to count?"""
    at = state.get("subscribed_at")
    return at is not None and (time.monotonic() - at) >= PRODUCTIVE_AFTER


def _session(
    cfg: Config, tokens: Tokens, sink: RawSink, dbsink: "DbSink"
) -> tuple[bool, int | None, bool]:
    """One MQTT connection, torn down when the id_token nears expiry.

    Rebuilding on every refresh is deliberate: reusing a connection across a
    token change can leave it wedged in an unauthorised state after a network
    blip, even when BMW re-issues an identical token.

    Returns (clean, reason_code) so the caller can tell an orderly token cycle
    from a failure and back off accordingly.
    """
    down = threading.Event()
    connected = threading.Event()
    state = {"reason": None, "subscribed_at": None}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        connected.set()
        if reason_code != 0:
            _log(f"[mqtt] connect refused: {reason_code}")
            state["reason"] = int(getattr(reason_code, "value", reason_code))
            down.set()
            return
        topics = _topics(cfg, tokens.gcid)
        for topic in topics:
            client.subscribe(topic, qos=1)
        _log(f"[mqtt] connected, subscribing to {', '.join(topics)}")
        _status(cfg, "connected")

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        # A refused subscription otherwise leaves us "connected" with no data
        # and no error. Treat any failure code (>= 0x80) as fatal for this
        # session so the supervisor rebuilds rather than sitting on a dead socket.
        if not any(int(rc.value) >= 0x80 for rc in reason_codes):
            state["subscribed_at"] = time.monotonic()
        failed = [rc for rc in reason_codes if int(rc.value) >= 0x80]
        if failed:
            _log(f"[mqtt] subscription refused: {[str(r) for r in failed]}")
            state["reason"] = int(failed[0].value)
            down.set()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        _log(f"[mqtt] disconnected: {reason_code}")
        state["reason"] = int(getattr(reason_code, "value", reason_code) or 0)
        down.set()

    def on_message(client, userdata, msg):
        # Guarded because paho re-raises callback exceptions and nothing between
        # here and its thread main catches them: one raising message kills the
        # network thread, on_disconnect never fires, and the session then waits
        # out the whole token window and reports it as a CLEAN cycle -- which
        # also resets the supervisor's failure counter. Fail loudly instead.
        try:
            body = sink.write(msg.topic, msg.payload)  # durable first
            if isinstance(body, dict):
                dbsink.write(body)  # enqueue only; never block this thread
            _log(_summarise(body))
        except Exception as exc:  # noqa: BLE001
            _log(f"[mqtt] message handling failed: {exc!r}")
            state["reason"] = None
            down.set()

    # Client id = GCID: BMW allows one connection per GCID, so reusing the same
    # identifier makes a reconnect deterministically evict our own stale session
    # instead of racing it.
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2, client_id=tokens.gcid, protocol=mqtt.MQTTv5
    )
    client.username_pw_set(tokens.gcid, tokens.id_token)
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3  # BMW no longer accepts 1.2
    client.tls_set_context(ctx)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    hold = max(60.0, tokens.seconds_left() - REFRESH_MARGIN)
    _log(f"[mqtt] connecting; token good for {int(tokens.seconds_left())}s")

    # connect_async + loop_start rather than blocking connect(): a wedged TLS
    # handshake never fires a callback and a blocking connect can hang there
    # indefinitely, with no keep-alive to notice and nothing to time it out.
    client.loop_start()
    try:
        try:
            client.connect_async(HOST, PORT, keepalive=KEEPALIVE)
        except OSError as exc:
            _log(f"[mqtt] connect failed: {exc}")
            return False, None, _productive(state)

        if not connected.wait(timeout=CONNECT_TIMEOUT):
            _log(f"[mqtt] no CONNACK within {CONNECT_TIMEOUT}s")
            return False, None, _productive(state)
        if down.is_set():
            return False, state["reason"], _productive(state)

        # Two clocks, because neither alone is trustworthy here.
        #
        # macOS stops time.monotonic() while the machine is asleep, but the
        # id_token expires on wall-clock time -- so a monotonic-only wait returns
        # from a two-hour sleep believing it still has 50 minutes on a token that
        # died an hour ago. Conversely a backward NTP step makes wall clock alone
        # sit far past expiry. Take whichever deadline fires first.
        wall_deadline = time.time() + hold
        mono_deadline = time.monotonic() + hold
        while True:
            remaining = min(
                wall_deadline - time.time(), mono_deadline - time.monotonic()
            )
            if remaining <= 0:
                _log("[mqtt] token refresh due; cycling connection")
                return True, None, True

            wall_before, mono_before = time.time(), time.monotonic()
            woke = down.wait(timeout=min(30.0, remaining))

            # Measure the gap BEFORE interpreting `woke`. On resume the network
            # is gone, so paho usually fires on_disconnect first and wins the
            # race -- checking `woke` first would classify every single wake as a
            # connection failure, incrementing the backoff counter until a run of
            # lid-closes earns a 10-minute outage on a feed that cannot backfill.
            wall_elapsed = time.time() - wall_before
            mono_elapsed = time.monotonic() - mono_before
            # Wall advanced while monotonic did not: that is suspension, and
            # unlike a bare wall-clock threshold it cannot be faked by an NTP
            # step, which moves both clocks' *difference* not at all.
            if wall_elapsed - mono_elapsed > 60:
                _log(f"[mqtt] resumed after {wall_elapsed / 60:.0f}m suspended; cycling")
                return True, None, _productive(state)
            if woke:
                return False, state["reason"], _productive(state)
            _status(cfg, "connected")
            # Checked here rather than only in the nightly prune: this is the
            # always-on process, and a busy drive can add tens of megabytes
            # between two 04:00 runs. A stat every 30s costs nothing.
            for name in logs.rotate_all(cfg):
                _log(f"[logs] rotated {name}")
    finally:
        _status(cfg, "disconnected")
        client.disconnect()
        client.loop_stop()
