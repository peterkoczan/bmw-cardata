"""BMW's telematic data catalogue.

Public and unauthenticated -- no CarData credentials involved. It carries the
display name, unit, datatype, value range and category for every key, which
lets us type and label values from BMW's own metadata instead of guessing from
whatever happened to arrive first.
"""

import json
import re
from pathlib import Path

import requests

from . import db
from .config import Config

URL = "https://www.bmw.co.uk/en-gb/utilities/bmw/api/cd/catalogue"
PAGE = 10  # server-fixed; pageSize is ignored

# Values that mean "no reading", not a state.
NULLISH = {"INVALID", "UNKNOWN", "NOT_AVAILABLE", "ASN_ISUNKNOWN"}


def fetch() -> list[dict]:
    items, offset = [], 0
    while True:
        resp = requests.get(
            URL, params={"offset": offset, "streamable": "false"}, timeout=30
        )
        resp.raise_for_status()
        # Envelope is {status, success, data:{items, total, hasNextPage, ...}}.
        data = resp.json().get("data") or {}
        page = data.get("items") or []
        items.extend(e for e in page if isinstance(e, dict))
        if not page or not data.get("hasNextPage"):
            break
        offset += PAGE
    return items


UNIVERSAL = {"TRUE": True, "ASN_ISTRUE": True, "FALSE": False, "ASN_ISFALSE": False}
NEGATORS = ("NOT", "NON", "UN", "IN", "NO")
# Antonyms, where the false form is a different word rather than a negated one.
# Deliberately small and explicit: each pair is a claim about meaning, and a
# wrong one silently records the opposite of what the car reported.
ANTONYMS = {
    "OPEN": {"CLOSED", "SHUT"},
    "LOCKED": {"UNLOCKED"},
    "ACTIVE": {"INACTIVE"},
    "CONNECTED": {"DISCONNECTED"},
    "ON": {"OFF"},
}


def _polarity(token: str, predicate: str) -> bool | None:
    """Is `token` the affirmative form of `predicate`?

    Position in BMW's range string is NOT a reliable signal -- `isOpen` ships as
    both "CLOSED, OPEN, INVALID" and "OPEN, CLOSED, INVALID, UNKNOWN", and
    `isRemoteEngineStartAllowed` as "true, false, INVALID". Ordering-based
    derivation inverts those silently, which is worse than not mapping them. So
    read meaning instead: the key names its own predicate (isOpen -> OPEN), and
    a token carrying that word is true unless it is negated (FLAP_UNLOCKED,
    NOT_ACTIVE, NOTCHOSEN).
    """
    if token in UNIVERSAL:
        return UNIVERSAL[token]
    if not predicate:
        return None
    if any(token.endswith(a) for a in ANTONYMS.get(predicate, ())):
        return False
    idx = token.find(predicate)
    if idx < 0:
        return None
    prefix = token[:idx].rstrip("_")
    return not any(prefix.endswith(n) for n in NEGATORS)


def boolean_vocabulary(entry: dict) -> dict[str, bool | None]:
    """Derive a value->bool map from the catalogue's `range` and the key name.

    Returns {} unless exactly one true and one false token can be identified --
    an unmapped value stays queryable as text, whereas a wrong one is a lie.
    """
    if (entry.get("datatype") or "").lower() != "boolean":
        return {}
    tokens = [
        t.strip().upper()
        for t in re.split(r"[,/\n]", entry.get("range") or "")
        if t.strip()
    ]
    if not tokens:
        return {}

    # `vehicle.body.trunk.isOpen` -> OPEN; `...isHospitalityActive` -> ACTIVE.
    leaf = (entry.get("id") or "").split(".")[-1]
    match = re.match(r"is([A-Z][a-z]*)", leaf[0].upper() + leaf[1:] if leaf else "")
    words = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])", leaf[2:]) if leaf.startswith("is") else []
    predicate = (words[-1] if words else (match.group(1) if match else "")).upper()

    vocab: dict[str, bool | None] = {}
    for token in tokens:
        if token in NULLISH:
            vocab[token] = None
            continue
        polarity = _polarity(token, predicate)
        if polarity is not None:
            vocab[token] = polarity

    decided = [v for v in vocab.values() if v is not None]
    if decided.count(True) != 1 or decided.count(False) != 1:
        return {}
    return vocab


def is_numeric(entry: dict) -> bool:
    return (entry.get("datatype") or "").lower() in {
        "float", "double", "integer", "int", "uint8", "int32", "uint32", "number"
    }


def cache_path(cfg: Config) -> Path:
    return cfg.data_dir / "catalogue.json"


def save(cfg: Config, items: list[dict]) -> Path:
    path = cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2))
    return path


def load(cfg: Config) -> dict[str, dict]:
    path = cache_path(cfg)
    if not path.exists():
        return {}
    return {e["id"]: e for e in json.loads(path.read_text()) if e.get("id")}


def load_into_db(cfg: Config, items: list[dict]) -> int:
    rows = [
        (
            e.get("id"),
            e.get("name"),
            e.get("description"),
            (e.get("unit") or "").strip() or None,
            e.get("datatype"),
            e.get("range"),
            e.get("category"),
            bool(e.get("streamable")),
            e.get("vehicletypes") or [],
        )
        for e in items
        if e.get("id")
    ]
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO catalogue
                (key, name, description, unit, datatype, value_range,
                 category, streamable, vehicle_types)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (key) DO UPDATE SET
                name=EXCLUDED.name, description=EXCLUDED.description,
                unit=EXCLUDED.unit, datatype=EXCLUDED.datatype,
                value_range=EXCLUDED.value_range, category=EXCLUDED.category,
                streamable=EXCLUDED.streamable, vehicle_types=EXCLUDED.vehicle_types
            """,
            rows,
        )
    return len(rows)
