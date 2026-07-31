# bmw-cardata

I wanted my own copy of what my car reports. BMW's CarData gives you a live MQTT
stream of telemetry, so this subscribes to it, writes everything to disk, puts it
in Postgres, and draws a map I can scrub back through.

It runs on my laptop as a background service with a menu bar icon that tells me
whether it's actually working.

![Map preview](docs/screenshot.png)

[Live demo](https://peterkoczan.github.io/bmw-cardata/demo.html) — made-up data,
not my car.

## What you need

This is macOS-only, and not by accident. The service supervision is launchd, the
menu bar app is native, and a few things shell out to `open` and `pbcopy`. The
core of it (auth, the MQTT client, the database, the map export) is plain Python
and would run anywhere. Everything around it wouldn't. On Linux you'd swap
launchd for systemd and skip the menu bar.

| | |
|---|---|
| OS | macOS 13 or newer |
| Python | 3.11+, because I use `tomllib` |
| Database | PostgreSQL 14+ (the installer sets up 17 via Homebrew) |
| Packages | paho-mqtt, requests, certifi, psycopg, rumps |
| Network | outbound TLS 1.3 to `customer.streaming-cardata.bmwgroup.com:9000` |

Things no code can fix:

- CarData streaming is EU-only right now.
- You need a My BMW account with the car mapped to it, and you have to be the
  **primary user**. A secondary user can't set up a stream.
- How much you get depends on the car. Mine has Live Cockpit Professional and
  reports its position every 3 minutes or so while driving. Live Cockpit Plus
  only reports at the start and end of a trip. My i3 barely reports at all.

You never give this your BMW password. You sign in on BMW's own site and approve
a code.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/peterkoczan/bmw-cardata/main/install.sh | bash
```

It clones to `~/Developer/bmw-cardata` (set `BMWCD_DIR` if you want it
elsewhere), makes the venv, installs Postgres if you don't have it (it asks
first), creates the database, fetches BMW's key catalogue and loads the launchd
agents. You can run it again safely — it updates the clone and leaves your
`config.toml` alone.

Then click the menu bar icon and pick **Set up / re-authorise…**. It tells you
what to do in the BMW portal, takes your Client ID, and handles the sign-in.

## Setting it up in the BMW portal

The menu bar app walks you through this, but here it is written down:

1. Sign in to My BMW, go to your vehicle overview, open **BMW CarData** under
   your car, accept the terms. Note that accepting also signs you up to whatever
   they change in the next six weeks.
2. **Create CarData client**, copy the Client ID.
3. Turn on **both** toggles: API access and CarData stream. If you skip the
   stream one, sign-in still works and then MQTT rejects you with nothing useful
   in the error. That one cost me a while.
4. **Configure data stream**, tick what you want, submit. You do this per car.
5. Don't click **Authenticate device**. This app does that part itself.

## Using it

```
.venv/bin/python -m bmwcd auth       # sign in (or use the menu bar)
.venv/bin/python -m bmwcd initdb     # create the schema
.venv/bin/python -m bmwcd catalogue  # fetch BMW's key metadata
.venv/bin/python -m bmwcd status     # what's happening
.venv/bin/python -m bmwcd stream     # subscribe
.venv/bin/python -m bmwcd export     # build the map
.venv/bin/python -m bmwcd load       # rebuild the DB from the raw files
.venv/bin/python -m bmwcd prune      # apply retention
```

Only one `stream` can run at once — there's a lock file. BMW allows one
connection per account, so two of them just kick each other off in a loop.

## Running it in the background

```
./launchd/install.sh            # all three agents
./launchd/install.sh menubar    # just one
```

Three agents: the stream itself (restarts if it dies), a nightly prune at 04:00,
and the menu bar app. If a plist hasn't changed the script leaves that agent
running rather than bouncing it, because restarting the stream for no reason
costs you data.

Logs go to `data/logs/` and rotate on size, not age — they grow with how much you
drive, not with the calendar. Default is 10 MB with 3 old copies kept
(`log_max_mb`, `log_keep`). The running stream checks every 30 seconds and the
nightly prune checks too, in case it isn't running.

It rotates by copying and truncating rather than renaming, which looks odd until
you remember launchd opens the log itself and holds onto that file for the life
of the job. Rename it and launchd cheerfully keeps writing to the renamed copy
while the "current" log sits there empty.

### The menu bar

| Icon | What it means |
|---|---|
| green | streaming, and the car said something recently |
| yellow | streaming, but nothing for 6+ hours — usually just parked |
| orange | streaming, but the database is unreachable |
| red | not streaming |
| grey | not set up yet, or I can't tell |

The important bit: green means *actually connected*, not just "the process is
alive". The stream writes a heartbeat and the icon reads it. Without that a
crashed reconnect loop looks identical to a car sitting in a car park, which is
the one thing I wanted to be able to tell apart.

The menu has the setup flow, a retention setting, start/stop/restart, and
**Open map**, which rebuilds from whatever is in the database right now and opens
it. Stopping from here actually stops it rather than having launchd bring it
straight back.

**Rename vehicle** lists every car that has streamed anything and lets you call
it something better than a VIN tail. The list refreshes itself, so a newly added
car turns up within a few minutes of its first message — no restart, no config
editing. Names are written to `config.toml`; see [More than one
car](#more-than-one-car).

### Sleep

If I close the lid I lose whatever the car sent while it was shut. That's fine,
I'm not going to leave it open all night. What matters is that it picks itself
back up, and it does.

Unless you stop it deliberately, it always comes back — from sleep, from losing
wifi, from anything. Getting that right took two goes.

First: the wait was driven by the monotonic clock while the token expires on real
time, so it could come back from a long sleep thinking it still had 50 minutes
left on a token that died an hour ago. That one's driven by wall clock now.

Then a real overnight test caught the actual problem. The lid was shut for 90
minutes, and every reconnect during that time failed with no network — but each
failure still climbed the backoff ladder: 5s, 10, 20, 40, 80, 160. It eventually
got a connection, dropped once more, and that landed on step 7, which is a
5-minute wait. So it looked dead when it was just sleeping off a counter it
should never have accumulated.

Two changes. Attempts that never reach BMW no longer count — if DNS can't even
resolve the broker there's no network, so it retries steadily every 15s without
escalating. And a session that actually connected and ran for a minute resets the
counter, because whatever ends it is a fresh incident, not the seventh step of
something.

Every log line is timestamped now too. Working out what happened that night meant
lining the log up against `pmset -g log` by hand, which was annoying enough once.

If you want no gaps at all, this belongs on something that's always on. Just
remember it has to *move* there, not run in both places.

## Storage

Plain PostgreSQL. No Timescale — at the rate a parked car produces data the
hypertable machinery buys nothing, and retention is one scheduled `DELETE`.

`telemetry` is one row per (car, key, timestamp), with the value in a typed
column: `num`, `bool` or `txt`. Keys genuinely differ in type, which is also why
this isn't InfluxDB — Influx fixes a field's type on first write and then rejects
everything that disagrees.

The primary key deduplicates as it writes. BMW re-sends a lot: my first capture
was 277 messages carrying 168 actual rows.

Two views: `location` pairs latitude and longitude into points (they arrive as
separate messages), and `latest` is the most recent value per key.

Scrubbing back through time is just a range scan:

```sql
SELECT ts, lat, lon FROM location
WHERE vin = $1 AND ts BETWEEN $2 AND $3
ORDER BY ts;
```

The raw JSONL is the real source of truth and it outlives the database, so I can
drop Postgres and rebuild it with `load` whenever I change my mind about the
schema.

## The map

```
.venv/bin/python -m bmwcd export [--days N]
open data/viz/map.html
```

Everything is embedded in the HTML, so it works straight off disk without a web
server. There's a time slider that moves the car to where it was, and hovering
the route shows what was happening at that point.

The route is coloured by what was driving the car: blue for electric, red for
petrol, green when it was recovering energy, darker for heavier use.

**That colour is worked out, not measured.** CarData has no instantaneous power
or fuel-flow reading anywhere in its catalogue — everything is a lifetime total,
a per-trip total, or an average. So I take engine state to decide petrol vs
electric, and work out the shade from how much charge or fuel disappeared over
the distance between two fixes. It's an estimate across a segment, not a reading
at a point.

There's also no speed. BMW deliberately doesn't send it: "due to privacy reasons
some functions are not allowed to transmit the current driving speed". You get a
range like 50–60 km/h, so that's what the readout shows.

### More than one car

The subscription is a single wildcard topic covering the whole account, so I
never told it which cars to expect. **A car shows up the first time it streams
anything** — the map builds its list from what's actually in the database, not
from a list I maintain. Add a car in the BMW portal, configure its stream, and it
appears on its own. Nothing here assumes there are two of them.

Buttons along the top switch between them, and switching changes everything: the
map draws that car's route and nothing else, the dashboard shows its readings,
the trip list is its trips.

The time slider is per car too, which sounds like a detail and isn't. My X5 had
eleven hours of history one day and the i3 had two minutes the next; on a shared
timeline the whole of the i3 sat in the last pixel of the track and couldn't be
scrubbed at all. Each car now gets its own axis. The page opens on whichever one
reported most recently, since that's the one I came to look at.

### Naming the cars

Until you name one it's labelled by the tail of its VIN, which tells you nothing.
There are two ways to fix that, and they're for different situations.

**Double-click the name on the map** to edit it in place. Enter saves, Escape
cancels, and clearing it puts the old name back. This is the quick one — but the
map page is opened straight off disk and a `file://` page can't write to my
config, so the name lives in that browser's local storage. It shows with a `*`
and the line under the dashboard says what everything else still calls the car,
so I can't forget which is which.

**Rename vehicle in the menu bar** is the real one. It writes `config.toml`,
which the exporter and the CLI both read, so the name follows the car
everywhere:

```toml
[names]
"WBAXXXXXXXXXXXXXX" = "X5"
"WBYXXXXXXXXXXXXXX" = "i3"
```

Keep that block at the bottom of the file — everything after a TOML table header
belongs to that table, so anything you put below it stops being a top-level
setting.

If the two ever disagree, config wins: a name typed into the browser is dropped
as soon as `config.toml` says something different. Otherwise a label I typed once
would quietly outrank every change I made afterwards.

One more thing worth knowing: a car that has never reported fuel above zero is
treated as battery-only, and its fuel tiles and the petrol swatch disappear. The
i3 reports `remainingFuel` and `lastRemainingRange` as a flat zero, and "Fuel in
tank 0 l" next to "Electric range 219 km" is worse than showing nothing.

### Trips

Nothing is sent while the car is parked, so a day of fixes is several separate
drives rather than one route. Joining them all up would draw roads I never drove
and invent fuel consumption for distance I never covered. So fixes only get
connected when they look like continuous movement:

| Rule | Threshold |
|---|---|
| gap ends a trip | 10 minutes |
| below this it hasn't moved | 10 m |
| above this it's a tunnel, not a drive | 2 km |

### Watching it live

**Open map** from the menu bar now serves the page instead of writing a file,
and the page updates itself as data arrives. No refreshing. Leave it open on a
second monitor while you drive and the route extends itself.

```
.venv/bin/python -m bmwcd serve [--port N]
```

is the same thing from a terminal. It binds to `127.0.0.1` and nothing else — not
reachable from your network, GET only, three fixed routes, no credentials, writes
nothing. Default port 8770, `map_port` in config.

It polls a cheap `/stamp.json` (one indexed query: newest timestamp and row
count) every 15 seconds and only pulls the full payload when that moves. When it
does, the page rebuilds and **puts you back where you were** — same car, same
date and trip filter, same scrub position. If you'd panned or zoomed the map
yourself it keeps your view; if you hadn't, it reframes, so a drive in progress
stays in shot as it grows. There's a green **live** dot on the right of the
pickers that pulses on each update, and goes grey if the server goes away.

Opened as a file it still works exactly as before — one self-contained snapshot,
no badge, no polling. A `file://` page has an opaque origin and can't fetch
anything, which is why the data is embedded in the first place.

### Narrowing it down

Two dropdowns under the vehicle row: **Date** and **Trip**.

Pick a date and everything narrows to that day — the map draws only that day's
fixes, the slider spans only that day, and the trip dropdown drops to the trips
on it. Pick a trip and it narrows again to that one drive, framed to its own
bounds, with the summary next to it: distance, duration, average speed, and what
it cost in kWh or litres.

They were buttons in a row before. That worked with one day of data and stopped
working the moment there was more — a month of retention is dozens of trips, and
the row was already scrolling sideways after a single day. **Show all** puts
everything back, and switching cars clears the filter, since one car's dates mean
nothing to another.

The date list counts what's on each day, including days where the car never moved
but still reported something — a charge finishing, a door opening. Those show as
"state only" and still scrub, because the dashboard has something to say even
when the map doesn't.

## The catalogue

```
.venv/bin/python -m bmwcd catalogue
```

BMW publishes their whole telematic catalogue at a public endpoint that needs no
authentication at all. 294 keys, 245 of them streamable, which is exactly what
the portal offers you. Each one has a proper display name, unit, datatype and
category.

It's worth having because it types the values properly instead of guessing from
whatever turned up first:

- Numbers sometimes arrive quoted. `batterySizeMax` came through as the string
  `"0.0"` and went into the text column where no numeric query would ever find
  it.
- Plenty of "boolean" keys don't use true/false. They use `OPEN`/`CLOSED`,
  `FLAP_UNLOCKED`/`FLAP_LOCKED`, `NOTCHOSEN`/`CHOSEN`. I work out which way round
  from the key's own name (`isOpen` means `OPEN` is true), *not* from the order
  they're listed in — `isOpen` ships both as `CLOSED, OPEN, INVALID` and as
  `OPEN, CLOSED, INVALID, UNKNOWN`, so going by position gets half of them
  backwards. If it can't be worked out for certain, the value stays as text.
  Unmapped is still queryable, wrong is a lie.

It's also how I know `batteryManagement.header` is state of charge — BMW's own
name for it is "Charging status of high-voltage battery".

## Remote control

There isn't any. Every CarData endpoint is a `GET` except the ones that manage
which data you've selected. No location refresh, no remote functions. Those live
on the separate unofficial MyBMW API that `bimmer_connected` talks to.

## Things that took me a while

- MQTT v5, TLS, port 9000, keepalive 30 seconds or less.
- The topic is `{gcid}/+`. The portal shows the bare VIN as the "topic" but
  that's only half of it — your `id_token` carries the broker's access rule as a
  regex requiring the topic to start with your account ID. Subscribe to the bare
  VIN and you get "not authorized", and BMW then drops the **whole** connection,
  taking your working subscriptions with it. Don't test topics on a live
  subscriber.
- Username is the account ID, password is the **`id_token`**, not the access
  token.
- The `id_token` dies every hour, so the connection is rebuilt each time rather
  than swapping credentials underneath it.
- The refresh token lasts about two weeks. Longer than that off and you sign in
  again.
- One connection per account. If you want Home Assistant to have this too, this
  service has to republish it locally — HA can't connect to BMW alongside it.
- It's forward-only. Location history builds up here and can't be backfilled.
  For older data there's the Customer Archive export, or the REST charging
  history endpoint, which is limited to 50 requests a day.
- Long silences from the i3 are the car, not a bug.

## Sources

Endpoints confirmed against
[whi-tw/bmw-cardata-streaming-poc](https://github.com/whi-tw/bmw-cardata-streaming-poc)
and BMW's own
[customer API swagger](https://bmw-cardata.bmwgroup.com/customer/public/assets/swagger/swagger-customer-api-v1.json).
