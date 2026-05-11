-- Schema for Apple Health ingest. Idempotent.

-- Generic table for quantity-shape records: anything that's
-- (value + unit + interval). Type-specific structure (workouts, sleep,
-- categoricals) belongs in dedicated tables when added later.
--
-- Surrogate id + natural-key UNIQUE: lets legitimate same-interval
-- datapoints coexist (e.g. multiple Watch samples in the same second) while
-- still making re-ingest idempotent. NULL source_version/creation_date can
-- cause SQLite to admit "duplicates" because NULL != NULL in UNIQUE
-- constraints, but Apple Health fills these in practice.
CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY,
    record_type     TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT NOT NULL,
    source_version  TEXT,
    creation_date   TEXT,
    UNIQUE (record_type, source_name, source_version,
            start_date, end_date, value, creation_date)
);

-- Supports day-bucketed views that filter by record_type.
CREATE INDEX IF NOT EXISTS idx_records_type_day
    ON records (record_type, substr(start_date, 1, 10));

-- Per-record-type source priority. Lower number = preferred.
-- Sources with no row here are treated as worst-priority fallbacks.
CREATE TABLE IF NOT EXISTS source_priority (
    record_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    PRIMARY KEY (record_type, source_name)
);

-- Default preferences. INSERT OR IGNORE so manual edits survive re-ingest.
INSERT OR IGNORE INTO source_priority (record_type, source_name, priority)
VALUES
    -- Garmin Connect wins; iPhone unranked = fallback when Connect is silent.
    ('ActiveEnergyBurned', 'Connect',       0),
    -- Smart scales beat manual entries; eufy (current) outranks Connect on
    -- the brief Oct-2025 handoff. Manual sources stay unranked-equivalent
    -- so they appear only on days no scale reported.
    ('BodyMass',           'eufy Life',     0),
    ('BodyMass',           'Connect',       1),
    ('BodyMass',           'Health',       10),
    -- Garmin Connect wins
    ('BasalEnergyBurned',  'Connect',       0),
    -- MyFitnessPal wins
    ('CaloriesConsumed',   'MyFitnessPal',  0);
