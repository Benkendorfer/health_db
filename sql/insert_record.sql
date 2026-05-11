-- Parameterized insert into the generic records table. ? placeholders are
-- bound by the Python driver (sqlite3.Connection.executemany). Idempotent:
-- the natural-key UNIQUE constraint on records collapses re-ingested rows.

INSERT OR IGNORE INTO records
    (record_type, source_name, start_date, end_date,
     value, unit, source_version, creation_date)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
