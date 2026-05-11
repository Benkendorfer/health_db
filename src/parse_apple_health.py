"""Parse Apple Health export XML into a SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterator

from lxml import etree  # type: ignore[attr-defined]

ACTIVE_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


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


def load_active_energy(
    xml_path: Path, db_path: Path, batch_size: int = 10_000
) -> tuple[int, int]:
    """Ingest ActiveEnergyBurned records idempotently. Returns (seen, inserted)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = (SQL_DIR / "schema.sql").read_text()
    views_sql = (SQL_DIR / "views.sql").read_text()
    insert_sql = (SQL_DIR / "insert_active_energy.sql").read_text()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.executescript(views_sql)
        before = conn.execute("SELECT COUNT(*) FROM active_energy").fetchone()[0]

        batch: list[tuple] = []
        seen = 0
        for rec in iter_records(xml_path, ACTIVE_ENERGY_TYPE):
            batch.append(
                (
                    rec.get("sourceName"),
                    rec.get("startDate"),
                    rec.get("endDate"),
                    float(rec["value"]),
                    rec.get("unit"),
                    rec.get("sourceVersion"),
                    rec.get("creationDate"),
                )
            )
            seen += 1
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)

        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM active_energy").fetchone()[0]
        return seen, after - before
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, help="Path to export.xml")
    parser.add_argument("db_path", type=Path, help="Path to SQLite database to write")
    args = parser.parse_args()

    seen, inserted = load_active_energy(args.xml_path, args.db_path)
    skipped = seen - inserted
    print(
        f"Saw {seen:,} active-energy records; "
        f"inserted {inserted:,} new, skipped {skipped:,} already present."
    )


if __name__ == "__main__":
    main()
