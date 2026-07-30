# bmw-cardata

Standalone subscriber for the BMW CarData streaming interface: MQTT → raw JSONL
→ PostgreSQL, with a self-contained map page for replaying where the car went
and what it was doing.

## Portal prerequisites

1. My BMW → Personal Data → My Vehicles → CarData → **Create Client ID**.
   Do *not* click "Authenticate Vehicle".
2. **Subscribe the client ID to CarData Streaming** — this allocates the
   `cardata:streaming:read` scope. Must happen before running `auth`, otherwise
   the token comes back scopeless and MQTT rejects it with nothing useful in
   the error. `auth` checks for this and fails loudly.
3. **Change data selection** → enable the telematic keys you want. Nothing
   streams for keys that aren't selected.

## Use

```
.venv/bin/python -m bmwcd auth       # device-code flow, one-off
.venv/bin/python -m bmwcd initdb     # create the schema
.venv/bin/python -m bmwcd catalogue  # fetch BMW's key metadata
.venv/bin/python -m bmwcd status     # token state and row counts
.venv/bin/python -m bmwcd stream     # subscribe -> data/raw/ + Postgres
.venv/bin/python -m bmwcd export     # render data/viz/map.html
```

Only one `stream` may run at a time — a `flock` on `data/stream.lock` enforces
it. BMW allows one connection per GCID, so a second process does not duplicate
work, it evicts the first in a loop and neither stays connected.

During `auth`, finish the BMW login in the browser completely before touching
the terminal — interrupting the flow wedges the device code and it has to be
restarted.

## Running as a service

```
./launchd/install.sh
```

Installs three user LaunchAgents (idempotent — re-run after editing a plist):

- `nl.koczan.bmw-cardata.stream` — `RunAtLoad` + `KeepAlive`, so it starts at
  login and restarts on any exit. `ThrottleInterval` 30s stops a crash-loop
  hammering BMW when the refresh token has expired and needs `bmwcd auth` by hand.
- `nl.koczan.bmw-cardata.prune` — daily at 04:00.
- `nl.koczan.bmw-cardata.menubar` — the status indicator (below).

Logs land in `data/logs/`. Check state with `launchctl list | grep bmw-cardata`.

### Sleep

Closing the lid loses data — the feed is forward-only and nothing buffers it —
but the service resumes on its own without intervention.

That needed a specific fix. The session wait was driven by `time.monotonic()`,
which macOS **stops while the machine is asleep**, whereas the `id_token`
expires on wall-clock time. A single monotonic wait would therefore return from
a two-hour sleep still believing it had 50 minutes left on a token that died an
hour earlier, sitting on a socket the network dropped long before. The wait is
now sliced and compared against `time.time()`, so a resume is noticed within 30
seconds and cycles straight into a refresh and reconnect.

For genuinely continuous capture the service belongs on an always-on host — at
which point remember BMW allows only one stream connection per GCID, so it moves
rather than being duplicated.

### Menu bar

```
python -m bmwcd menubar     # or let the launchd agent run it
```

A status glyph with the details behind it:

| Glyph | Meaning |
|---|---|
| 🟢 | Streaming, database up, heard from the car recently |
| 🟡 | Up, but nothing from the car for over 6 hours — usually just parked |
| 🟠 | Streaming, but the database is unreachable |
| 🔴 | Subscriber not running |

The menu shows the stream PID, database reachability, how long since the last
message, and row/key counts. Actions: restart, stop and start the stream,
rebuild and open the map, open the log.

Control goes through `launchctl` rather than signalling the process directly, so
the supervisor's own view of the job stays correct — stopping from here means
stopped, not restarted three seconds later by `KeepAlive`.

## Storage

PostgreSQL (`brew install postgresql@17`), plain — no Timescale. At a parked
car's message rate the hypertable machinery earns nothing, and retention is one
scheduled `DELETE`. Add Timescale later if volume ever justifies it; the schema
does not change.

`telemetry` is one row per `(vin, key, ts)`. Values land in a typed column —
`num`, `bool` or `txt` — because keys genuinely differ in type (of the first 277
messages: 99 int, 95 str, 74 bool, 9 float). That heterogeneity is why this is
not InfluxDB: Influx pins a field's type on first write and rejects the rest.

The primary key dedupes at write time. BMW re-sends identical readings — the
first capture had 277 messages carrying 168 distinct rows.

```
python -m bmwcd initdb   # idempotent
python -m bmwcd load     # backfill from raw JSONL, idempotent
python -m bmwcd prune    # apply retention_days + raw_retention_days
```

Alongside `telemetry` sits `catalogue` (see below) and two views:

- `location` — latitude, longitude, altitude, heading, satellites and fix status
  as points. The two coordinates arrive as *separate messages*; they have shared
  an identical measurement timestamp in every fix seen so far, but the view
  pairs each latitude with the nearest longitude within 5 seconds rather than
  requiring an exact match, so a near-miss degrades to a slightly-off pairing
  instead of a discarded position. It also drops `NO_FIX`, near-zero
  coordinates, and out-of-range values.
- `latest` — most recent value per key, for a dashboard header.

Replay for a time slider is a plain range scan:

```sql
SELECT ts, lat, lon FROM location
WHERE vin = $1 AND ts BETWEEN $2 AND $3
ORDER BY ts;
```

State as of an instant:

```sql
SELECT DISTINCT ON (key) key, ts, num, bool, txt, unit
FROM telemetry WHERE vin = $1 AND ts <= $2
ORDER BY key, ts DESC;
```

Raw JSONL stays the source of truth and outlives the database (`raw_retention_days`,
default 2x), so the DB can be dropped and rebuilt with `load` if the schema changes.

## Map

[![Map preview](docs/screenshot.png)](https://peterkoczan.github.io/bmw-cardata/demo.html)

*Click for the [live demo](https://peterkoczan.github.io/bmw-cardata/demo.html) — invented data, no real vehicle or location.*

```
python -m bmwcd export [--days N]
open data/viz/map.html
```

Regenerate the published preview after changing the template:

```
python tools/make_demo.py && tools/screenshot.sh
```

Leaflet page with all data embedded inline, so it works from `file://` without a
server — a browser will not `fetch()` a sibling JSON file off disk. Time slider
scrubs the vehicle to where it was at that instant and dims the route ahead of it.

Route colour encodes drivetrain mode: blue electric, red petrol, green
recuperating, with shade by intensity. Tracing the route with the cursor shows a
readout of that moment — speed, consumption, charge, fuel, odometer, heading,
altitude and fix quality. Below the map, a state panel shows every recorded key
as it stood at the slider's position.

**There is no exact speed in CarData.** BMW withholds it deliberately: "due to
privacy reasons some functions are not allowed to transmit the current driving
speed". The only speed signal is `vehicle.vehicle.speedRange.lowerBound` /
`.upperBound`, so the readout shows a band rather than a number.

**Mode is derived, not measured.** CarData exposes no instantaneous power or
fuel-flow signal anywhere in the catalogue — every consumption key is a lifetime
total, a per-trip accumulator or a running average. `avgAuxPower` looks like the
exception but covers auxiliary load only, not traction. So mode comes from engine
state, and shade from differencing state of charge or fuel level over the
distance between consecutive fixes. It is an estimate over a segment, not a
reading at a point.

Keys that matter, all confirmed streamable:

| Purpose | Key |
|---|---|
| State of charge | `vehicle.drivetrain.batteryManagement.header` |
| Battery capacity | `vehicle.drivetrain.batteryManagement.maxEnergy` |
| Engine running | `vehicle.drivetrain.engine.isIgnitionOn` / `.isActive` |
| Fuel | `vehicle.drivetrain.fuelSystem.level` (%), `.remainingFuel` (l) |
| Charging state | `vehicle.drivetrain.electricEngine.charging.hvStatus` |

Two traps in that list. BMW's catalogue has the *human-readable names* of the two
engine keys crossed over relative to their key names, so both are consulted and
either one reporting "running" wins. And `electricEngine.charging.level` looks
like SoC but is the *predicted* value and is not streamable at all.

Position updates depend on the instrument cluster, not the API: Live Cockpit
Professional emits roughly every 3 minutes or 2 km while moving, Live Cockpit
Plus only at trip start and end. Expect a coarse polyline either way. Fixes
reporting `NO_FIX`, null island, or out-of-range coordinates are dropped by the
`location` view.

### Trips, not one long line

Nothing is transmitted while parked, so a day's fixes are several separate
drives. Joining them all would draw roads that were never travelled and invent
a consumption figure for distance nobody covered. Consecutive fixes are only
connected when they look like continuous movement:

| Rule | Threshold | Why |
|---|---|---|
| Gap ends a trip | 10 min | Longer silence means parked, not driving |
| Below this, no movement | 10 m | Parked jitter, and BMW's repeated bursts |
| Above this, no segment | 2 km | A tunnel catch-up, not a drive |

Trips are listed under the map; clicking one zooms to it and scrubs the panel
to how it finished.

Odometer deltas are sanity-checked too — BMW has been known to report a km
value labelled as miles — and fall back to GPS distance when implausible.

State-panel labels and groupings come from the catalogue, so tiles read
"Charging status of high-voltage battery" rather than a name derived from the
key path. That label is also how we know `batteryManagement.header` really is
state of charge.

## Catalogue

```
python -m bmwcd catalogue
```

BMW publishes the telematic data catalogue at a **public, unauthenticated**
endpoint — no CarData credentials involved. 294 keys, 245 of them streamable
(exactly the set the portal offers). Each carries a display name, unit,
datatype, value range and category, cached to `data/catalogue.json` and loaded
into the `catalogue` table for joining against `telemetry`.

It earns its place by typing values from BMW's metadata rather than from
whatever arrived first:

- **Numbers sometimes arrive quoted.** `batterySizeMax` came through as the
  string `"0.0"`. Typing on the Python type alone buried it in `txt`, invisible
  to every numeric query. Now `num` is populated too, with the raw text kept.
- **Many "boolean" keys ship bespoke vocabularies** — `OPEN/CLOSED`,
  `FLAP_UNLOCKED/FLAP_LOCKED`, `NOTCHOSEN/CHOSEN`. The mapping is derived from
  each key's own predicate (`isOpen` → `OPEN` is true), *not* from position in
  the range string: `isOpen` ships as both `CLOSED, OPEN, INVALID` and
  `OPEN, CLOSED, INVALID, UNKNOWN`, so positional derivation inverts half of
  them. Where polarity can't be established the value stays text — an unmapped
  value is still queryable, a wrong one is a lie.
- Keys with three or more real states (`isPermanentlyUnlocked` →
  `NO_ACTION, FLAP_UNLOCKED, FLAP_LOCKED`) are deliberately left alone.

## Reliability

The feed is forward-only, so every failure mode below costs data permanently.

- **Reconnect backs off** 5s doubling to 5 min, then 10 min after ten
  consecutive failures, seeded from the MQTT reason code — quota-exceeded waits
  60s, because reconnecting hard against a quota error is how you stay
  quota-exceeded.
- **A failed token refresh no longer kills the process.** `invalid_grant` is
  fatal and asks for `bmwcd auth`; network errors and 5xx back off and retry.
- **Connect has a timeout.** A wedged TLS handshake never fires a callback, so
  a blocking connect can hang forever with nothing to notice.
- **Database writes happen on their own thread** behind a bounded queue.
  Writing inline on paho's network thread meant a slow Postgres delayed PINGREQ
  past the 30s keep-alive and got us disconnected — a database problem becoming
  a data-loss problem. On overflow, messages are dropped and counted: the JSONL
  still has them and `bmwcd load` repairs the gap.
- **Tokens are written atomically, and never incompletely.** BMW rotates the
  refresh token on every refresh, so truncating in place risked destroying the
  only copy of a two-week credential. A refresh response that omits
  `refresh_token`, `id_token` or `gcid` is also refused rather than saved —
  writing a partial file would discard a credential that was still valid.
- **One `stream` at a time**, enforced by a `flock`. Two processes evict each
  other under BMW's one-connection-per-GCID rule and neither survives.

There is deliberately **no stall watchdog**. The `id_token` expires hourly, so
the loop already tears the connection down and rebuilds it every ~55 minutes; a
wedged subscription cannot outlive that. A separate silence timer would either
duplicate the token cycle or fire while the car is legitimately parked, which is
most of the time.

## Remote control

There is none. Every CarData endpoint is a `GET` except `POST`/`DELETE` on
`/customers/containers`, which manages data selection rather than the car. No
location refresh, no remote functions. Those live on the separate, unofficial
MyBMW API that `bimmer_connected` speaks.

## Facts worth not relearning

- MQTT **v5.0**, TLS, port 9000, keep-alive ≤ 30s.
- Topic: **`{gcid}/+`**. The `id_token` carries the broker ACL in its
  `dynamic_scopes` claim as `read:streaming/*/{gcid}.*`, so a topic must begin
  with the GCID. The portal's connection panel shows the bare VIN as "Thema" —
  that is only the second component, and subscribing to it returns 0x87 Not
  authorized. BMW then drops the **entire connection**, so one speculative
  topic kills the working ones. Never probe topics on the live subscriber.
- Username is the GCID, password is the **`id_token`** (not the access token).
- `id_token` expires hourly → the stream loop tears down and rebuilds the
  connection each cycle rather than swapping credentials in place.
- Refresh token lasts ~2 weeks. Longer downtime than that means re-running `auth`.
- **One stream connection per GCID.** If Home Assistant should also get this
  data, this service has to republish to a local broker; HA cannot connect to
  BMW in parallel.
- The stream is forward-only. Location history accumulates here and cannot be
  backfilled from it — use the portal's Customer Archive export or the REST
  charging-history endpoint (rate limited to 50 req/24h).
- The i3 reports infrequently (typically parked/off or at 100% charge). Long
  silences are the vehicle, not the client.

## Sources

Endpoints confirmed against `whi-tw/bmw-cardata-streaming-poc` AUTHENTICATION.md
and `https://bmw-cardata.bmwgroup.com/customer/public/assets/swagger/swagger-customer-api-v1.json`.
