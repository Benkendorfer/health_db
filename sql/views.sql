-- Views over the records table.
--
-- records_canonical: deduplicates across sources at (record_type, day)
-- granularity. For each (type, day), keep rows from the source(s) with the
-- lowest priority value in source_priority. Sources missing from
-- source_priority are treated as worst-priority fallbacks, so they appear
-- only on days no ranked source reported.
--
-- <type>_canonical: thin per-type filters over records_canonical, kept for
-- readability when querying a single metric.
--
-- daily_<type>_canonical: per-day aggregations -- the single source of truth
-- for "X per day". SUM for accumulating quantities (energy); MEDIAN for
-- point measurements with intra-day noise (body mass: multiple weigh-ins,
-- some of which may be transient like post-meal). Analysis scripts should
-- query these rather than re-deriving aggregations.

-- Drop in reverse dependency order, then recreate forward.
DROP VIEW IF EXISTS daily_body_mass_canonical;
DROP VIEW IF EXISTS daily_active_energy_canonical;
DROP VIEW IF EXISTS daily_basal_energy_canonical;
DROP VIEW IF EXISTS daily_calories_consumed_canonical;
DROP VIEW IF EXISTS body_mass_canonical;
DROP VIEW IF EXISTS active_energy_canonical;
DROP VIEW IF EXISTS basal_energy_canonical;
DROP VIEW IF EXISTS calories_consumed_canonical;
DROP VIEW IF EXISTS records_canonical;

CREATE VIEW records_canonical AS
WITH ranked AS (
    SELECT
        r.*,
        SUBSTR(r.start_date, 1, 10) AS day,
        COALESCE(sp.priority, 1000000) AS priority
    FROM records AS r
    LEFT JOIN source_priority AS sp
        ON sp.source_name = r.source_name
       AND sp.record_type = r.record_type
),
day_min AS (
    SELECT record_type, day, MIN(priority) AS min_priority
    FROM ranked
    GROUP BY record_type, day
)
SELECT
    r.id, r.record_type, r.source_name, r.start_date, r.end_date,
    r.value, r.unit, r.source_version, r.creation_date
FROM ranked AS r
JOIN day_min AS dm
    ON dm.record_type = r.record_type
   AND dm.day = r.day
   AND dm.min_priority = r.priority;

CREATE VIEW active_energy_canonical AS
SELECT source_name, start_date, end_date, value, unit, source_version, creation_date
FROM records_canonical
WHERE record_type = 'ActiveEnergyBurned';

CREATE VIEW body_mass_canonical AS
SELECT source_name, start_date, end_date, value, unit, source_version, creation_date
FROM records_canonical
WHERE record_type = 'BodyMass';

CREATE VIEW daily_active_energy_canonical AS
SELECT SUBSTR(start_date, 1, 10) AS day, SUM(value) AS kcal
FROM active_energy_canonical
GROUP BY day;

CREATE VIEW basal_energy_canonical AS
SELECT source_name, start_date, end_date, value, unit, source_version, creation_date
FROM records_canonical
WHERE record_type = 'BasalEnergyBurned';

CREATE VIEW daily_basal_energy_canonical AS
SELECT SUBSTR(start_date, 1, 10) AS day, SUM(value) AS kcal
FROM basal_energy_canonical
GROUP BY day;

CREATE VIEW calories_consumed_canonical AS
SELECT source_name, start_date, end_date, value, unit, source_version, creation_date
FROM records_canonical
WHERE record_type = 'CaloriesConsumed';

CREATE VIEW daily_calories_consumed_canonical AS
SELECT SUBSTR(start_date, 1, 10) AS day, SUM(value) AS kcal
FROM calories_consumed_canonical
GROUP BY day;

-- Median emulation for SQLite < 3.44. For each day, number the readings by
-- ascending value; pick the middle row (odd count) or the two middle rows
-- (even count), then AVG. (cnt+1)/2 and (cnt+2)/2 land on the same row for
-- odd cnt and on adjacent rows for even cnt -- exactly the median definition.
CREATE VIEW daily_body_mass_canonical AS
WITH numbered AS (
    SELECT
        SUBSTR(start_date, 1, 10) AS day,
        value,
        ROW_NUMBER() OVER (PARTITION BY SUBSTR(start_date, 1, 10) ORDER BY value) AS rn,
        COUNT(*)    OVER (PARTITION BY SUBSTR(start_date, 1, 10))                 AS cnt
    FROM body_mass_canonical
)
SELECT day, AVG(value) AS kg
FROM numbered
WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
GROUP BY day;
