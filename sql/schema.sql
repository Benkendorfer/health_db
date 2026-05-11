-- Schema for Apple Health ingest. Idempotent

CREATE TABLE IF NOT EXISTS active_energy (
    source_name    TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    value          REAL NOT NULL,
    unit           TEXT NOT NULL,
    source_version TEXT,
    creation_date  TEXT,
    PRIMARY KEY (source_name, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_active_energy_start
    ON active_energy (start_date);
