#!/usr/bin/env python3
"""Generate docs/demo.html — a public, shareable preview of the map page.

Everything here is invented. The routes are centred on Amsterdam city centre,
deliberately NOT on any real recorded position, because this file is committed
to a public repository. Never point this at the database.

Two cars, because the switcher is the thing worth showing: a plug-in hybrid that
uses both drivetrains, and a battery car that has neither a tank nor a petrol
swatch in the legend.

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

# An overnight stop partway through, so the preview shows two separate trips on
# two separate days -- one route would be implausible, and one day would leave
# the date picker with nothing to pick.
BREAK_AT, BREAK_MINUTES = 45, 22 * 60


def series(points, unit, fn):
    """Emit value changes only, matching what export.py produces."""
    out, last = [], object()
    for i, p in enumerate(points):
        value = fn(i)
        if value != last:
            out.append([p["t"], value])
            last = value
    return {"u": unit, "v": out}


def hybrid():
    """A PHEV: electric, then recuperating, then petrol, then electric again."""
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
        for field in ("kwh_per_100km", "l_per_100km", "kwh"):
            points[i].pop(field, None)

    state = {
        "vehicle.vehicle.speedRange.lowerBound":
            series(points, "km/h", lambda i: 10 * (i % 9)),
        "vehicle.vehicle.speedRange.upperBound":
            series(points, "km/h", lambda i: 10 * (i % 9) + 10),
        "vehicle.drivetrain.batteryManagement.header":
            series(points, "percent", lambda i: 78 - i // 3),
        "vehicle.drivetrain.fuelSystem.level":
            series(points, "percent", lambda i: 100 - i // 9),
        "vehicle.drivetrain.fuelSystem.remainingFuel":
            series(points, "l", lambda i: round(69 - i * 0.06, 1)),
        "vehicle.vehicle.travelledDistance":
            series(points, "km", lambda i: 41200 + round(i * 0.42)),
        "vehicle.drivetrain.lastRemainingRange":
            series(points, "km", lambda i: 640 - i),
        "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange":
            series(points, "km", lambda i: 84 - i // 2),
        "vehicle.drivetrain.electricEngine.charging.hvStatus":
            series(points, None, lambda i: "NOT_CHARGING"),
        "vehicle.drivetrain.engine.isIgnitionOn":
            series(points, None, lambda i: 25 <= i < 66),
        "vehicle.chassis.axle.row1.wheel.left.tire.pressure":
            series(points, "kpa", lambda i: 238 + (i % 4)),
        "vehicle.chassis.axle.row1.wheel.right.tire.pressure":
            series(points, "kpa", lambda i: 241 - (i % 3)),
        "vehicle.cabin.door.row1.driver.isOpen":
            series(points, None, lambda i: False),
        "vehicle.cabin.window.row1.driver.status":
            series(points, None, lambda i: "CLOSED"),
        "vehicle.body.trunk.isOpen":
            series(points, None, lambda i: False),
    }
    return points, state


def battery():
    """A BEV, on a shorter and sparser run through a different part of town.

    Its cadence is deliberately unlike the hybrid's: two cars with very
    different histories is exactly the case the per-vehicle timeline exists for.
    """
    points = []
    for i in range(26):
        # Runs and finishes before the hybrid does. The page opens on whichever
        # car reported most recently, and the hybrid is the better first
        # impression: it is the one that shows all three route colours.
        t = START + timedelta(minutes=8, seconds=70 * i)
        lat = LAT0 - 0.022 + 0.016 * math.sin(i / 5.5)
        lon = LON0 + 0.030 + 0.026 * math.cos(i / 6.5)
        if i == 0:
            mode, intensity, extra = "start", 0.0, {}
        else:
            mode = "regen" if 9 <= i < 13 else "electric"
            intensity = 0.3 + 0.45 * abs(math.sin(i / 4.5))
            extra = (
                {"kwh": round(0.06 * intensity, 3)} if mode == "regen"
                else {"kwh_per_100km": round(13 + 9 * intensity, 1)}
            )
        points.append(
            {
                "t": int(t.timestamp() * 1000),
                "iso": t.isoformat(),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "alt": 2 + (i % 4),
                "heading": (i * 13) % 360,
                "mode": mode,
                "intensity": round(intensity, 3),
                "km": 0.61,
                "step_m": 610,
                "gap_s": 70 if i else None,
                "trip": 0,
                "draw": i != 0,
                **extra,
            }
        )

    # A BEV reports these as a flat zero. Present on purpose: the page is
    # supposed to keep them out of the headline tiles, and the demo should show
    # that happening rather than dodge it by leaving the keys out.
    state = {
        "vehicle.drivetrain.batteryManagement.header":
            series(points, "percent", lambda i: 88 - i // 2),
        "vehicle.powertrain.electric.battery.stateOfCharge.displayed":
            series(points, "percent", lambda i: round(87.6 - i / 2, 1)),
        "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange":
            series(points, "km", lambda i: 214 - i),
        "vehicle.vehicle.travelledDistance":
            series(points, "km", lambda i: 56651 + round(i * 0.61)),
        "vehicle.drivetrain.fuelSystem.remainingFuel":
            series(points, "l", lambda i: 0),
        "vehicle.drivetrain.lastRemainingRange":
            series(points, "km", lambda i: 0),
        "vehicle.drivetrain.electricEngine.charging.status":
            series(points, None, lambda i: "NOCHARGING"),
        "vehicle.body.chargingPort.status":
            series(points, None, lambda i: "DISCONNECTED"),
        "vehicle.drivetrain.batteryManagement.maxEnergy":
            series(points, "kwh", lambda i: 27.66),
        "vehicle.cabin.door.status":
            series(points, None, lambda i: "SECURED"),
        "vehicle.body.trunk.isOpen":
            series(points, None, lambda i: False),
    }
    return points, state


def trips_from(points):
    out = []
    for trip_id in sorted({p["trip"] for p in points}):
        members = [p for p in points if p["trip"] == trip_id and p["draw"]]
        if not members:
            continue
        modes: dict[str, float] = {}
        for p in members:
            modes[p["mode"]] = modes.get(p["mode"], 0.0) + p["km"]
        start, end = min(p["t"] for p in members), max(p["t"] for p in members)
        km = round(sum(p["km"] for p in members), 2)
        minutes = round((end - start) / 60000, 1)
        out.append(
            {
                "trip": trip_id,
                "start": start,
                "end": end,
                "km": km,
                "minutes": minutes,
                "modes": modes,
                "dominant": max(modes, key=modes.get),
                "avg_kmh": round(km / (minutes / 60)) if minutes else None,
            }
        )
    return out


# Two invented cars. `burns_fuel` is what the exporter derives from whether a
# fuel reading was ever above zero; stated outright here because this data
# never went through the exporter.
VEHICLES = [
    {
        "vin": "DEMO0000000000001",
        "label": "X5",
        "battery_kwh": 26,
        "burns_fuel": True,
        "build": hybrid,
        "notes": ["demo hybrid: both drivetrains in one route"],
    },
    {
        "vin": "DEMO0000000000002",
        "label": "i3",
        "battery_kwh": 27.66,
        "burns_fuel": False,
        "build": battery,
        "notes": ["demo battery car: no tank, so no fuel tiles and no petrol swatch"],
    },
]

vehicles = []
for spec in VEHICLES:
    points, state = spec["build"]()
    vehicles.append(
        {
            "vin": spec["vin"],
            "label": spec["label"],
            "battery_kwh": spec["battery_kwh"],
            "burns_fuel": spec["burns_fuel"],
            "soc_key": "vehicle.drivetrain.batteryManagement.header",
            "points": points,
            "trips": trips_from(points),
            "state": state,
            "notes": spec["notes"],
        }
    )

# Real BMW display names and categories when the catalogue has been fetched;
# the page falls back to names derived from the key path otherwise.
labels = {}
try:
    from bmwcd import catalogue as cat, config as _config

    spec = cat.load(_config.load())
    labels = {
        key: {
            "n": " ".join((spec[key].get("name") or "").split()) or None,
            "c": {
                "BASIC_DATA": "Basic data", "VEHICLE_STATUS": "Vehicle status",
                "USAGE_BASED": "Usage", "EVENTS": "Events",
                "BEV_PHEV_DATA": "Battery & charging", "META_DATA": "Metadata",
                "TYRE_DATA": "Tyres", "CD_CONTRACT": "Contract",
            }.get(spec[key].get("category"), "Other"),
            "d": spec[key].get("description"),
        }
        for key in {k for v in vehicles for k in v["state"]}
        if key in spec
    }
except (Exception, SystemExit):  # noqa: BLE001 - must build without a configured env
    # SystemExit explicitly: config.load() raises it when there is no
    # config.toml, which is exactly the state of a fresh clone. A bare
    # `except Exception` does not catch a BaseException, so the guard that was
    # meant to make this work on a fresh clone was the thing stopping it.
    pass

data = {
    "generated": START.isoformat(),
    "vehicles": vehicles,
    # Published to GitHub Pages, which serves over https -- and the page turns
    # live polling on for any http(s) origin. Without this flag the demo shows a
    # "live" badge, asks a static site for stamp.json every 15 seconds, and sits
    # there greyed out having never reached anything.
    "static": True,
    "labels": labels,
    "notes": [
        "Demo page — invented data, no real vehicle or location",
        "run it yourself and the map updates as the car reports, no refreshing",
    ],
}

OUT.parent.mkdir(parents=True, exist_ok=True)
html = TEMPLATE.read_text().replace(
    "/*__DATA__*/null", json.dumps(data, separators=(",", ":"))
)
OUT.write_text(html)
print(
    f"wrote {OUT} ({len(vehicles)} vehicles, "
    f"{sum(len(v['points']) for v in vehicles)} points)"
)
