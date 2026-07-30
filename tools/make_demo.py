#!/usr/bin/env python3
"""Generate docs/demo.html — a public, shareable preview of the map page.

Everything here is invented. The route is centred on Amsterdam city centre,
deliberately NOT on any real recorded position, because this file is committed
to a public repository. Never point this at the database.

Re-run after changing the template so the published preview keeps up:
    python tools/make_demo.py && tools/screenshot.sh
"""

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATE = ROOT / "bmwcd" / "viz" / "map.html"
OUT = ROOT / "docs" / "demo.html"

# Amsterdam centre. A neutral, obviously-public landmark.
LAT0, LON0 = 52.3676, 4.9041
START = datetime(2026, 3, 14, 8, 30, tzinfo=timezone.utc)

# A 40-minute stop partway through, so the preview shows two separate trips
# rather than one implausible continuous route.
BREAK_AT, BREAK_MINUTES = 45, 40

points = []
for i in range(90):
    t = START + timedelta(seconds=45 * i)
    if i >= BREAK_AT:
        t += timedelta(minutes=BREAK_MINUTES)
    lat = LAT0 + 0.035 * math.sin(i / 14) + 0.010 * math.sin(i / 3.1)
    lon = LON0 + 0.055 * math.cos(i / 11) + 0.014 * math.cos(i / 4.3)

    if i == 0:
        mode, intensity, extra = "start", 0.0, {}
    elif i < 25:
        mode = "electric"
        intensity = 0.25 + 0.5 * abs(math.sin(i / 6))
        extra = {"kwh_per_100km": round(12 + 24 * intensity, 1)}
    elif i < 38:
        mode = "regen"
        intensity = 0.3 + 0.6 * abs(math.sin(i / 4))
        extra = {"kwh": round(0.05 * intensity, 3)}
    elif i < 66:
        mode = "petrol"
        intensity = 0.2 + 0.7 * abs(math.cos(i / 7))
        extra = {"l_per_100km": round(4 + 12 * intensity, 1)}
    else:
        mode = "electric"
        intensity = 0.15 + 0.35 * abs(math.cos(i / 5))
        extra = {"kwh_per_100km": round(12 + 24 * intensity, 1)}

    points.append(
        {
            "t": int(t.timestamp() * 1000),
            "iso": t.isoformat(),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "alt": 4 + (i % 7),
            "heading": (i * 7) % 360,
            "sats": 9 + (i % 3),
            "fix": "GPS_FIX_3D",
            "mode": mode,
            "intensity": round(intensity, 3),
            "km": 0.42,
            "step_m": 420,
            "gap_s": 45 if i not in (0, BREAK_AT) else None,
            "trip": 0 if i < BREAK_AT else 1,
            "draw": i not in (0, BREAK_AT),
            **extra,
        }
    )

# The two fixes that open a trip are not joined to what came before.
for i in (0, BREAK_AT):
    points[i] |= {"mode": "start", "intensity": 0.0, "km": 0.0}
    points[i].pop("kwh_per_100km", None)
    points[i].pop("l_per_100km", None)
    points[i].pop("kwh", None)


def series(unit, fn):
    """Emit value changes only, matching what export.py produces."""
    out, last = [], object()
    for i, p in enumerate(points):
        value = fn(i)
        if value != last:
            out.append([p["t"], value])
            last = value
    return {"u": unit, "v": out}


state = {
    "vehicle.vehicle.speedRange.lowerBound": series("km/h", lambda i: 10 * (i % 9)),
    "vehicle.vehicle.speedRange.upperBound": series("km/h", lambda i: 10 * (i % 9) + 10),
    "vehicle.drivetrain.batteryManagement.header": series("percent", lambda i: 78 - i // 3),
    "vehicle.drivetrain.fuelSystem.level": series("percent", lambda i: 100 - i // 9),
    "vehicle.drivetrain.fuelSystem.remainingFuel": series("l", lambda i: round(69 - i * 0.06, 1)),
    "vehicle.vehicle.travelledDistance": series("km", lambda i: 41200 + round(i * 0.42)),
    "vehicle.drivetrain.lastRemainingRange": series("km", lambda i: 640 - i),
    "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange": series("km", lambda i: 84 - i // 2),
    "vehicle.drivetrain.electricEngine.charging.hvStatus": series(None, lambda i: "NOT_CHARGING"),
    "vehicle.drivetrain.engine.isIgnitionOn": series(None, lambda i: 25 <= i < 66),
    "vehicle.chassis.axle.row1.wheel.left.tire.pressure": series("kpa", lambda i: 238 + (i % 4)),
    "vehicle.chassis.axle.row1.wheel.right.tire.pressure": series("kpa", lambda i: 241 - (i % 3)),
    "vehicle.cabin.door.row1.driver.isOpen": series(None, lambda i: False),
    "vehicle.cabin.window.row1.driver.status": series(None, lambda i: "CLOSED"),
    "vehicle.body.trunk.isOpen": series(None, lambda i: False),
}

trips = []
for trip_id in (0, 1):
    members = [p for p in points if p["trip"] == trip_id and p["draw"]]
    if not members:
        continue
    modes: dict[str, float] = {}
    for p in members:
        modes[p["mode"]] = modes.get(p["mode"], 0.0) + p["km"]
    trips.append(
        {
            "trip": trip_id,
            "start": min(p["t"] for p in members),
            "end": max(p["t"] for p in members),
            "km": round(sum(p["km"] for p in members), 2),
            "minutes": round(
                (max(p["t"] for p in members) - min(p["t"] for p in members)) / 60000, 1
            ),
            "modes": modes,
            "dominant": max(modes, key=modes.get),
        }
    )

# Real BMW display names and categories when the catalogue has been fetched;
# the page falls back to names derived from the key path otherwise.
labels = {}
try:
    from bmwcd import catalogue as cat, config as _config

    spec = cat.load(_config.load())
    labels = {
        k: {
            "n": spec[k].get("name"),
            "c": {
                "BASIC_DATA": "Basic data", "VEHICLE_STATUS": "Vehicle status",
                "USAGE_BASED": "Usage", "EVENTS": "Events",
                "BEV_PHEV_DATA": "Battery & charging", "META_DATA": "Metadata",
                "TYRE_DATA": "Tyres", "CD_CONTRACT": "Contract",
            }.get(spec[k].get("category"), "Other"),
            "d": spec[k].get("description"),
        }
        for k in state
        if k in spec
    }
except Exception:  # noqa: BLE001 - the demo must build without a configured env
    pass

data = {
    "generated": START.isoformat(),
    "vehicles": [
        {
            "vin": "DEMO0000000000000",
            "label": "DEMO",
            "battery_kwh": 26,
            "soc_key": "vehicle.drivetrain.batteryManagement.header",
            "points": points,
            "trips": trips,
            "state": state,
        }
    ],
    "labels": labels,
    "notes": ["Demo page — invented data, no real vehicle or location"],
}

OUT.parent.mkdir(parents=True, exist_ok=True)
html = TEMPLATE.read_text().replace(
    "/*__DATA__*/null", json.dumps(data, separators=(",", ":"))
)
OUT.write_text(html)
print(f"wrote {OUT} ({len(points)} points, {len(state)} state keys)")
