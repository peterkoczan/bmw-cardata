"""Postgres sink: raw CarData messages -> typed rows."""

import json
from pathlib import Path

import psycopg

from .config import ROOT, Config


def connect(cfg: Config):
    return psycopg.connect(cfg.dsn, autocommit=True)


def init(cfg: Config) -> None:
    with connect(cfg) as conn:
        conn.execute((ROOT / "schema.sql").read_text())


# ASN_isUnknown is deliberately absent: it stays text-only.
ASN = {"asn_istrue": True, "asn_isfalse": False}


def _row(payload: dict):
    """Flatten one message into a telemetry row, or None if unusable.

    BMW sends exactly one key per message, but don't assume it -- yield each
    entry in `data` so a multi-key message would still land intact.
    """
    vin = payload.get("vin")
    msg_ts = payload.get("timestamp")
    data = payload.get("data")
    if not (vin and msg_ts and isinstance(data, dict)):
        return
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        num = bool_ = txt = None
        # bool before int: bool is a subclass of int in Python, so the naive
        # order silently files every True/False as 1/0 in the numeric column.
        if isinstance(value, bool):
            bool_ = value
        elif isinstance(value, (int, float)):
            num = float(value)
        elif value is not None:
            txt = str(value)
            # Several keys BMW documents as boolean actually arrive as the ASN
            # enum -- ASN_isTrue / ASN_isFalse / ASN_isUnknown. Normalise the
            # two decidable cases so `bool` is queryable, but keep the raw text
            # as well: "unknown" is a real third state and mapping it to false
            # would invent a fact the car never reported.
            asn = ASN.get(txt.strip().lower())
            if asn is not None:
                bool_ = asn
        yield (
            entry.get("timestamp") or msg_ts,
            msg_ts,
            vin,
            key,
            num,
            bool_,
            txt,
            entry.get("unit"),
        )


INSERT = """
INSERT INTO telemetry (ts, msg_ts, vin, key, num, bool, txt, unit)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (vin, key, ts) DO NOTHING
"""


def write(conn, payload: dict) -> int:
    """Insert a message's rows. Returns rows actually stored, not attempted --
    BMW re-sends identical readings, so the difference is the duplicate count."""
    rows = list(_row(payload))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(INSERT, rows)
        return cur.rowcount


def load_jsonl(cfg: Config, paths: list[Path]) -> tuple[int, int]:
    """Backfill from the raw sink. Idempotent -- safe to re-run over old files."""
    seen = written = 0
    with connect(cfg) as conn:
        for path in paths:
            for line in path.open():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    seen += 1
                    written += write(conn, payload)
    return seen, written


def prune(cfg: Config) -> int:
    """Drop rows past the retention window. Configurable via retention_days."""
    with connect(cfg) as conn:
        cur = conn.execute(
            "DELETE FROM telemetry WHERE ts < now() - make_interval(days => %s)",
            (cfg.retention_days,),
        )
        return cur.rowcount
