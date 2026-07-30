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

-- Location fixes pivoted into points.
--
-- Latitude and longitude arrive as separate messages but share an identical
-- measurement timestamp, so a plain group-by pairs them exactly -- no
-- interpolation, no time-bucket fudging. Confirmed against the first capture.
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
-- join silently drops a fix whenever they straddle a boundary -- one was lost
-- that way in the first capture. Pairing to the nearest partner within a few
-- seconds costs nothing and cannot produce a worse answer than discarding the
-- position entirely.
DROP VIEW IF EXISTS location;
CREATE VIEW location AS
WITH loc AS (
    SELECT vin, ts, key, num, txt
    FROM telemetry
    WHERE key LIKE 'vehicle.cabin.infotainment.navigation.currentLocation.%'
),
lat AS (
    SELECT vin, ts, num AS lat FROM loc
    WHERE key LIKE '%.latitude' AND num IS NOT NULL
)
SELECT
    l.vin,
    l.ts,
    l.lat,
    lo.lon,
    al.altitude_m,
    hd.heading_deg,
    sa.satellites,
    fx.fix_status
FROM lat l
CROSS JOIN LATERAL (
    SELECT o.num AS lon FROM loc o
    WHERE o.vin = l.vin AND o.key LIKE '%.longitude' AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) lo
LEFT JOIN LATERAL (
    SELECT o.num AS altitude_m FROM loc o
    WHERE o.vin = l.vin AND o.key LIKE '%.altitude' AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) al ON true
LEFT JOIN LATERAL (
    SELECT o.num AS heading_deg FROM loc o
    WHERE o.vin = l.vin AND o.key LIKE '%.heading' AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) hd ON true
LEFT JOIN LATERAL (
    SELECT o.num AS satellites FROM loc o
    WHERE o.vin = l.vin AND o.key LIKE '%.numberOfSatellites' AND o.num IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) sa ON true
LEFT JOIN LATERAL (
    SELECT o.txt AS fix_status FROM loc o
    WHERE o.vin = l.vin AND o.key LIKE '%.fixStatus' AND o.txt IS NOT NULL
      AND o.ts BETWEEN l.ts - interval '5 seconds' AND l.ts + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM o.ts - l.ts)) LIMIT 1
) fx ON true
-- Null island is a sensor artefact, never a real position. The loose bound
-- catches near-zero noise as well as exact zeros.
WHERE NOT (abs(l.lat) < 0.1 AND abs(lo.lon) < 0.1)
  AND l.lat BETWEEN -90 AND 90
  AND lo.lon BETWEEN -180 AND 180
  -- Drop unresolved fixes. The catalogue says NO_FIX; the localised portal
  -- writes "NO FIX". Normalise rather than trusting either spelling.
  AND replace(replace(upper(coalesce(fx.fix_status, '')), '_', ''), ' ', '') <> 'NOFIX';

-- Latest value per key -- the "state as of now" a dashboard header wants.
CREATE OR REPLACE VIEW latest AS
SELECT DISTINCT ON (vin, key) vin, key, ts, num, bool, txt, unit
FROM telemetry
ORDER BY vin, key, ts DESC;
