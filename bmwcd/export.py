"""Build a self-contained map page from recorded telemetry.

Everything is embedded in the HTML rather than fetched, so the page works from
a file:// path -- a browser will not fetch() a sibling JSON file off disk.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import ROOT, Config

TEMPLATE = Path(__file__).parent / "viz" / "map.html"

DISTANCE = "vehicle.vehicle.travelledDistance"
FUEL_PCT = "vehicle.drivetrain.fuelSystem.level"
FUEL_LITRES = "vehicle.drivetrain.fuelSystem.remainingFuel"

# State of charge. `batteryManagement.header` is the streamed actual value;
# `electricEngine.charging.level` looks right but is the *predicted* SoC and is
# not streamable at all.
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
MOVING = "vehicle.isMoving"
HV_STATUS = "vehicle.drivetrain.electricEngine.charging.hvStatus"

# Rough envelopes for normalising a segment's intensity to 0..1 for shading.
ELECTRIC_KWH_PER_100KM = (12.0, 40.0)
FUEL_L_PER_100KM = (4.0, 16.0)
FUEL_PCT_PER_100KM = (2.0, 12.0)


def _series(conn, vin: str, key: str, column: str = "num"):
    return conn.execute(
        f"SELECT ts, {column} FROM telemetry"
        f" WHERE vin=%s AND key=%s AND {column} IS NOT NULL ORDER BY ts",
        (vin, key),
    ).fetchall()


def _at(series, when):
    """Last value at or before `when`, or None. Series is ascending by ts."""
    value = None
    for ts, num in series:
        if ts > when:
            break
        value = num
    return value


def _scale(value, lo, hi):
    return max(0.0, min(1.0, (value - lo) / (hi - lo))) if hi > lo else 0.5


def _mode(prev_t, t, s):
    """Classify the drive between two fixes.

    CarData exposes no instantaneous power or fuel-flow signal at all -- every
    consumption key is a lifetime total, a per-trip accumulator or a running
    average. So mode is derived: engine state splits petrol from electric, and
    the shade comes from differencing state of charge or fuel level over the
    distance covered between the two fixes.
    """
    d0, d1 = _at(s["dist"], prev_t), _at(s["dist"], t)
    km = (d1 - d0) if (d0 is not None and d1 is not None) else None
    if not km or km <= 0:
        return {"mode": "idle", "intensity": 0.0, "km": km or 0.0}

    soc0, soc1 = _at(s["soc"], prev_t), _at(s["soc"], t)
    d_soc = (soc1 - soc0) if (soc0 is not None and soc1 is not None) else None
    battery = s["battery_kwh"]

    engine_on = any(_at(series, t) for series in s["engine"] if series)
    hv = (_at(s["hv"], t) or "") if s["hv"] else ""
    plugged = hv.upper() in {"CHARGING", "WAITING_FOR_CHARGING"}

    out = {"km": round(km, 3)}

    # Charge climbing while under way, and not plugged in, is recuperation.
    if d_soc is not None and d_soc > 0.5 and not plugged:
        kwh = (d_soc / 100.0) * battery if battery else 0.0
        return out | {
            "mode": "regen",
            "intensity": _scale(kwh / km * 100, *ELECTRIC_KWH_PER_100KM),
            "kwh": round(kwh, 3),
        }

    if engine_on:
        l0, l1 = _at(s["fuel_l"], prev_t), _at(s["fuel_l"], t)
        d_litres = (l0 - l1) if (l0 is not None and l1 is not None) else None
        if d_litres and d_litres > 0:
            per100 = d_litres / km * 100
            return out | {
                "mode": "petrol",
                "intensity": _scale(per100, *FUEL_L_PER_100KM),
                "l_per_100km": round(per100, 1),
            }
        p0, p1 = _at(s["fuel_pct"], prev_t), _at(s["fuel_pct"], t)
        d_pct = (p0 - p1) if (p0 is not None and p1 is not None) else None
        per100 = (d_pct / km * 100) if d_pct and d_pct > 0 else None
        return out | {
            "mode": "petrol",
            "intensity": _scale(per100, *FUEL_PCT_PER_100KM) if per100 else 0.5,
            **({"fuel_pct_per_100km": round(per100, 2)} if per100 else {}),
        }

    if d_soc is not None and d_soc < 0:
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
        value = num if num is not None else (bool_ if bool_ is not None else txt)
        if value is None:
            continue
        if key in last and last[key] == value:
            continue
        last[key] = value
        entry = out.setdefault(key, {"u": unit, "v": []})
        entry["v"].append([int(ts.timestamp() * 1000), value])
    return out


def build(cfg: Config, days: int | None = None) -> dict:
    since = datetime.now().astimezone() - timedelta(days=days) if days else None
    vehicles, notes = [], []

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
            # A vehicle with telemetry but no GPS fix (a parked i3) still belongs
            # in the state panel, so carry on with an empty route.
            soc, soc_key = [], None
            for candidate in SOC_CANDIDATES:
                soc = _series(conn, vin, candidate)
                if soc:
                    soc_key = candidate
                    break

            battery_kwh = 0.0
            for candidate in BATTERY_KWH:
                found = _series(conn, vin, candidate)
                if found:
                    battery_kwh = found[-1][1]
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

            points, prev_t = [], None
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
                point |= (
                    _mode(prev_t, ts, s)
                    if prev_t
                    else {"mode": "start", "intensity": 0.0, "km": 0.0}
                )
                points.append(point)
                prev_t = ts

            vehicles.append(
                {
                    "vin": vin,
                    "label": vin[-6:],
                    "battery_kwh": battery_kwh,
                    "soc_key": soc_key,
                    "points": points,
                    "state": _state_series(conn, vin, since),
                }
            )

    return {
        "generated": datetime.now().astimezone().isoformat(),
        "vehicles": vehicles,
        "notes": notes,
    }


def render(cfg: Config, days: int | None = None) -> tuple[Path, dict]:
    data = build(cfg, days)
    out = cfg.data_dir / "viz" / "map.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text().replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"))
    )
    out.write_text(html)
    return out, data
