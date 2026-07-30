"""MQTT subscriber for the CarData stream -> raw JSONL on disk and Postgres.

Raw JSONL is written first and is the source of truth: the feed is forward-only,
so anything not captured at the moment it arrives is gone for good.
"""

import fcntl
import json
import os
import queue
import ssl
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import db
from .auth import REFRESH_MARGIN, AuthRetryable, TokenStore, Tokens
from .config import Config

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
                print(f"[db] queue full, {self.dropped} message(s) not stored "
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
                    print("[db] reconnected")
                    self.degraded = False
            except Exception as exc:  # noqa: BLE001 - never let the DB break capture
                if not self.degraded:
                    print(f"[db] unavailable, raw capture continues: {exc}")
                    self.degraded = True
                self.conn = None
                time.sleep(1.0)  # do not spin against a down database
            finally:
                self.queue.task_done()

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=5)
        if self.conn is not None and not self.conn.closed:
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


def _backoff(failures: int) -> float:
    if failures >= EXTENDED_AFTER:
        return EXTENDED_BACKOFF
    return min(BASE_BACKOFF * (2 ** max(0, failures - 1)), MAX_BACKOFF)


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
                print(f"[auth] {exc}; retrying in {delay:.0f}s")
                time.sleep(delay)
                continue

            _assert_gcid(cfg, tokens)
            ok, reason = _session(cfg, tokens, sink, dbsink)
            if ok:
                failures = 0
                continue
            failures += 1
            delay = REASON_DELAY.get(reason, 0) or _backoff(failures)
            print(f"[mqtt] reconnecting in {delay:.0f}s (failure {failures})")
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sink.close()
        dbsink.close()


def _assert_gcid(cfg: Config, tokens: Tokens) -> None:
    if cfg.expected_gcid and cfg.expected_gcid != tokens.gcid:
        raise SystemExit(
            f"GCID mismatch: config says {cfg.expected_gcid}, "
            f"token says {tokens.gcid}. Check the portal value."
        )


def _session(
    cfg: Config, tokens: Tokens, sink: RawSink, dbsink: "DbSink"
) -> tuple[bool, int | None]:
    """One MQTT connection, torn down when the id_token nears expiry.

    Rebuilding on every refresh is deliberate: reusing a connection across a
    token change can leave it wedged in an unauthorised state after a network
    blip, even when BMW re-issues an identical token.

    Returns (clean, reason_code) so the caller can tell an orderly token cycle
    from a failure and back off accordingly.
    """
    down = threading.Event()
    connected = threading.Event()
    state = {"reason": None}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        connected.set()
        if reason_code != 0:
            print(f"[mqtt] connect refused: {reason_code}")
            state["reason"] = int(getattr(reason_code, "value", reason_code))
            down.set()
            return
        topics = _topics(cfg, tokens.gcid)
        for topic in topics:
            client.subscribe(topic, qos=1)
        print(f"[mqtt] connected, subscribing to {', '.join(topics)}")

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        # A refused subscription otherwise leaves us "connected" with no data
        # and no error. Treat any failure code (>= 0x80) as fatal for this
        # session so the supervisor rebuilds rather than sitting on a dead socket.
        failed = [rc for rc in reason_codes if int(rc.value) >= 0x80]
        if failed:
            print(f"[mqtt] subscription refused: {[str(r) for r in failed]}")
            state["reason"] = int(failed[0].value)
            down.set()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[mqtt] disconnected: {reason_code}")
        state["reason"] = int(getattr(reason_code, "value", reason_code) or 0)
        down.set()

    def on_message(client, userdata, msg):
        body = sink.write(msg.topic, msg.payload)  # durable first
        if isinstance(body, dict):
            dbsink.write(body)  # enqueue only; never block this thread
        print(f"{datetime.now().strftime('%H:%M:%S')} {_summarise(body)}")

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
    print(f"[mqtt] connecting; token good for {int(tokens.seconds_left())}s")

    # connect_async + loop_start rather than blocking connect(): a wedged TLS
    # handshake never fires a callback and a blocking connect can hang there
    # indefinitely, with no keep-alive to notice and nothing to time it out.
    client.loop_start()
    try:
        try:
            client.connect_async(HOST, PORT, keepalive=KEEPALIVE)
        except OSError as exc:
            print(f"[mqtt] connect failed: {exc}")
            return False, None

        if not connected.wait(timeout=CONNECT_TIMEOUT):
            print(f"[mqtt] no CONNACK within {CONNECT_TIMEOUT}s")
            return False, None
        if down.is_set():
            return False, state["reason"]

        # No stall watchdog here on purpose. The id_token expires hourly, so
        # this loop already tears the connection down and rebuilds it every ~55
        # minutes; a wedged subscription cannot outlive that. An additional
        # silence timer would either duplicate the token cycle or fire while the
        # car is legitimately parked and silent, which is most of the time.
        if down.wait(timeout=hold):
            return False, state["reason"]
        print("[mqtt] token refresh due; cycling connection")
        return True, None
    finally:
        client.disconnect()
        client.loop_stop()
