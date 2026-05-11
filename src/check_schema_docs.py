"""Verify the README Schema section names every table and view in sql/.

Exits 0 if in sync, 1 with a diff if drift. Run after schema changes, or
wire into pre-commit / CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "sql" / "schema.sql"
VIEWS_SQL = ROOT / "sql" / "views.sql"
README = ROOT / "README.md"
SCHEMA_SECTION = "## Schema"

CREATE_RE = re.compile(
    r"CREATE\s+(TABLE|VIEW)(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)", re.IGNORECASE
)
# Matches "    table_name {" inside a Mermaid erDiagram block.
MERMAID_TABLE_RE = re.compile(r"^\s{4}(\w+)\s*\{", re.MULTILINE)


def schema_objects() -> dict[str, str]:
    """Return {name: kind} for every CREATE TABLE/VIEW in sql/."""
    text = SCHEMA_SQL.read_text() + "\n" + VIEWS_SQL.read_text()
    return {name: kind.lower() for kind, name in CREATE_RE.findall(text)}


def readme_section() -> str:
    """Return the body of the Schema section, or '' if missing."""
    text = README.read_text()
    start = text.find(SCHEMA_SECTION)
    if start == -1:
        return ""
    rest = text[start + len(SCHEMA_SECTION):]
    next_section = re.search(r"\n##\s", rest)
    return rest[: next_section.start()] if next_section else rest


def main() -> int:
    objs = schema_objects()
    section = readme_section()
    if not section:
        print(
            f"ERROR: '{SCHEMA_SECTION}' section not found in {README.name}",
            file=sys.stderr,
        )
        return 1

    # Forward: every defined object must be mentioned by name somewhere in
    # the section (in either the Mermaid block or the prose tree).
    undocumented = sorted(n for n in objs if n not in section)

    # Reverse: tables drawn in the Mermaid ER diagram must actually exist.
    mermaid_tables = set(MERMAID_TABLE_RE.findall(section))
    stale = sorted(t for t in mermaid_tables if t not in objs)

    if not undocumented and not stale:
        print(f"OK: README is in sync ({len(objs)} object(s) documented).")
        return 0

    if undocumented:
        print(
            "Defined in sql/ but missing from README Schema section:",
            file=sys.stderr,
        )
        for n in undocumented:
            print(f"  - {n} ({objs[n]})", file=sys.stderr)
    if stale:
        print("Drawn in README ER diagram but not in sql/:", file=sys.stderr)
        for n in stale:
            print(f"  - {n}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
