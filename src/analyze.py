"""Plot daily metrics from the health_db SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.axes import Axes
from matplotlib.colors import ListedColormap
from scipy import stats

DEFAULT_DB = Path(__file__).resolve().parent.parent / \
    "data" / "db" / "health.db"

# Personal constants (height/age/sex) live in a gitignored config.toml so
# they don't ship to the public repo. See config.example.toml for the schema.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"
try:
    with open(_CONFIG_PATH, "rb") as _f:
        _CONFIG = tomllib.load(_f)
except FileNotFoundError as e:
    raise SystemExit(
        f"Missing {_CONFIG_PATH.name}. Copy config.example.toml to config.toml "
        "and fill in your subject info."
    ) from e

HEIGHT_CM: float = _CONFIG["subject"]["height_cm"]
AGE_YR:    float = _CONFIG["subject"]["age_yr"]
SEX:       str   = _CONFIG["subject"]["sex"]


def load_daily(
    db_path: Path,
    view: str,
    value_col: str,
    *,
    window: int,
    min_periods: int,
) -> pd.DataFrame:
    """Read a daily view, reindex to calendar days, attach a rolling mean.

    The reindex makes the rolling window count *calendar* days, not measurement
    rows -- so a `window`-day mean spans actual time, and `min_periods` is the
    minimum number of days in that window that must carry a reading for the
    mean to be emitted (rather than NaN).
    """
    # view/value_col are hardcoded by callers, not user input -- f-string is safe.
    query = f"SELECT day, {value_col} FROM {view} ORDER BY day"
    with closing(sqlite3.connect(db_path)) as conn:
        df = pd.read_sql_query(query, conn, parse_dates=["day"])
    df = df.set_index("day").asfreq("D").reset_index()
    df[f"{value_col}_smooth"] = (
        df[value_col].rolling(window, center=True,
                              min_periods=min_periods).mean()
    )
    return df


def plot_daily(
    ax: Axes,
    df: pd.DataFrame,
    value_col: str,
    ylabel: str,
    title: str,
    smooth_label: str,
) -> None:
    """Plot raw daily values plus a rolling-mean overlay on `ax`."""
    sns.lineplot(
        data=df, x="day", y=value_col, ax=ax,
        color="tab:blue", alpha=0.4, linewidth=0.6, label="Daily",
    )
    sns.lineplot(
        data=df, x="day", y=f"{value_col}_smooth", ax=ax,
        color="tab:orange", linewidth=1.5, label=smooth_label,
    )
    ax.set_xlabel("Date")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(frameon=False)


def diagnose_missing_data(df: pd.DataFrame) -> None:
    """Print per-column NaN diagnostics and plot a time x column missingness map."""
    diagnostics = pd.DataFrame({
        "n_missing":    df.isna().sum(),
        "n_present":    df.count(),
        "frac_missing": df.isna().mean(),
        "first_valid":  df.apply(lambda s: s.first_valid_index()),
        "last_valid":   df.apply(lambda s: s.last_valid_index()),
    })
    print(diagnostics)

    # Transpose so time runs left-to-right and each row is one source column.
    miss = df.isna().T.astype(int)
    fig, ax = plt.subplots(figsize=(12, 1.5 + 0.5 * len(df.columns)))
    sns.heatmap(
        miss,
        ax=ax,
        cmap=ListedColormap(["#eeeeee", "#d62728"]),
        cbar=False,
        xticklabels=False,
    )

    # One tick per month
    month_starts = [
        i for i, d in enumerate(df.index)
        if isinstance(d, pd.Timestamp) and d.day == 1
    ]
    ax.set_xticks([m + 0.5 for m in month_starts])
    ax.set_xticklabels(
        [df.index[i].strftime("%Y-%m") for i in month_starts],
        rotation=45, ha="right",
    )

    ax.set_xlabel("Date")
    ax.set_ylabel("")
    ax.set_title("Missing data over time (red = missing)")
    fig.tight_layout()
    # plt.show()


def impute_energy(df: pd.DataFrame) -> None:
    print("Diagnostics before imputation:")
    diagnose_missing_data(df)

    df["weight"] = df["weight"].interpolate(method="time")
    # df["intake"] = df["intake"].fillna(df["intake"].mean())

    basal = bmr_mifflin_st_jeor(df["weight"], HEIGHT_CM, AGE_YR, SEX)
    maintenance = basal + df["active"]
    df["intake"] = df["intake"].fillna(maintenance)

    print("\n Diagnostics after imputation:")
    diagnose_missing_data(df)


def bmr_mifflin_st_jeor(
    weight_kg: float | pd.Series,
    height_cm: float,
    age_yr: float,
    sex: str = "m",
) -> float | pd.Series:
    """Mifflin-St Jeor basal metabolic rate, kcal/day. Vectorizes over a Series."""
    offset = 5 if sex.lower().startswith("m") else -161
    return 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_yr + offset


def plot_intake_weight_correlations(db_path, first_day: str = "2025-10-10") -> None:
    sns.set_theme(style="whitegrid")

    active_energy = load_daily(
        db_path, "daily_active_energy_canonical", "kcal", window=1, min_periods=1)
    basal_energy = load_daily(
        db_path, "daily_basal_energy_canonical", "kcal", window=1, min_periods=1)
    calories_consumed = load_daily(
        db_path, "daily_calories_consumed_canonical", "kcal", window=1, min_periods=1)
    weight = load_daily(
        db_path, "daily_body_mass_canonical", "kg", window=1, min_periods=1
    )

    base_df = pd.DataFrame({
        "intake": calories_consumed.set_index("day")["kcal"],
        "active": active_energy.set_index("day")["kcal"],
        "basal": basal_energy.set_index("day")["kcal"],
        "weight": weight.set_index("day")["kg"]
    })

    # Plot recorded intake
    _, ax = plt.subplots(figsize=(8, 4))
    sns.histplot(base_df, x="intake", bins=30, kde=True,
                 color="tab:blue", edgecolor="white", ax=ax)
    ax.set_xlabel("Daily intake (kcal)")
    ax.set_ylabel("Days")
    ax.set_title("Distribution of daily calorie intake")
    # plt.show()

    # Filter to first day
    filtered = base_df.loc[base_df.index >= first_day].copy()
    impute_energy(filtered)
    filtered["basal_msj"] = bmr_mifflin_st_jeor(
        filtered["weight"], HEIGHT_CM, AGE_YR, SEX)

    # Compute cumulative sum to compare against weight
    filtered["energy_excess"] = filtered["intake"] - \
        filtered["basal_msj"] - filtered["active"]
    filtered["cumulative_excess"] = filtered["energy_excess"].cumsum()

    print(f"Mean excess per day: {filtered["energy_excess"].mean():.2f} kcal")

    print(f"Mean basal:  {filtered['basal'].mean():.0f} kcal/day")
    print(f"Mean MSJ basal:  {filtered['basal_msj'].mean():.0f} kcal/day")
    print(f"Mean active: {filtered['active'].mean():.0f} kcal/day")
    print(
        f"Mean total expenditure: {(filtered['basal']+filtered['active']).mean():.0f} kcal/day")
    print(
        f"Mean total expenditure (MSJ basal): {(filtered['basal_msj']+filtered['active']).mean():.0f} kcal/day")
    print(
        f"Mean logged intake: {base_df['intake'].dropna().mean():.0f} kcal/day")

    # Compare the cumulative sum to weight
    fit_df = filtered[["cumulative_excess", "weight"]].dropna()
    # type: ignore[arg-type]
    fit = stats.linregress(fit_df["cumulative_excess"], fit_df["weight"])
    label = (
        # type: ignore[attr-defined]
        f"slope = {fit.slope:.2e} kg/kcal ({1.0/fit.slope:.1f} kcal/kg)\n"
        f"intercept = {fit.intercept:.2f} kg\n"  # type: ignore[attr-defined]
        f"R² = {fit.rvalue ** 2:.3f}"  # type: ignore[attr-defined]
    )

    _, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(
        data=filtered,
        x="cumulative_excess",
        y="weight",
        ax=ax,
        alpha=0.5,
        s=12,
    )
    ax.axline(
        (0, fit.intercept),  # type: ignore[attr-defined]
        slope=fit.slope,  # type: ignore[attr-defined]
        color="tab:orange",
        linewidth=1.5,
        label=label,
    )
    ax.set_xlabel("Cumulative deficit (kcal)")
    ax.set_ylabel("Body mass (kg)")
    ax.legend(frameon=False, loc="best")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="SQLite database path")
    parser.add_argument("--output", type=Path,
                        help="Save figure to this path instead of showing")
    args = parser.parse_args()

    sns.set_theme(style="whitegrid")
    active_energy = load_daily(
        args.db, "daily_active_energy_canonical", "kcal",
        window=7, min_periods=1,
    )
    consumed_energy = load_daily(
        args.db, "daily_calories_consumed_canonical", "kcal",
        window=3, min_periods=2
    )
    # Body mass is sparser; require >=2 of the 3 days to have a reading so the
    # rolling line disappears during measurement droughts rather than smoothing
    # a straight line across them.
    weight = load_daily(
        args.db, "daily_body_mass_canonical", "kg",
        window=3, min_periods=2,
    )

    fig, (ax_e, ax_w, ax_c) = plt.subplots(3, 1, figsize=(15, 7), sharex=True)
    plot_daily(ax_e, active_energy, "kcal", "Active energy (kcal)",
               "Active energy per day", "7-day rolling mean")
    plot_daily(ax_w, weight, "kg", "Body mass (kg)",
               "Body mass per day", "3-day rolling mean")
    plot_daily(ax_c, consumed_energy, "kcal", "Energy consumed (kcal)",
               "Energy consumed per day", "3-day rolling mean")
    fig.tight_layout()

    if args.output:
        fig.savefig(args.output, dpi=120)
        print(f"Saved {args.output}")
    else:
        # plt.show()
        pass

    plot_intake_weight_correlations(args.db)

    plt.show()


if __name__ == "__main__":
    main()
