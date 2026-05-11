-- Parameterized insert. The ? placeholders are bound by the Python driver
-- (sqlite3.Connection.executemany). Idempotent.

INSERT OR IGNORE INTO active_energy
    (source_name, start_date, end_date, value, unit, source_version, creation_date)
VALUES (?, ?, ?, ?, ?, ?, ?);
