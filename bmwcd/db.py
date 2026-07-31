"""Postgres sink: raw CarData messages -> typed rows."""

import json
import re
from pathlib import Path

import psycopg

from .config import ROOT, Config


def connect(cfg: Config, connect_timeout: int = 10, statement_timeout_ms: int = 0):
    """Open a connection.

    libpq's default connect timeout is infinite and the server's
    statement_timeout is 0, so a wedged Postgres blocks the caller forever. That
    is merely annoying in a CLI, but fatal in the menu bar app, where it runs on
    the UI thread and freezes the very indicator you opened to diagnose it.
    Callers that must stay responsive pass short timeouts; bulk paths do not.
    """
    options = f"-c statement_timeout={statement_timeout_ms}" if statement_timeout_ms else ""
    return psycopg.connect(
        cfg.dsn,
        autocommit=True,
        connect_timeout=connect_timeout,
        **({"options": options} if options else {}),
    )


def init(cfg: Config) -> None:
    with connect(cfg) as conn:
        conn.execute((ROOT / "schema.sql").read_text())


# Fallback for when the catalogue has not been fetched yet. ASN_isUnknown is
# deliberately absent: it stays text-only rather than becoming a false.
ASN = {"ASN_ISTRUE": True, "ASN_ISFALSE": False}

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_VOCAB_CACHE: dict[str, dict] = {}


def catalogue_is_textual(spec: dict) -> bool:
    """True only when BMW positively says this key is not a number."""
    return (spec.get("datatype") or "").lower() in {"string", "boolean", "enum"}


def _bool_vocab(key: str, spec: dict) -> dict:
    if key not in _VOCAB_CACHE:
        from . import catalogue as cat

        _VOCAB_CACHE[key] = (cat.boolean_vocabulary(spec) if spec else {}) or ASN
    return _VOCAB_CACHE[key]


def _row(payload: dict, catalogue: dict | None = None):
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
        spec = (catalogue or {}).get(key, {})
        num = bool_ = txt = None
        # bool before int: bool is a subclass of int in Python, so the naive
        # order silently files every True/False as 1/0 in the numeric column.
        if isinstance(value, bool):
            bool_ = value
        elif isinstance(value, (int, float)):
            num = float(value)
        elif value is not None:
            txt = str(value)
            token = txt.strip().upper()

            # Numbers sometimes arrive quoted -- batterySizeMax came through as
            # the string "0.0" with unit kWh. Typing purely on the Python type
            # buries those in `txt` where every numeric query misses them, so
            # populate `num` as well when the value really is a number.
            # Gate on "not known to be non-numeric" rather than on an explicit
            # allow-list: the catalogue has uint16/int16/int8 and 22 blank
            # datatypes, and an allow-list left those quoted numerics stranded
            # in txt with num NULL -- invisible to every numeric query. Unknown
            # should default permissive, which is also how the no-catalogue path
            # already behaved.
            if _NUMERIC.match(token) and not catalogue_is_textual(spec):
                try:
                    num = float(token)
                except ValueError:
                    pass

            # Many keys BMW documents as boolean ship bespoke vocabularies:
            # OPEN/CLOSED, FLAP_UNLOCKED/FLAP_LOCKED, NOTCHOSEN/CHOSEN. Derive
            # the mapping from the catalogue's own range rather than curating a
            # table of BMW's synonyms for yes. Raw text is always kept: INVALID
            # and UNKNOWN are real third states, not false.
            vocab = _bool_vocab(key, spec)
            if token in vocab and vocab[token] is not None:
                bool_ = vocab[token]
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


def write(conn, payload: dict, catalogue: dict | None = None) -> int:
    """Insert a message's rows. Returns rows actually stored, not attempted --
    BMW re-sends identical readings, so the difference is the duplicate count."""
    rows = list(_row(payload, catalogue))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(INSERT, rows)
        return cur.rowcount


def load_jsonl(cfg: Config, paths: list[Path]) -> tuple[int, int]:
    """Backfill from the raw sink. Idempotent -- safe to re-run over old files."""
    from . import catalogue as cat

    spec = cat.load(cfg)
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
                    written += write(conn, payload, spec)
    return seen, written


def vins(cfg: Config) -> list[str]:
    """Every VIN that has ever sent something, newest talker first.

    Read from the data rather than from config.vins: the subscription is a
    wildcard over the whole account, so a car added in the BMW portal starts
    streaming without anyone editing config, and this is what makes it show up.
    """
    with connect(cfg) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT vin FROM telemetry GROUP BY vin ORDER BY max(ts) DESC"
            ).fetchall()
        ]


def prune(cfg: Config) -> int:
    """Drop rows past the retention window. Configurable via retention_days."""
    with connect(cfg) as conn:
        cur = conn.execute(
            "DELETE FROM telemetry WHERE ts < now() - make_interval(days => %s)",
            (cfg.retention_days,),
        )
        return cur.rowcount
