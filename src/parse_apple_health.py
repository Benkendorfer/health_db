"""Parse Apple Health export XML into a SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterator

from lxml import etree  # type: ignore[attr-defined]

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# Apple Health type identifier -> short name stored in records.record_type.
# Extend this dict to ingest more quantity metrics (BodyMass, HeartRate, etc).
QUANTITY_TYPES: dict[str, str] = {
    "HKQuantityTypeIdentifierActiveEnergyBurned": "ActiveEnergyBurned",
    "HKQuantityTypeIdentifierBodyMass": "BodyMass",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "BasalEnergyBurned",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed": "CaloriesConsumed"
}

# Canonical unit per record_type. Values are normalized to this unit at ingest
# so the rollup views can aggregate raw `value` without mixing units, and so
# a mid-history source switch (e.g. lb -> kg scale) introduces no artificial
# discontinuity. To add a metric: name its canonical unit here and add a
# conversion family below covering every unit that metric can appear in.
CANONICAL_UNITS: dict[str, str] = {
    "ActiveEnergyBurned": "kcal",
    "BasalEnergyBurned": "kcal",
    "CaloriesConsumed": "kcal",
    "BodyMass": "kg",
}

# Multiplicative factors to the canonical unit, grouped by physical quantity.
# Each canonical unit maps to itself with factor 1.0 (so already-canonical
# values pass through unchanged). Apple Health commonly writes `Cal` to mean
# kilocalories. Unit spellings are matched case-sensitively as Apple emits
# them. Extend a family with new spellings/units as needed.
_UNIT_FACTORS: dict[str, dict[str, float]] = {
    "kcal": {  # energy -> kcal
        "kcal": 1.0,
        "Cal": 1.0,  # Apple's "Cal" == kilocalorie
        "kJ": 0.239006,
    },
    "kg": {  # mass -> kg
        "kg": 1.0,
        "lb": 0.45359237,
    },
}


def normalize_value(record_type: str, value: float, unit: str | None) -> tuple[float, str]:
    """Convert (value, unit) to the canonical unit for record_type.

    Returns (canonical_value, canonical_unit). Already-canonical values are
    returned unchanged (factor 1.0), which keeps re-ingest idempotent. Raises
    KeyError if record_type has no canonical unit, or ValueError if `unit` is
    not a recognized spelling for that type -- failing loudly beats silently
    storing a value in the wrong unit.
    """
    canonical_unit = CANONICAL_UNITS[record_type]
    factors = _UNIT_FACTORS[canonical_unit]
    if unit not in factors:
        raise ValueError(
            f"Unrecognized unit {unit!r} for {record_type} "
            f"(canonical {canonical_unit}); known: {sorted(factors)}"
        )
    return value * factors[unit], canonical_unit


def iter_records(
    xml_path: Path, record_types: set[str] | None = None
) -> Iterator[dict]:
    """Stream <Record> elements from an Apple Health export.

    If record_types is given, only records whose `type` attribute is in the
    set are yielded. Uses iterparse with element clearing so memory stays
    flat on multi-GB exports.
    """
    context = etree.iterparse(str(xml_path), events=("end",), tag="Record")
    for _, elem in context:
        if record_types is None or elem.attrib.get("type") in record_types:
            yield dict(elem.attrib)
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]


def load_records(
    xml_path: Path, db_path: Path, batch_size: int = 10_000
) -> tuple[int, int, int]:
    """Ingest all configured quantity records idempotently.

    Returns (seen, inserted, skipped_malformed). Records with a missing or
    non-numeric `value` are counted and skipped rather than aborting the run.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = (SQL_DIR / "schema.sql").read_text()
    views_sql = (SQL_DIR / "views.sql").read_text()
    insert_sql = (SQL_DIR / "insert_record.sql").read_text()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.executescript(views_sql)
        before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

        batch: list[tuple] = []
        seen = 0
        malformed = 0
        for rec in iter_records(xml_path, set(QUANTITY_TYPES)):
            seen += 1
            record_type = QUANTITY_TYPES[rec["type"]]
            try:
                value = float(rec["value"])
            except (KeyError, ValueError):
                malformed += 1
                continue
            # Normalize to the canonical unit before insert so stored data is
            # unit-consistent and the UNIQUE key (which includes value+unit)
            # stays stable across re-ingest. An unrecognized unit is a data
            # problem we want surfaced, not silently dropped, so let it raise.
            value, unit = normalize_value(record_type, value, rec.get("unit"))
            batch.append(
                (
                    record_type,
                    rec.get("sourceName"),
                    rec.get("startDate"),
                    rec.get("endDate"),
                    value,
                    unit,
                    rec.get("sourceVersion"),
                    rec.get("creationDate"),
                )
            )
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)

        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return seen, after - before, malformed
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, help="Path to export.xml")
    parser.add_argument("db_path", type=Path, help="Path to SQLite database to write")
    args = parser.parse_args()

    seen, inserted, malformed = load_records(args.xml_path, args.db_path)
    duplicates = seen - inserted - malformed
    print(
        f"Saw {seen:,} records; "
        f"inserted {inserted:,} new, "
        f"skipped {duplicates:,} already present, "
        f"skipped {malformed:,} malformed."
    )


if __name__ == "__main__":
    main()
