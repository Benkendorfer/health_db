"""Parse Apple Health export XML into a SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterator

from lxml import etree  # type: ignore[attr-defined]

ACTIVE_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"


def iter_records(xml_path: Path, record_type: str | None = None) -> Iterator[dict]:
    """Stream <Record> elements from an Apple Health export.

    If record_type is given, only records whose `type` attribute matches are yielded.
    Uses iterparse with element clearing so memory stays flat on multi-GB exports.
    """
    context = etree.iterparse(str(xml_path), events=("end",), tag="Record")
    for _, elem in context:
        if record_type is None or elem.attrib.get("type") == record_type:
            yield dict(elem.attrib)
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]


def load_active_energy(xml_path: Path, db_path: Path, batch_size: int = 10_000) -> int:
    """Extract ActiveEnergyBurned records into a SQLite table. Returns row count."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS active_energy")
        conn.execute(
            """
            CREATE TABLE active_energy (
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                source_name TEXT,
                source_version TEXT,
                creation_date TEXT
            )
            """
        )
        insert_sql = (
            "INSERT INTO active_energy "
            "(start_date, end_date, value, unit, source_name, source_version, creation_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )

        batch: list[tuple] = []
        total = 0
        for rec in iter_records(xml_path, ACTIVE_ENERGY_TYPE):
            batch.append(
                (
                    rec.get("startDate"),
                    rec.get("endDate"),
                    float(rec["value"]),
                    rec.get("unit"),
                    rec.get("sourceName"),
                    rec.get("sourceVersion"),
                    rec.get("creationDate"),
                )
            )
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)
            total += len(batch)

        conn.execute("CREATE INDEX idx_active_energy_start ON active_energy(start_date)")
        conn.commit()
        return total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, help="Path to export.xml")
    parser.add_argument("db_path", type=Path, help="Path to SQLite database to write")
    args = parser.parse_args()

    n = load_active_energy(args.xml_path, args.db_path)
    print(f"Inserted {n:,} active energy records into {args.db_path}")


if __name__ == "__main__":
    main()
