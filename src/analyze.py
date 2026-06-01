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


def prepare_energy_frame(df: pd.DataFrame) -> pd.Series:
    """Print missing-data diagnostics; return time-interpolated weight.

    Basal expenditure needs a weight every day to feed the cumulative surplus,
    so this returns a gap-filled copy for that purpose only. The original
    df["weight"] is left untouched, so the fits use just the *observed* weigh-ins
    as their regression target rather than fabricated interpolated points (#10).
    Intake is deliberately not imputed: unlogged days are modeled in the fit.
    """
    diagnose_missing_data(df)
    return df["weight"].interpolate(method="time")


def bmr_mifflin_st_jeor(
    weight_kg: float | pd.Series,
    height_cm: float,
    age_yr: float,
    sex: str = "m",
) -> float | pd.Series:
    """Mifflin-St Jeor basal metabolic rate, kcal/day. Vectorizes over a Series."""
    offset = 5 if sex.lower().startswith("m") else -161
    return 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_yr + offset


def fit_unlogged_intake(df: pd.DataFrame) -> dict:
    """Fit weight ~ cumulative surplus, estimating mean intake on unlogged days.

    Daily energy surplus is intake - expenditure. On logged days it is known; on
    unlogged days intake is an unknown constant mu, so the cumulative surplus
    through day t splits into a known part A_t (unlogged intake counted as 0) plus
    mu times B_t, the running count of unlogged days:

        weight_t ~ w0 + k*A_t + (k*mu)*B_t

    Regressing weight on A and B therefore identifies k (kg per kcal) and
    mu = coef_B / coef_A, the implied average intake on unlogged days. This
    replaces the old assumption that unlogged days sat exactly at maintenance.

    Expects columns `weight` (observed-only; gap days are NaN), `A`, `B`. Returns
    the fitted model plus k, mu, and a delta-method standard error for mu. Rows
    with any NaN are dropped, so the fit uses only real weigh-ins (#10) and
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
    fit_df = df[["weight", "A", "B"]].dropna()
    max_lags = max(30, int(4.0 * (len(fit_df) / 100.0) ** (2.0 / 9.0)))
    model = sm.OLS(fit_df["weight"], sm.add_constant(fit_df[["A", "B"]])).fit(
        cov_type="HAC", cov_kwds={"maxlags": max_lags})

    k, coef_b = model.params["A"], model.params["B"]
    mu = coef_b / k

    # Delta method for the ratio mu = coef_B / coef_A.
    cov = model.cov_params()
    var_mu = (
        cov.loc["B", "B"] / k**2
        + coef_b**2 * cov.loc["A", "A"] / k**4
        - 2.0 * coef_b * cov.loc["A", "B"] / k**3
    )
    return {
        "model": model,
        "k": k,
        "mu": mu,
        "se_mu": float(var_mu) ** 0.5,
        "n_obs": int(model.nobs),
    }


def fit_unlogged_intake_differenced(df: pd.DataFrame, period_days: int = 7) -> dict:
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

    Expects columns `weight` (observed, gaps allowed), `A`, `B` on a daily
    DatetimeIndex. Returns the model, k, mu, a delta-method SE for mu, n_obs, and
    the block-level frame.
    """
    rule = f"{period_days}D"
    blocks = pd.DataFrame({
        "weight": df["weight"].resample(rule).mean(),
        "A": df["A"].resample(rule).mean(),
        "B": df["B"].resample(rule).mean(),
    }).dropna(subset=["weight"])
    d = pd.DataFrame({
        "dW": blocks["weight"].diff(),
        "dA": blocks["A"].diff(),
        "dB": blocks["B"].diff(),
    }).dropna()
    model = sm.OLS(d["dW"], sm.add_constant(d[["dA", "dB"]])).fit(
        cov_type="HAC", cov_kwds={"maxlags": 1})

    k, coef_b = model.params["dA"], model.params["dB"]
    mu = coef_b / k

    # Delta method for the ratio mu = coef_dB / coef_dA.
    cov = model.cov_params()
    var_mu = (
        cov.loc["dB", "dB"] / k**2
        + coef_b**2 * cov.loc["dA", "dA"] / k**4
        - 2.0 * coef_b * cov.loc["dA", "dB"] / k**3
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

    # Filter to first day. Capture which days have a real log *before* touching
    # the frame -- the indicator drives the unlogged-intake fit below.
    filtered = base_df.loc[base_df.index >= first_day].copy()
    intake_logged = filtered["intake"].notna()
    # Interpolated weight feeds basal/expenditure (needed every day); filtered's
    # own "weight" stays observed-only so the fits never treat a filled-in day as
    # a real measurement (#10).
    weight_interp = prepare_energy_frame(filtered)
    filtered["basal_msj"] = bmr_mifflin_st_jeor(
        weight_interp, HEIGHT_CM, AGE_YR, SEX)

    # Predictors for fit_unlogged_intake (surplus convention):
    #   A = cumulative known surplus, counting unlogged-day intake as 0
    #   B = running count of unlogged days
    expenditure = filtered["basal_msj"] + filtered["active"]
    filtered["A"] = (filtered["intake"].fillna(0.0) - expenditure).cumsum()
    filtered["B"] = (~intake_logged).astype(float).cumsum()

    fit = fit_unlogged_intake(filtered)
    model, k, mu, se_mu = fit["model"], fit["k"], fit["mu"], fit["se_mu"]
    logged_mean = base_df["intake"].dropna().mean()

    print(f"Days: {int(intake_logged.sum())} logged, "
          f"{int((~intake_logged).sum())} unlogged "
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
    filtered["cumulative_surplus"] = filtered["A"] + mu * filtered["B"]
    label = (
        f"{1.0 / k:.0f} kcal/kg  (k = {k:.2e} kg/kcal)\n"
        f"unlogged intake = {mu:.0f} +/- {se_mu:.0f} kcal/day\n"
        f"R² = {model.rsquared:.3f}"
    )

    _, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(
        data=filtered,
        x="cumulative_surplus",
        y="weight",
        ax=ax,
        alpha=0.5,
        s=12,
    )
    ax.axline(
        (0, model.params["const"]),
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


def _plot_differenced_fit(filtered: pd.DataFrame) -> None:
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
    diff_df = diff_fit["frame"].assign(
        balance=lambda r: r["dA"] + d_mu * r["dB"])
    d_label = (
        f"{1.0 / d_k:.0f} kcal/kg  (k = {d_k:.2e} kg/kcal)\n"
        f"unlogged intake = {d_mu:.0f} +/- {d_se:.0f} kcal/day\n"
        f"R² = {diff_fit['model'].rsquared:.3f}  [experimental]"
    )

    _, ax = plt.subplots(figsize=(8, 6))
    sns.scatterplot(data=diff_df, x="balance", y="dW", ax=ax, alpha=0.6, s=24)
    ax.axline(
        (0, diff_fit["model"].params["const"]),
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
