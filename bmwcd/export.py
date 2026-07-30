"""Build a self-contained map page from recorded telemetry.

Everything is embedded in the HTML rather than fetched, so the page works from
a file:// path -- a browser will not fetch() a sibling JSON file off disk.
"""

import bisect
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from . import catalogue as cat
from . import db
from .config import ROOT, Config

TEMPLATE = Path(__file__).parent / "viz" / "map.html"

DISTANCE = "vehicle.vehicle.travelledDistance"
FUEL_PCT = "vehicle.drivetrain.fuelSystem.level"
FUEL_LITRES = "vehicle.drivetrain.fuelSystem.remainingFuel"

# State of charge. `batteryManagement.header` is the streamed actual value;
# `electricEngine.charging.level` looks right but is the *predicted* SoC and
# does not appear in the catalogue at all.
SOC_CANDIDATES = (
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
)
BATTERY_KWH = (
    "vehicle.drivetrain.batteryManagement.maxEnergy",
    "vehicle.drivetrain.batteryManagement.batterySizeMax",
)

# BMW's catalogue has the human-readable names of these two crossed over
# relative to the key names, so trust neither label and consult both: if either
# reports the engine running, treat it as running.
ENGINE_KEYS = (
    "vehicle.drivetrain.engine.isIgnitionOn",
    "vehicle.drivetrain.engine.isActive",
)
HV_STATUS = "vehicle.drivetrain.electricEngine.charging.hvStatus"

# Route stitching. Position updates arrive every ~3 min or 2 km on Live Cockpit
# Professional, and not at all while parked, so consecutive fixes are not
# automatically one continuous drive.
GAP_SPLIT_SECONDS = 600   # silence this long ends a trip
MIN_STEP_M = 10           # below this the car has not meaningfully moved
MAX_STEP_M = 2000         # a jump this big is a tunnel catch-up, not a drive

# Odometer sanity. BMW has been known to report a km value labelled as miles.
MAX_ODOMETER_JUMP_KM = 2000
ODOMETER_DECREASE_TOLERANCE_KM = -1.0

# Rough envelopes for normalising a segment's intensity to 0..1 for shading.
ELECTRIC_KWH_PER_100KM = (12.0, 40.0)
FUEL_L_PER_100KM = (4.0, 16.0)
FUEL_PCT_PER_100KM = (2.0, 12.0)

# BMW's own categories, in its own rank order. Better than a taxonomy derived
# from key prefixes, which splits related readings across made-up buckets.
CATEGORY_LABELS = {
    "BASIC_DATA": "Basic data",
    "VEHICLE_STATUS": "Vehicle status",
    "USAGE_BASED": "Usage",
    "EVENTS": "Events",
    "BEV_PHEV_DATA": "Battery & charging",
    "META_DATA": "Metadata",
    "TYRE_DATA": "Tyres",
    "CD_CONTRACT": "Contract",
}


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Series:
    """Ascending (ts, value) pairs with an O(log n) as-of lookup.

    The naive linear scan is O(n) per probe and `_mode` probes eight series per
    fix, so a month of data turns into a visible stall.
    """

    def __init__(self, rows):
        self.times = [r[0] for r in rows]
        self.values = [r[1] for r in rows]

    def __bool__(self):
        return bool(self.times)

    def at(self, when):
        idx = bisect.bisect_right(self.times, when) - 1
        return self.values[idx] if idx >= 0 else None


def _series(conn, vin: str, key: str, column: str = "num") -> Series:
    return Series(
        conn.execute(
            f"SELECT ts, {column} FROM telemetry"
            f" WHERE vin=%s AND key=%s AND {column} IS NOT NULL ORDER BY ts",
            (vin, key),
        ).fetchall()
    )


def _scale(value, lo, hi):
    return max(0.0, min(1.0, (value - lo) / (hi - lo))) if hi > lo else 0.5


def _odometer_delta(dist: Series, prev_t, t):
    """Distance covered, or None if the odometer reading is not believable."""
    d0, d1 = dist.at(prev_t), dist.at(t)
    if d0 is None or d1 is None:
        return None
    km = d1 - d0
    if km < ODOMETER_DECREASE_TOLERANCE_KM or km > MAX_ODOMETER_JUMP_KM:
        return None
    return km if km > 0 else 0.0


def _mode(prev_t, t, s, fallback_km):
    """Classify the drive between two fixes.

    CarData exposes no instantaneous power or fuel-flow signal at all -- every
    consumption key is a lifetime total, a per-trip accumulator or a running
    average. So mode is derived: engine state splits petrol from electric, and
    the shade comes from differencing state of charge or fuel level over the
    distance covered between the two fixes.
    """
    km = _odometer_delta(s["dist"], prev_t, t)
    if km is None:
        km = fallback_km  # odometer unusable; fall back to GPS distance
    if not km or km <= 0:
        return {"mode": "idle", "intensity": 0.0, "km": km or 0.0}

    soc0, soc1 = s["soc"].at(prev_t), s["soc"].at(t)
    d_soc = (soc1 - soc0) if (soc0 is not None and soc1 is not None) else None
    battery = s["battery_kwh"]

    engine_on = any(series.at(t) for series in s["engine"] if series)
    hv = (s["hv"].at(t) or "") if s["hv"] else ""
    plugged = hv.upper() in {"CHARGING", "WAITING_FOR_CHARGING"}

    out = {"km": round(km, 3)}

    # BMW reports integer SoC, so treat anything inside half a point as noise
    # rather than as a real change.
    if d_soc is not None and d_soc > 0.5 and not plugged:
        kwh = (d_soc / 100.0) * battery if battery else 0.0
        return out | {
            "mode": "regen",
            "intensity": _scale(kwh / km * 100, *ELECTRIC_KWH_PER_100KM),
            "kwh": round(kwh, 3),
        }

    if engine_on:
        l0, l1 = s["fuel_l"].at(prev_t), s["fuel_l"].at(t)
        d_litres = (l0 - l1) if (l0 is not None and l1 is not None) else None
        if d_litres and d_litres > 0:
            per100 = d_litres / km * 100
            return out | {
                "mode": "petrol",
                "intensity": _scale(per100, *FUEL_L_PER_100KM),
                "l_per_100km": round(per100, 1),
            }
        p0, p1 = s["fuel_pct"].at(prev_t), s["fuel_pct"].at(t)
        d_pct = (p0 - p1) if (p0 is not None and p1 is not None) else None
        per100 = (d_pct / km * 100) if d_pct and d_pct > 0 else None
        return out | {
            "mode": "petrol",
            "intensity": _scale(per100, *FUEL_PCT_PER_100KM) if per100 else 0.5,
            **({"fuel_pct_per_100km": round(per100, 2)} if per100 else {}),
        }

    if d_soc is not None and d_soc < -0.5:
        kwh = (-d_soc / 100.0) * battery if battery else 0.0
        per100 = kwh / km * 100
        return out | {
            "mode": "electric",
            "intensity": _scale(per100, *ELECTRIC_KWH_PER_100KM),
            "kwh_per_100km": round(per100, 1),
        }

    return out | {"mode": "unknown", "intensity": 0.0}


def _state_series(conn, vin: str, since) -> dict:
    """Every recorded key as a sparse time series, for the state panel.

    Only value *changes* are emitted. Most keys are near-static -- doors, tyre
    targets, charging limits -- so storing every repeat would inflate the
    embedded payload by an order of magnitude for no visible difference.
    """
    where = "vin=%s" + (" AND ts >= %s" if since else "")
    args = (vin, since) if since else (vin,)
    rows = conn.execute(
        f"SELECT key, ts, num, bool, txt, unit FROM telemetry WHERE {where}"
        f" ORDER BY key, ts",
        args,
    ).fetchall()

    out: dict[str, dict] = {}
    last: dict[str, object] = {}
    for key, ts, num, bool_, txt, unit in rows:
        # Prefer the typed value; fall back to text for genuine enums.
        value = bool_ if bool_ is not None else (num if num is not None else txt)
        if value is None:
            continue
        if key in last and last[key] == value:
            continue
        last[key] = value
        entry = out.setdefault(key, {"u": unit, "v": []})
        entry["v"].append([int(ts.timestamp() * 1000), value])
    return out


def _trips(points) -> list[dict]:
    """Summarise each contiguous run of movement."""
    trips: dict[int, dict] = {}
    for p in points:
        if p.get("trip") is None:
            continue
        t = trips.setdefault(
            p["trip"],
            {"trip": p["trip"], "start": p["t"], "end": p["t"], "km": 0.0,
             "modes": {}, "kwh": 0.0, "litres": 0.0},
        )
        t["end"] = max(t["end"], p["t"])
        t["start"] = min(t["start"], p["t"])
        t["km"] += p.get("km") or 0.0
        mode = p.get("mode")
        if mode in {"electric", "petrol", "regen"}:
            t["modes"][mode] = t["modes"].get(mode, 0.0) + (p.get("km") or 0.0)
    out = []
    for t in sorted(trips.values(), key=lambda x: x["start"]):
        t["km"] = round(t["km"], 2)
        t["minutes"] = round((t["end"] - t["start"]) / 60000, 1)
        t["dominant"] = max(t["modes"], key=t["modes"].get) if t["modes"] else "unknown"
        out.append(t)
    return out


def build(cfg: Config, days: int | None = None) -> dict:
    since = datetime.now().astimezone() - timedelta(days=days) if days else None
    vehicles, notes = [], []
    spec = cat.load(cfg)

    with db.connect(cfg) as conn:
        vins = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT vin FROM telemetry ORDER BY vin"
            ).fetchall()
        ]
        for vin in vins:
            where = "vin=%s" + (" AND ts >= %s" if since else "")
            args = (vin, since) if since else (vin,)
            fixes = conn.execute(
                "SELECT ts, lat, lon, altitude_m, heading_deg, satellites, fix_status"
                f" FROM location WHERE {where} ORDER BY ts",
                args,
            ).fetchall()

            soc, soc_key = Series([]), None
            for candidate in SOC_CANDIDATES:
                soc = _series(conn, vin, candidate)
                if soc:
                    soc_key = candidate
                    break

            battery_kwh = 0.0
            for candidate in BATTERY_KWH:
                found = _series(conn, vin, candidate)
                if found and found.values[-1]:
                    battery_kwh = found.values[-1]
                    break

            s = {
                "dist": _series(conn, vin, DISTANCE),
                "fuel_pct": _series(conn, vin, FUEL_PCT),
                "fuel_l": _series(conn, vin, FUEL_LITRES),
                "soc": soc,
                "battery_kwh": battery_kwh,
                "engine": [_series(conn, vin, k, "bool") for k in ENGINE_KEYS],
                "hv": _series(conn, vin, HV_STATUS, "txt"),
            }

            if not fixes:
                notes.append(f"{vin}: no location fixes yet, state panel only")
            if soc_key is None:
                notes.append(f"{vin}: no state-of-charge recorded yet")
            if fixes and not any(s["engine"]):
                notes.append(
                    f"{vin}: no engine-state recorded yet, so petrol vs electric "
                    f"cannot be separated"
                )

            points, prev = [], None
            trip_id, jumps, stationary = 0, 0, 0
            for ts, lat, lon, alt, heading, sats, fix_status in fixes:
                point = {
                    "t": int(ts.timestamp() * 1000),
                    "iso": ts.isoformat(),
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "heading": heading,
                    "sats": sats,
                    "fix": fix_status,
                }

                if prev is None:
                    point |= {"mode": "start", "intensity": 0.0, "km": 0.0,
                              "trip": trip_id, "draw": False}
                else:
                    step_m = haversine_m(prev["lat"], prev["lon"], lat, lon)
                    gap_s = (point["t"] - prev["t"]) / 1000.0
                    point["step_m"] = round(step_m)

                    if gap_s > GAP_SPLIT_SECONDS or step_m > MAX_STEP_M:
                        # A feed gap or a post-tunnel jump. Drawing across it
                        # invents a road that was never travelled and produces a
                        # consumption figure for a distance we did not observe.
                        if step_m > MAX_STEP_M and gap_s <= GAP_SPLIT_SECONDS:
                            jumps += 1
                        trip_id += 1
                        point |= {"mode": "start", "intensity": 0.0, "km": 0.0,
                                  "trip": trip_id, "draw": False}
                    elif step_m < MIN_STEP_M:
                        # Parked jitter, or one of BMW's repeated bursts.
                        stationary += 1
                        point |= {"mode": "idle", "intensity": 0.0, "km": 0.0,
                                  "trip": trip_id, "draw": False}
                    else:
                        point |= _mode(prev["ts"], ts, s, step_m / 1000.0)
                        point |= {"trip": trip_id, "draw": True}

                point["ts"] = ts  # internal; stripped before serialising
                points.append(point)
                prev = point

            if jumps:
                notes.append(f"{vin}: {jumps} fix(es) jumped >{MAX_STEP_M} m, not drawn")
            if stationary:
                notes.append(f"{vin}: {stationary} stationary fix(es) below {MIN_STEP_M} m")

            trips = _trips(points)
            for p in points:
                p.pop("ts", None)

            vehicles.append(
                {
                    "vin": vin,
                    "label": vin[-6:],
                    "battery_kwh": battery_kwh,
                    "soc_key": soc_key,
                    "points": points,
                    "trips": trips,
                    "state": _state_series(conn, vin, since),
                }
            )

    # Labels and categories straight from BMW, for every key we actually hold.
    seen = {k for v in vehicles for k in v["state"]}
    labels = {
        key: {
            "n": spec[key].get("name"),
            "c": CATEGORY_LABELS.get(spec[key].get("category"), "Other"),
            "d": spec[key].get("description"),
        }
        for key in seen
        if key in spec
    }
    if seen and not labels:
        notes.append("catalogue not fetched; run: bmwcd catalogue")

    return {
        "generated": datetime.now().astimezone().isoformat(),
        "vehicles": vehicles,
        "labels": labels,
        "categories": list(CATEGORY_LABELS.values()),
        "notes": notes,
    }


def render(cfg: Config, days: int | None = None) -> tuple[Path, dict]:
    data = build(cfg, days)
    out = cfg.data_dir / "viz" / "map.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text().replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"), default=str)
    )
    out.write_text(html)
    return out, data
