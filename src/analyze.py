"""Plot daily metrics from the health_db SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
import tomllib
from contextlib import closing
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import polars as pl
import seaborn as sns
import statsmodels.api as sm
from matplotlib.axes import Axes
from matplotlib.colors import ListedColormap

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
) -> pl.DataFrame:
    """Read a daily view, reindex to calendar days, attach a rolling mean.

    The reindex makes the rolling window count *calendar* days, not measurement
    rows -- so a `window`-day mean spans actual time, and `min_periods` is the
    minimum number of days in that window that must carry a reading for the
    mean to be emitted (rather than null).

    Returns a frame with a `day` (Date) column, the raw `value_col`, and
    `{value_col}_smooth`, sorted ascending by day.
    """
    # view/value_col are hardcoded by callers, not user input -- f-string is safe.
    query = f"SELECT day, {value_col} FROM {view} ORDER BY day"
    with closing(sqlite3.connect(db_path)) as conn:
        df = pl.read_database(query, conn).with_columns(
            pl.col("day").str.to_date()
        )

    # polars has no row index, so the pandas asfreq("D") calendar reindex becomes
    # an explicit left join of the data onto a gap-free daily date range.
    df = _reindex_daily(df).sort("day")

    # center=True + min_samples mirrors pandas' centered rolling mean: callers
    # use odd windows (1/3/7) so centering is symmetric and unambiguous, and
    # min_samples is the count of non-null readings required in-window.
    df = df.with_columns(
        pl.col(value_col)
        .rolling_mean(window_size=window, min_samples=min_periods, center=True)
        .alias(f"{value_col}_smooth")
    )
    return df


def _reindex_daily(df: pl.DataFrame) -> pl.DataFrame:
    """Left-join `df` onto a gap-free daily calendar spanning its day range.

    Replaces pandas' `set_index("day").asfreq("D")`: emits one row per calendar
    day between the first and last present day, with nulls where data is absent.
    An empty frame is returned unchanged.
    """
    if df.height == 0:
        return df
    lo, hi = df["day"].min(), df["day"].max()
    calendar = pl.DataFrame(
        {"day": pl.date_range(lo, hi, interval="1d", eager=True)}
    )
    return calendar.join(df, on="day", how="left")


def plot_daily(
    ax: Axes,
    df: pl.DataFrame,
    value_col: str,
    ylabel: str,
    title: str,
    smooth_label: str,
) -> None:
    """Plot raw daily values plus a rolling-mean overlay on `ax`."""
    # seaborn consumes pandas/array-likes, so convert at the plotting boundary.
    pdf = df.to_pandas()
    sns.lineplot(
        data=pdf, x="day", y=value_col, ax=ax,
        color="tab:blue", alpha=0.4, linewidth=0.6, label="Daily",
    )
    sns.lineplot(
        data=pdf, x="day", y=f"{value_col}_smooth", ax=ax,
        color="tab:orange", linewidth=1.5, label=smooth_label,
    )
    ax.set_xlabel("Date")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(frameon=False)


def diagnose_missing_data(df: pl.DataFrame) -> None:
    """Print per-column null diagnostics and plot a time x column missingness map.

    Expects a `day` (Date) column plus one column per source metric; `day`
    itself is treated as the time axis and excluded from the diagnostics.
    """
    days = df["day"]
    value_cols = [c for c in df.columns if c != "day"]
    n_rows = df.height

    # first/last day carrying a real reading, per column (pandas first/last_valid).
    rows = []
    for c in value_cols:
        present = df.select(pl.col(c).is_not_null()).to_series()
        n_missing = int((~present).sum())
        n_present = int(present.sum())
        valid_days = days.filter(present)
        first_valid = valid_days.min() if n_present else None
        last_valid = valid_days.max() if n_present else None
        rows.append({
            "column":       c,
            "n_missing":    n_missing,
            "n_present":    n_present,
            "frac_missing": n_missing / n_rows if n_rows else float("nan"),
            "first_valid":  first_valid,
            "last_valid":   last_valid,
        })
    diagnostics = pl.DataFrame(rows)
    print(diagnostics)

    # Missingness matrix: rows are source columns, columns are days (time L->R).
    miss = (
        df.select(value_cols)
        .select(pl.all().is_null().cast(pl.Int8))
        .to_pandas()
        .T
    )
    fig, ax = plt.subplots(figsize=(12, 1.5 + 0.5 * len(value_cols)))
    sns.heatmap(
        miss,
        ax=ax,
        cmap=ListedColormap(["#eeeeee", "#d62728"]),
        cbar=False,
        xticklabels=False,
    )

    # One tick per month (first-of-month days).
    day_list = days.to_list()
    month_starts = [
        i for i, d in enumerate(day_list) if d is not None and d.day == 1
    ]
    ax.set_xticks([m + 0.5 for m in month_starts])
    ax.set_xticklabels(
        [day_list[i].strftime("%Y-%m") for i in month_starts],
        rotation=45, ha="right",
    )

    ax.set_xlabel("Date")
    ax.set_ylabel("")
    ax.set_title("Missing data over time (red = missing)")
    fig.tight_layout()
    # plt.show()


def prepare_energy_frame(df: pl.DataFrame) -> pl.Series:
    """Print missing-data diagnostics; return time-interpolated weight.

    Basal expenditure needs a weight every day to feed the cumulative surplus,
    so this returns a gap-filled copy for that purpose only. The original
    df["weight"] is left untouched, so the fits use just the *observed* weigh-ins
    as their regression target rather than fabricated interpolated points (#10).
    Intake is deliberately not imputed: unlogged days are modeled in the fit.

    Expects a `day` (Date) column and a `weight` column. Returns a `weight`
    Series aligned row-for-row to `df`.
    """
    diagnose_missing_data(df)
    return _interpolate_time(df["day"], df["weight"])


def _interpolate_time(days: pl.Series, values: pl.Series) -> pl.Series:
    """Time-weighted linear interpolation of `values` over the `days` axis.

    polars has no `interpolate(method="time")`, so we implement pandas' behavior
    explicitly: each interior gap is filled by linear interpolation weighted by
    the actual elapsed time between the bracketing non-null readings (here the
    axis is daily, so the time weights equal day counts, but we compute them from
    `days` so irregular spacing is handled correctly). Leading nulls (before the
    first reading) stay null, matching pandas; trailing nulls are forward-filled
    by pandas' time method and we replicate that. Returns a Series named like the
    input, aligned row-for-row.
    """
    n = values.len()
    name = values.name
    if n == 0:
        return values

    day_ordinals = [d.toordinal() if d is not None else None
                    for d in days.to_list()]
    vals = values.to_list()
    out: list[float | None] = list(vals)

    # Index/value/time of the most recent non-null reading seen so far.
    prev_i: int | None = None
    for i in range(n):
        if vals[i] is not None:
            if prev_i is not None and i - prev_i > 1:
                t0, t1 = day_ordinals[prev_i], day_ordinals[i]
                v0, v1 = vals[prev_i], vals[i]
                span = t1 - t0
                for j in range(prev_i + 1, i):
                    frac = (day_ordinals[j] - t0) / span
                    out[j] = v0 + (v1 - v0) * frac
            prev_i = i

    # Trailing nulls: pandas' time interpolation forward-fills past the last
    # reading. Leading nulls (before the first reading) are left as null.
    if prev_i is not None:
        for j in range(prev_i + 1, n):
            out[j] = vals[prev_i]

    return pl.Series(name, out)


def bmr_mifflin_st_jeor(
    weight_kg: float | pl.Series,
    height_cm: float,
    age_yr: float,
    sex: str = "m",
) -> float | pl.Series:
    """Mifflin-St Jeor basal metabolic rate, kcal/day. Vectorizes over a Series."""
    offset = 5 if sex.lower().startswith("m") else -161
    return 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_yr + offset


def fit_unlogged_intake(df: pl.DataFrame) -> dict:
    """Fit weight ~ cumulative surplus, estimating mean intake on unlogged days.

    Daily energy surplus is intake - expenditure. On logged days it is known; on
    unlogged days intake is an unknown constant mu, so the cumulative surplus
    through day t splits into a known part A_t (unlogged intake counted as 0) plus
    mu times B_t, the running count of unlogged days:

        weight_t ~ w0 + k*A_t + (k*mu)*B_t

    Regressing weight on A and B therefore identifies k (kg per kcal) and
    mu = coef_B / coef_A, the implied average intake on unlogged days. This
    replaces the old assumption that unlogged days sat exactly at maintenance.

    Expects columns `weight` (observed-only; gap days are null), `A`, `B`. Returns
    the fitted model plus k, mu, and a delta-method standard error for mu. Rows
    with any null are dropped, so the fit uses only real weigh-ins (#10) and
    `n_obs` flags how many days actually entered the fit.

    Standard errors use a Newey-West (HAC) covariance: the predictors are
    cumulative sums, so residuals are strongly autocorrelated and classical OLS
    errors would be far too small. The
    standard 4*(n/100)^(2/9) rule yields too short a bandwidth here (~4 days),
    well before the SE stabilizes, so we floor it at 30 days. This is a stopgap:
    because the regressors are non-stationary cumulative sums, even HAC errors
    are not fully trustworthy -- a first-difference reformulation (#13) is the
    real fix.
    """
    # Drop gap days then hand numpy arrays to statsmodels (pandas/numpy-centric).
    fit_df = df.select(["weight", "A", "B"]).drop_nulls()
    y = fit_df["weight"].to_numpy()
    x = sm.add_constant(fit_df.select(["A", "B"]).to_numpy())  # cols: const, A, B
    max_lags = max(30, int(4.0 * (fit_df.height / 100.0) ** (2.0 / 9.0)))
    model = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": max_lags})

    # Positional params/cov: index 0=const, 1=A, 2=B.
    k, coef_b = model.params[1], model.params[2]
    mu = coef_b / k

    # Delta method for the ratio mu = coef_B / coef_A.
    cov = model.cov_params()
    var_mu = (
        cov[2, 2] / k**2
        + coef_b**2 * cov[1, 1] / k**4
        - 2.0 * coef_b * cov[1, 2] / k**3
    )
    return {
        "model": model,
        "k": k,
        "mu": mu,
        "se_mu": float(var_mu) ** 0.5,
        "n_obs": int(model.nobs),
    }


def fit_unlogged_intake_differenced(df: pl.DataFrame, period_days: int = 7) -> dict:
    """Block-difference counterpart to `fit_unlogged_intake`.

    Differencing the levels model weight_t ~ w0 + k*A_t + (k*mu)*B_t gives a
    regression of weight *change* on energy balance, with the same identity
    mu = coef_dB / coef_dA. The win over the levels fit is that the differenced
    regressors are stationary, escaping its cointegration/spurious-regression
    regime so the standard errors are genuinely valid (#13).

    The catch is signal-to-noise: a single day's fat change (~surplus/7700 kg)
    is dwarfed by day-to-day water-weight swings, so a *daily* difference is pure
    noise (and even flips the sign of k). We therefore aggregate to
    non-overlapping `period_days` blocks -- accumulated surplus over a block
    rises above the noise floor:

        dW_i ~ c + k*dA_i + (k*mu)*dB_i      (i indexes blocks)

    Each block is summarized by its *mean* over the block. Because the model is
    linear, averaging both sides is consistent, and using the mean keeps weight
    and the A/B predictors aligned to the same (mid-block) time center -- a
    .last()/.mean() mismatch otherwise corrupts the fit. Block weight is the mean
    of that block's *observed weigh-ins* (no interpolated days, per #10), which
    also averages down measurement noise. Blocks with no weigh-in are dropped, so
    a difference spans from one populated block to the next. Non-overlapping
    blocks make residuals roughly independent; a small HAC bandwidth absorbs the
    mild autocorrelation from shared block endpoints.

    Expects columns `day` (Date), `weight` (observed, gaps allowed), `A`, `B`.
    Returns the model, k, mu, a delta-method SE for mu, n_obs, and the
    block-level frame.
    """
    # Anchor the block grid to the first day so blocks match pandas' resample,
    # which buckets relative to the series origin.
    origin = df["day"].min()
    blocks = (
        df.with_columns(
            ((pl.col("day") - origin).dt.total_days() // period_days)
            .alias("_block")
        )
        .group_by("_block")
        .agg(
            pl.col("weight").mean().alias("weight"),
            pl.col("A").mean().alias("A"),
            pl.col("B").mean().alias("B"),
        )
        .drop_nulls(subset=["weight"])
        .sort("_block")
    )
    # First differences between consecutive populated blocks.
    d = (
        blocks.select(
            pl.col("weight").diff().alias("dW"),
            pl.col("A").diff().alias("dA"),
            pl.col("B").diff().alias("dB"),
        )
        .drop_nulls()
    )

    y = d["dW"].to_numpy()
    x = sm.add_constant(d.select(["dA", "dB"]).to_numpy())  # cols: const, dA, dB
    model = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": 1})

    k, coef_b = model.params[1], model.params[2]
    mu = coef_b / k

    # Delta method for the ratio mu = coef_dB / coef_dA.
    cov = model.cov_params()
    var_mu = (
        cov[2, 2] / k**2
        + coef_b**2 * cov[1, 1] / k**4
        - 2.0 * coef_b * cov[1, 2] / k**3
    )
    return {
        "model": model,
        "k": k,
        "mu": mu,
        "se_mu": float(var_mu) ** 0.5,
        "n_obs": int(model.nobs),
        "frame": d,
    }


def plot_intake_weight_correlations(
    db_path, first_day: str = "2025-10-10", include_differenced: bool = False,
) -> None:
    """Fit and plot the unlogged-intake model from the daily views.

    Reports the levels fit (`fit_unlogged_intake`) over observed weigh-ins only.
    The block-differenced fit is experimental and data-gated -- pass
    `include_differenced=True` to also run it (see `_plot_differenced_fit`).
    """
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

    # Join the four metrics on `day` (outer, full union of dates) -- the polars
    # equivalent of pandas' index-aligned DataFrame({...}) constructor.
    base_df = (
        calories_consumed.select("day", pl.col("kcal").alias("intake"))
        .join(active_energy.select("day", pl.col("kcal").alias("active")),
              on="day", how="full", coalesce=True)
        .join(basal_energy.select("day", pl.col("kcal").alias("basal")),
              on="day", how="full", coalesce=True)
        .join(weight.select("day", pl.col("kg").alias("weight")),
              on="day", how="full", coalesce=True)
        .sort("day")
    )

    # Plot recorded intake
    _, ax = plt.subplots(figsize=(8, 4))
    sns.histplot(base_df.to_pandas(), x="intake", bins=30, kde=True,
                 color="tab:blue", edgecolor="white", ax=ax)
    ax.set_xlabel("Daily intake (kcal)")
    ax.set_ylabel("Days")
    ax.set_title("Distribution of daily calorie intake")
    # plt.show()

    # Filter to first day. Capture which days have a real log *before* touching
    # the frame -- the indicator drives the unlogged-intake fit below.
    first = date.fromisoformat(first_day)
    filtered = base_df.filter(pl.col("day") >= first).sort("day")
    intake_logged = filtered["intake"].is_not_null()
    # Interpolated weight feeds basal/expenditure (needed every day); filtered's
    # own "weight" stays observed-only so the fits never treat a filled-in day as
    # a real measurement (#10).
    weight_interp = prepare_energy_frame(filtered)
    filtered = filtered.with_columns(
        bmr_mifflin_st_jeor(weight_interp, HEIGHT_CM, AGE_YR, SEX)
        .alias("basal_msj")
    )

    # Predictors for fit_unlogged_intake (surplus convention):
    #   A = cumulative known surplus, counting unlogged-day intake as 0
    #   B = running count of unlogged days
    # `active` comes from a calendar-day reindex, so a day with no active-energy
    # record is null. cum_sum propagates null, which would turn A into null from
    # the first gap onward and silently drop the entire tail of the series in the
    # fit. Zero-fill instead: a day with no recorded active energy is treated as
    # zero active expenditure, keeping the cumulative sum intact (#9). Surface any
    # such gaps so the imputation is visible rather than silent.
    active_gaps = int(filtered["active"].is_null().sum())
    if active_gaps:
        print(f"Warning: {active_gaps} in-range day(s) lack an active-energy "
              "record; treating as 0 active kcal for the cumulative surplus (#9).")
    filtered = filtered.with_columns(
        (
            (pl.col("intake").fill_null(0.0)
             - (pl.col("basal_msj") + pl.col("active").fill_null(0.0))).cum_sum()
        ).alias("A"),
        (
            pl.col("intake").is_null().cast(pl.Float64).cum_sum()
        ).alias("B"),
    )

    fit = fit_unlogged_intake(filtered)
    model, k, mu, se_mu = fit["model"], fit["k"], fit["mu"], fit["se_mu"]
    logged_mean = base_df["intake"].drop_nulls().mean()

    n_logged = int(intake_logged.sum())
    n_unlogged = int((~intake_logged).sum())
    print(f"Days: {n_logged} logged, {n_unlogged} unlogged "
          f"({fit['n_obs']} weigh-ins entered the fit)")
    print(f"Mean basal:  {filtered['basal'].mean():.0f} kcal/day")
    print(f"Mean MSJ basal:  {filtered['basal_msj'].mean():.0f} kcal/day")
    print(f"Mean active: {filtered['active'].mean():.0f} kcal/day")
    print(f"Mean logged intake:   {logged_mean:.0f} kcal/day")
    print(f"Est. unlogged intake: {mu:.0f} +/- {se_mu:.0f} kcal/day")
    print(f"Energy per kg: {1.0 / k:.0f} kcal/kg (R^2 = {model.rsquared:.3f})")

    # Plot weight against the fitted cumulative surplus (A + mu*B): by
    # construction the model is weight ~ const + k*(A + mu*B), so the fitted
    # line has intercept `const` and slope `k`.
    filtered = filtered.with_columns(
        (pl.col("A") + mu * pl.col("B")).alias("cumulative_surplus")
    )
    label = (
        f"{1.0 / k:.0f} kcal/kg  (k = {k:.2e} kg/kcal)\n"
        f"unlogged intake = {mu:.0f} +/- {se_mu:.0f} kcal/day\n"
        f"R² = {model.rsquared:.3f}"
    )

    _, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(
        data=filtered.to_pandas(),
        x="cumulative_surplus",
        y="weight",
        ax=ax,
        alpha=0.5,
        s=12,
    )
    ax.axline(
        (0, model.params[0]),
        slope=k,
        color="tab:orange",
        linewidth=1.5,
        label=label,
    )
    ax.set_xlabel("Cumulative energy surplus (kcal)")
    ax.set_ylabel("Body mass (kg)")
    ax.legend(frameon=False, loc="best")

    if include_differenced:
        _plot_differenced_fit(filtered)


def _plot_differenced_fit(filtered: pl.DataFrame) -> None:
    """Experimental block-differenced fit (see fit_unlogged_intake_differenced).

    Off by default: at the current weigh-in density the estimate is unstable --
    the sign of k flips with block size -- so it is gated behind
    `include_differenced` until more data accumulates (#16). The machinery is
    correct; only the data is insufficient, which is why we don't report it as a
    headline result alongside the levels fit.
    """
    diff_fit = fit_unlogged_intake_differenced(filtered)
    d_k, d_mu, d_se = diff_fit["k"], diff_fit["mu"], diff_fit["se_mu"]
    print("\n[experimental] differenced fit (data-limited, unstable -- see #16):")
    print(f"  unlogged intake {d_mu:.0f} +/- {d_se:.0f} kcal/day, "
          f"{1.0 / d_k:.0f} kcal/kg, R^2 {diff_fit['model'].rsquared:.3f}")

    # Per-block weight change vs the fitted block balance (dA + mu*dB).
    diff_df = diff_fit["frame"].with_columns(
        (pl.col("dA") + d_mu * pl.col("dB")).alias("balance")
    )
    d_label = (
        f"{1.0 / d_k:.0f} kcal/kg  (k = {d_k:.2e} kg/kcal)\n"
        f"unlogged intake = {d_mu:.0f} +/- {d_se:.0f} kcal/day\n"
        f"R² = {diff_fit['model'].rsquared:.3f}  [experimental]"
    )

    _, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(data=diff_df.to_pandas(), x="balance", y="dW", ax=ax,
                    alpha=0.6, s=24)
    ax.axline(
        (0, diff_fit["model"].params[0]),
        slope=d_k,
        color="tab:orange",
        linewidth=1.5,
        label=d_label,
    )
    ax.set_xlabel("Block energy balance (kcal)")
    ax.set_ylabel("Block body-mass change (kg)")
    ax.legend(frameon=False, loc="best")


def plot_active_intake_correlations(db_path) -> None:
    pass


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
    plot_active_intake_correlations(args.db)

    plt.show()


if __name__ == "__main__":
    main()
