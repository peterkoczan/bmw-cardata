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
CREATE OR REPLACE VIEW location AS
SELECT
    vin,
    ts,
    max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.latitude')  AS lat,
    max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.longitude') AS lon,
    max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.altitude')  AS altitude_m,
    max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.heading')   AS heading_deg
FROM telemetry
WHERE key LIKE 'vehicle.cabin.infotainment.navigation.currentLocation.%'
GROUP BY vin, ts
HAVING max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.latitude')  IS NOT NULL
   AND max(num) FILTER (WHERE key = 'vehicle.cabin.infotainment.navigation.currentLocation.longitude') IS NOT NULL;

-- Latest value per key -- the "state as of now" a dashboard header wants.
CREATE OR REPLACE VIEW latest AS
SELECT DISTINCT ON (vin, key) vin, key, ts, num, bool, txt, unit
FROM telemetry
ORDER BY vin, key, ts DESC;
