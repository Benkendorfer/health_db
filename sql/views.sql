-- Views over the raw record tables.
--
-- active_energy_canonical: deduplicates across sources at day granularity.
-- Rule: if Connect (Garmin) reports any active energy on a day, use Connect's
-- rows for that day; otherwise fall back to non-Connect rows.
--
-- daily_active_energy_canonical: rolls active_energy_canonical up to one row
-- per day. The single source of truth for "kcal burned on day X" -- any
-- analysis script should query this rather than re-deriving the aggregation.

-- Drop in reverse dependency order, then recreate forward.
DROP VIEW IF EXISTS daily_active_energy_canonical;
DROP VIEW IF EXISTS active_energy_canonical;

CREATE VIEW active_energy_canonical AS
WITH connect_days AS (
    SELECT DISTINCT SUBSTR(start_date, 1, 10) AS day
    FROM active_energy
    WHERE source_name = 'Connect'
)
SELECT ae.*
FROM active_energy AS ae
WHERE
    ae.source_name = 'Connect'
    OR SUBSTR(ae.start_date, 1, 10) NOT IN (SELECT day FROM connect_days);

CREATE VIEW daily_active_energy_canonical AS
SELECT
    SUBSTR(start_date, 1, 10) AS day,
    SUM(value) AS kcal
FROM active_energy_canonical
GROUP BY day;
