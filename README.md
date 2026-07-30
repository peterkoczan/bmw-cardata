# bmw-cardata

Standalone subscriber for the BMW CarData streaming interface. Milestone 1:
land raw JSONL on disk. Storage (InfluxDB/Timescale) and Grafana come after.

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
.venv/bin/python -m bmwcd auth      # device-code flow, one-off
.venv/bin/python -m bmwcd status    # token state
.venv/bin/python -m bmwcd stream    # subscribe, append to data/raw/
```

During `auth`, finish the BMW login in the browser completely before touching
the terminal — interrupting the flow wedges the device code and it has to be
restarted.

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

Two views ship with the schema:

- `location` — lat/lon/altitude/heading pivoted into points. Latitude and
  longitude arrive as *separate messages* but share an identical measurement
  timestamp, so this is an exact group-by, not an interpolation.
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
