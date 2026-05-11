"""Plot daily active energy from the health_db SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "db" / "health.db"


def daily_active_energy(db_path: Path) -> pd.DataFrame:
    """Load the daily canonical kcal view and add a 7-day rolling mean."""
    query = "SELECT day, kcal FROM daily_active_energy_canonical ORDER BY day"
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, parse_dates=["day"])
    df["kcal_7d"] = df["kcal"].rolling(7, center=True).mean()
    return df


def plot_daily(df: pd.DataFrame, output: Path | None = None) -> None:
    """Daily kcal as a line, with a 7-day rolling mean for trend visibility."""
    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(12, 4))
    sns.lineplot(
        data=df, x="day", y="kcal",
        ax=ax, color="tab:blue", alpha=0.4, linewidth=0.6, label="Daily",
    )
    sns.lineplot(
        data=df, x="day", y="kcal_7d",
        ax=ax, color="tab:orange", linewidth=2, label="7-day rolling mean",
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("Active energy (kcal)")
    ax.set_title("Active energy per day")
    fig.tight_layout()
    if output:
        fig.savefig(output, dpi=120)
        print(f"Saved {output}")
    else:
        plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--output", type=Path, help="Save figure to this path instead of showing")
    args = parser.parse_args()

    df = daily_active_energy(args.db)
    plot_daily(df, args.output)


if __name__ == "__main__":
    main()
