# health_db

Quick repo to run analyses over data collected with Apple Health. Uses an SQLite database to manage the data after import.

- [health\_db](#health_db)
  - [Layout](#layout)
  - [Setup](#setup)
  - [Ingesting an export](#ingesting-an-export)

## Layout

```txt
src/    Python code
sql/    SQL code
data/
  raw/  Place export.xml here (the file Apple Health produces)
  db/   SQLite database location
```

## Setup

Environment requirements are in `pyproject.toml`.

## Ingesting an export

Place the unzipped `export.xml` from Apple Health into `data/raw/`, then generate the SQLite database with

```bash
python src/parse_apple_health.py data/raw/export.xml data/db/health.db
```

Re-running the parser does not duplicate rows.
