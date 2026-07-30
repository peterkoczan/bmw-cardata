-- CarData telemetry store.
--
-- One row per (vehicle, key, measurement time). BMW sends exactly one key per
-- MQTT message, so the long/EAV shape is the natural fit -- and it absorbs the
-- 245 selected keys without a 245-column table or a type-per-key migration
-- every time BMW adds an attribute.

CREATE TABLE IF NOT EXISTS telemetry (
    ts      timestamptz      NOT NULL,  -- data[key].timestamp: when the value was measured
    msg_ts  timestamptz      NOT NULL,  -- payload.timestamp: when BMW emitted the message
    vin     text             NOT NULL,
    key     text             NOT NULL,
    -- Values arrive as int, float, bool or string depending on the key, so each
    -- gets its own typed column rather than being flattened to text. Exactly one
    -- is non-null per row.
    num     double precision,
    bool    boolean,
    txt     text,
    unit    text,
    -- BMW re-sends identical readings; observed duplicate lat/long at the same
    -- timestamp on the first capture. Dedupe at write time via ON CONFLICT.
    PRIMARY KEY (vin, key, ts)
);

CREATE INDEX IF NOT EXISTS telemetry_vin_key_ts_desc ON telemetry (vin, key, ts DESC);
CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry (ts);

-- BMW's own telematic data catalogue: display names, units, datatypes, value
-- ranges and categories. Public, unauthenticated, refreshed with
-- `bmwcd catalogue`. Joining against it beats inventing our own taxonomy.
CREATE TABLE IF NOT EXISTS catalogue (
    key           text PRIMARY KEY,
    name          text,
    description   text,
    unit          text,
    datatype      text,
    value_range   text,
    category      text,
    streamable    boolean,
    vehicle_types text[]
);

-- Location fixes, paired within a tolerance window.
--
-- Latitude and longitude arrive as separate messages. They have shared an
-- identical measurement timestamp in every fix observed so far, but an exact
-- join silently drops a fix whenever they straddle a boundary. Pairing to the
-- nearest partner within a few seconds cannot produce a worse answer than
-- discarding the position entirely.
--
-- Written against the base table with exact key equality rather than a CTE with
-- LIKE. A multiply-referenced CTE is an optimisation fence: Postgres
-- materialised it and every LATERAL then scanned the whole thing per latitude
-- row, which measured as clean n-squared (360 fixes 0.28s, 2880 fixes 18.1s).
-- Exact keys hit telemetry_vin_key_ts_desc; a prefix LIKE cannot under a
-- non-C collation.
--
-- Validity predicates live INSIDE each LATERAL, not in the outer WHERE: above
-- the LIMIT 1 a single out-of-range partner deletes the whole fix, instead of
-- simply losing to the valid one two seconds away.
DROP VIEW IF EXISTS location;
CREATE VIEW location AS
SELECT
    l.vin,
    l.ts,
    l.num AS lat,
    lo.lon,
    al.altitude_m,
    hd.heading_deg,
    sa.satellites,
    fx.fix_status
FROM telemetry l
CROSS JOIN LATERAL (
    SELECT o.num AS lon
    FROM telemetry o
    WHERE o.vin = l.vin
      AND o.key = 'vehicle.cabin.infotainment.navigation.currentLocation.longitude'
      AND o.num IS NOT NULL
      AND o.num BETWEEN -180 AND 180
      -- Null island is a sensor artefact, never a real position.
      AND NOT (abs(l.num) < 0.1 AND abs(o.num) < 0.1)
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts))
    LIMIT 1
) lo
LEFT JOIN LATERAL (
    SELECT o.num AS altitude_m FROM telemetry o
    WHERE o.vin = l.vin
      AND o.key = 'vehicle.cabin.infotainment.navigation.currentLocation.altitude'
      AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) al ON true
LEFT JOIN LATERAL (
    SELECT o.num AS heading_deg FROM telemetry o
    WHERE o.vin = l.vin
      AND o.key = 'vehicle.cabin.infotainment.navigation.currentLocation.heading'
      AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) hd ON true
LEFT JOIN LATERAL (
    SELECT o.num AS satellites FROM telemetry o
    WHERE o.vin = l.vin
      AND o.key = 'vehicle.cabin.infotainment.navigation.currentLocation.numberOfSatellites'
      AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) sa ON true
LEFT JOIN LATERAL (
    SELECT o.txt AS fix_status FROM telemetry o
    WHERE o.vin = l.vin
      AND o.key = 'vehicle.cabin.infotainment.navigation.currentLocation.fixStatus'
      AND o.txt IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) fx ON true
WHERE l.key = 'vehicle.cabin.infotainment.navigation.currentLocation.latitude'
  AND l.num IS NOT NULL
  AND l.num BETWEEN -90 AND 90
  -- Drop unresolved fixes. The catalogue says NO_FIX; the localised portal
  -- writes "NO FIX". Normalise rather than trusting either spelling.
  AND replace(replace(upper(coalesce(fx.fix_status, '')), '_', ''), ' ', '') <> 'NOFIX';

-- Latest value per key -- the "state as of now" a dashboard header wants.
CREATE OR REPLACE VIEW latest AS
SELECT DISTINCT ON (vin, key) vin, key, ts, num, bool, txt, unit
FROM telemetry
ORDER BY vin, key, ts DESC;
