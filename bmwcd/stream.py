"""MQTT subscriber for the CarData stream -> raw JSONL on disk.

Milestone 1: prove the pipe works and lose nothing. Structure comes later --
persist raw first, transform second.
"""

import json
import ssl
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import db
from .auth import REFRESH_MARGIN, TokenStore, Tokens
from .config import Config

HOST = "customer.streaming-cardata.bmwgroup.com"
PORT = 9000
KEEPALIVE = 30  # BMW requires <= 30s


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
    """Best-effort Postgres writer.

    The raw JSONL is the source of truth, so a database that is down, slow or
    mid-restart must never cost us a message or stall the MQTT loop. Failures
    are logged once per outage and the connection is reopened on the next
    message; the gap is repaired later with `bmwcd load`.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = None
        self.degraded = False

    def write(self, payload: dict) -> None:
        try:
            if self.conn is None or self.conn.closed:
                self.conn = db.connect(self.cfg)
            db.write(self.conn, payload)
            if self.degraded:
                print("[db] reconnected")
                self.degraded = False
        except Exception as exc:  # noqa: BLE001 - never let the DB break capture
            if not self.degraded:
                print(f"[db] unavailable, raw capture continues: {exc}")
                self.degraded = True
            self.conn = None

    def close(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.close()


def run(cfg: Config, store: TokenStore) -> None:
    sink = RawSink(cfg.data_dir)
    dbsink = DbSink(cfg)
    print(f"Raw sink: {sink.dir}")
    print(f"Database: {cfg.dsn}")
    try:
        while True:
            tokens = store.fresh()
            _assert_gcid(cfg, tokens)
            _session(cfg, tokens, sink, dbsink)
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


def _session(cfg: Config, tokens: Tokens, sink: RawSink, dbsink: "DbSink") -> None:
    """One MQTT connection, torn down when the id_token nears expiry.

    Rebuilding on every refresh is deliberate: reusing a connection across a
    token change can leave it wedged in an unauthorised state after a network
    blip, even when BMW re-issues an identical token.
    """
    down = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            print(f"[mqtt] connect refused: {reason_code}")
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
        failed = [str(rc) for rc in reason_codes if int(rc.value) >= 0x80]
        if failed:
            print(f"[mqtt] subscription refused: {failed}")
            down.set()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[mqtt] disconnected: {reason_code}")
        down.set()

    def on_message(client, userdata, msg):
        body = sink.write(msg.topic, msg.payload)  # durable first
        if isinstance(body, dict):
            dbsink.write(body)
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

    try:
        client.connect(HOST, PORT, keepalive=KEEPALIVE)
    except OSError as exc:
        print(f"[mqtt] connect failed: {exc}; retrying in 30s")
        time.sleep(30)
        return

    client.loop_start()
    try:
        if down.wait(timeout=hold):
            time.sleep(10)  # unexpected drop -- breathe before reconnecting
        else:
            print("[mqtt] token refresh due; cycling connection")
    finally:
        client.disconnect()
        client.loop_stop()
