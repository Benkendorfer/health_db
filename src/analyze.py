"""Plot daily metrics from the health_db SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.axes import Axes

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "db" / "health.db"


def load_daily(db_path: Path, view: str, value_col: str) -> pd.DataFrame:
    """Read a daily view (`day`, `<value_col>`) and add a 7-day rolling mean."""
    # view/value_col are hardcoded callers, not user input -- f-string is safe.
    query = f"SELECT day, {value_col} FROM {view} ORDER BY day"
    with closing(sqlite3.connect(db_path)) as conn:
        df = pd.read_sql_query(query, conn, parse_dates=["day"])
    df[f"{value_col}_7d"] = df[value_col].rolling(7, center=True).mean()
    return df


def plot_daily(
    ax: Axes, df: pd.DataFrame, value_col: str, ylabel: str, title: str
) -> None:
    """Plot raw daily values + 7-day rolling mean on `ax`."""
    sns.lineplot(
        data=df, x="day", y=value_col, ax=ax,
        color="tab:blue", alpha=0.4, linewidth=0.6, label="Daily",
    )
    sns.lineplot(
        data=df, x="day", y=f"{value_col}_7d", ax=ax,
        color="tab:orange", linewidth=1.5, label="7-day rolling mean",
    )
    ax.set_xlabel("Date")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(frameon=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--output", type=Path, help="Save figure to this path instead of showing")
    args = parser.parse_args()

    sns.set_theme(style="whitegrid")
    energy = load_daily(args.db, "daily_active_energy_canonical", "kcal")
    weight = load_daily(args.db, "daily_body_mass_canonical", "kg")

    fig, (ax_e, ax_w) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    plot_daily(ax_e, energy, "kcal", "Active energy (kcal)", "Active energy per day")
    plot_daily(ax_w, weight, "kg", "Body mass (kg)", "Body mass per day")
    fig.tight_layout()

    if args.output:
        fig.savefig(args.output, dpi=120)
        print(f"Saved {args.output}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
