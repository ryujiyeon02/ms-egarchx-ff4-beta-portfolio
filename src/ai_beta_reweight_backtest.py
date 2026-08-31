#!/usr/bin/env python3
"""Point-in-time FF4 beta portfolio with optional CLOVA Studio policy control.

The language model never chooses individual security weights. It only returns
bounded risk-penalty coefficients. A deterministic layer converts those
coefficients into long-only, fully invested weights with per-name bounds.

Every signal dated t uses observations whose timestamp is <= t and is applied
only to returns in (t, next_rebalance_date].
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PRICE_FILE = "2000_2026_코스피코스닥_수정주가_일별_비영업일제외.csv"
MCAP_FILE = "2000_2026_코스피코스닥_시가총액_일별_비영업일제외.csv"
FACTOR_FILE = "2000_2026_KOSPI200, SMB, HML, MOM_수정종가_91CD_알별_비영업일제외.csv"

BETA_WINDOW = 252
MIN_OBS = 189
RECENT_VOL_WINDOW = 63
UNIVERSE_SIZE = 300
DEFENSIVE_FRACTION = 0.20
MIN_WEIGHT_MULTIPLE = 0.50
MAX_WEIGHT_MULTIPLE = 1.75
ANNUALIZATION = 252


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        choices=("deterministic", "clova"),
        default="deterministic",
        help="Risk-penalty policy. CLOVA requires CLOVASTUDIO_API_KEY.",
    )
    parser.add_argument("--start", default=None, help="First signal date, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="Last realized date, YYYY-MM-DD")
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        help="Trailing backtest years when --start is omitted. Use 0 for full history.",
    )
    parser.add_argument(
        "--api-frequency-months",
        type=int,
        default=3,
        help="Refresh CLOVA policy every N months; weights still update monthly.",
    )
    parser.add_argument(
        "--cache",
        default="output/cache/clova_policy_decisions.json",
        help="Local CLOVA response cache. Keep this file out of git.",
    )
    return parser.parse_args()


def read_inputs(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    price = pd.read_csv(root / PRICE_FILE, index_col=0, parse_dates=True).sort_index()
    mcap = pd.read_csv(root / MCAP_FILE, index_col=0, parse_dates=True).sort_index()
    factor = pd.read_csv(root / FACTOR_FILE, index_col=0, parse_dates=True).sort_index()

    common_columns = price.columns.intersection(mcap.columns)
    price = price.loc[:, common_columns]
    mcap = mcap.loc[:, common_columns]
    return price, mcap, factor


def prepare_returns(
    price: pd.DataFrame, factor: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    stock_ret = price.pct_change(fill_method=None)
    factor_change = factor[["KOSPI200", "SMB", "HML", "MOM"]].pct_change(fill_method=None)
    rf_col = "CD(91)" if "CD(91)" in factor.columns else "91CD"
    daily_rf = ((1.0 + factor[rf_col] / 100.0) ** (1.0 / ANNUALIZATION)) - 1.0

    ff4 = factor_change.copy()
    ff4["MKT"] = ff4["KOSPI200"] - daily_rf
    ff4 = ff4[["MKT", "SMB", "HML", "MOM"]]

    common_index = stock_ret.index.intersection(ff4.index)
    stock_ret = stock_ret.loc[common_index]
    ff4 = ff4.loc[common_index]
    daily_rf = daily_rf.loc[common_index]
    benchmark_ret = factor_change.loc[common_index, "KOSPI200"]
    return stock_ret, ff4, daily_rf, benchmark_ret


def clip_within_window(values: np.ndarray, lower: float = 0.005, upper: float = 0.995) -> np.ndarray:
    clipped = values.copy()
    finite = np.isfinite(clipped)
    if finite.sum() < 10:
        return clipped
    lo, hi = np.quantile(clipped[finite], [lower, upper])
    clipped[finite] = np.clip(clipped[finite], lo, hi)
    return clipped


def fit_beta(y: np.ndarray, x: np.ndarray) -> dict[str, float] | None:
    valid = np.isfinite(y) & np.isfinite(x).all(axis=1)
    nobs = int(valid.sum())
    if nobs < MIN_OBS:
        return None

    y_valid = clip_within_window(y[valid])
    x_valid = x[valid].copy()
    for column in range(x_valid.shape[1]):
        x_valid[:, column] = clip_within_window(x_valid[:, column])

    design = np.column_stack([np.ones(nobs), x_valid])
    dof = nobs - design.shape[1]
    if dof <= 0:
        return None
    try:
        xtx_inv = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None

    params = xtx_inv @ design.T @ y_valid
    resid = y_valid - design @ params
    sigma2 = float(resid @ resid) / dof
    if not np.isfinite(sigma2) or sigma2 < 0:
        return None
    standard_error = np.sqrt(np.diag(xtx_inv) * sigma2)
    if not np.isfinite(params).all() or not np.isfinite(standard_error).all():
        return None

    return {
        "beta_mkt": float(params[1]),
        "beta_smb": float(params[2]),
        "beta_hml": float(params[3]),
        "beta_mom": float(params[4]),
        "beta_se": float(standard_error[1]),
        "beta_t": float(params[1] / standard_error[1]) if standard_error[1] > 0 else np.nan,
        "idio_vol": float(np.std(resid, ddof=1) * np.sqrt(ANNUALIZATION)),
        "nobs": nobs,
    }


def make_rebalance_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(index.to_series().groupby(index.to_period("M")).last())


def point_in_time_universe(
    signal_date: pd.Timestamp,
    price: pd.DataFrame,
    mcap: pd.DataFrame,
    first_valid_position: pd.Series,
) -> list[str]:
    if signal_date not in price.index or signal_date not in mcap.index:
        return []
    current_position = int(price.index.get_loc(signal_date))
    start_position = max(0, current_position - 59)
    recent_observations = price.iloc[start_position : current_position + 1].notna().sum()
    listing_age = current_position - first_valid_position + 1

    eligible = (
        (price.loc[signal_date] >= 1500)
        & (recent_observations >= 50)
        & (listing_age >= BETA_WINDOW)
        & mcap.loc[signal_date].notna()
    )
    eligible_names = eligible.index[eligible]
    return mcap.loc[signal_date, eligible_names].nlargest(UNIVERSE_SIZE).index.tolist()


def estimate_features(
    signal_date: pd.Timestamp,
    candidates: list[str],
    stock_ret: pd.DataFrame,
    ff4: pd.DataFrame,
    daily_rf: pd.Series,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    end_position = int(stock_ret.index.get_loc(signal_date))
    start_position = end_position - (BETA_WINDOW - 1)
    if start_position < 0:
        return pd.DataFrame(), pd.NaT, pd.NaT

    window_index = stock_ret.index[start_position : end_position + 1]
    x = ff4.loc[window_index].to_numpy(dtype=float)
    rf_values = daily_rf.loc[window_index].to_numpy(dtype=float)
    recent_index = window_index[-RECENT_VOL_WINDOW:]
    rows: dict[str, dict[str, float]] = {}

    for name in candidates:
        y = stock_ret.loc[window_index, name].to_numpy(dtype=float) - rf_values
        fitted = fit_beta(y, x)
        if fitted is None:
            continue
        recent = stock_ret.loc[recent_index, name].dropna()
        if len(recent) < 40:
            continue
        fitted["recent_vol"] = float(recent.std(ddof=1) * np.sqrt(ANNUALIZATION))
        rows[name] = fitted

    features = pd.DataFrame.from_dict(rows, orient="index")
    return features, window_index.min(), window_index.max()


def defensive_selection(features: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features
    reliable = features[features["beta_t"].abs().rank(pct=True) > 0.20].copy()
    if reliable.empty:
        return reliable

    shrunk_mkt = 0.6 * reliable["beta_mkt"] + 0.4
    shrunk_smb = 0.6 * reliable["beta_smb"]
    shrunk_hml = 0.6 * reliable["beta_hml"]
    reliable["defensive_score"] = (
        0.3 * (-shrunk_mkt).rank(pct=True)
        + 0.3 * (-shrunk_smb).rank(pct=True)
        + 0.3 * shrunk_hml.rank(pct=True)
        + 0.1 * (-reliable["beta_se"]).rank(pct=True)
    )
    cutoff = reliable["defensive_score"].quantile(1.0 - DEFENSIVE_FRACTION)
    return reliable[reliable["defensive_score"] >= cutoff].copy()


def robust_z(values: pd.Series) -> pd.Series:
    median = values.median()
    mad = (values - median).abs().median()
    scale = 1.4826 * mad
    if not np.isfinite(scale) or scale < 1e-12:
        scale = values.std(ddof=0)
    if not np.isfinite(scale) or scale < 1e-12:
        return pd.Series(0.0, index=values.index)
    return ((values - median) / scale).clip(-3.0, 3.0)


def project_box_simplex(raw: np.ndarray, lower: float, upper: float) -> np.ndarray:
    if lower * len(raw) > 1.0 or upper * len(raw) < 1.0:
        raise ValueError("Infeasible weight bounds")
    low = float(np.min(raw - upper))
    high = float(np.max(raw - lower))
    for _ in range(100):
        midpoint = 0.5 * (low + high)
        weights = np.clip(raw - midpoint, lower, upper)
        if weights.sum() > 1.0:
            low = midpoint
        else:
            high = midpoint
    weights = np.clip(raw - 0.5 * (low + high), lower, upper)
    return weights / weights.sum()


def market_context(
    signal_date: pd.Timestamp,
    benchmark_ret: pd.Series,
    selected: pd.DataFrame,
    observation_start: pd.Timestamp,
) -> dict[str, object]:
    past = benchmark_ret.loc[:signal_date].dropna()

    def trailing_return(window: int) -> float:
        values = past.tail(window)
        return float((1.0 + values).prod() - 1.0) if len(values) else 0.0

    def trailing_vol(window: int) -> float:
        values = past.tail(window)
        return float(values.std(ddof=1) * np.sqrt(ANNUALIZATION)) if len(values) > 1 else 0.0

    context: dict[str, object] = {
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "observation_start": observation_start.strftime("%Y-%m-%d"),
        "observation_end": signal_date.strftime("%Y-%m-%d"),
        "selected_count": int(len(selected)),
        "market_return_21d": trailing_return(21),
        "market_return_63d": trailing_return(63),
        "market_vol_63d": trailing_vol(63),
        "market_vol_252d": trailing_vol(252),
    }
    for column in ("beta_mkt", "beta_smb", "beta_hml", "beta_mom", "beta_se", "recent_vol"):
        quantiles = selected[column].quantile([0.1, 0.5, 0.9])
        context[f"{column}_p10"] = float(quantiles.loc[0.1])
        context[f"{column}_p50"] = float(quantiles.loc[0.5])
        context[f"{column}_p90"] = float(quantiles.loc[0.9])
    return context


def deterministic_policy(context: dict[str, object]) -> dict[str, object]:
    market_vol = float(context["market_vol_63d"])
    stress = float(np.clip((market_vol - 0.12) / 0.18, 0.0, 1.0))
    return {
        "beta_penalty": 1.0 + 0.75 * stress,
        "uncertainty_penalty": 0.50,
        "volatility_penalty": 0.50 + 0.50 * stress,
        "tilt_strength": 0.75 + 0.35 * stress,
        "rationale": "Deterministic point-in-time volatility rule",
        "policy_source": "deterministic",
    }


def validate_policy(policy: dict[str, object], source: str) -> dict[str, object]:
    bounds = {
        "beta_penalty": (0.50, 2.00),
        "uncertainty_penalty": (0.00, 1.50),
        "volatility_penalty": (0.00, 1.50),
        "tilt_strength": (0.00, 1.50),
    }
    validated: dict[str, object] = {}
    for key, (lower, upper) in bounds.items():
        value = float(policy[key])
        if not np.isfinite(value):
            raise ValueError(f"Non-finite policy value: {key}")
        validated[key] = float(np.clip(value, lower, upper))
    validated["rationale"] = str(policy.get("rationale", ""))[:300]
    validated["policy_source"] = source
    return validated


def clova_policy(
    context: dict[str, object],
    cache: dict[str, dict[str, object]],
) -> dict[str, object]:
    cache_key = str(context["signal_date"])
    if cache_key in cache:
        return validate_policy(cache[cache_key], "clova_cache")

    api_key = os.getenv("CLOVASTUDIO_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY")
    if not api_key:
        raise RuntimeError("CLOVASTUDIO_API_KEY is not set")
    model = os.getenv("CLOVASTUDIO_MODEL", "HCX-007")
    endpoint = os.getenv(
        "CLOVASTUDIO_ENDPOINT",
        f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}",
    )

    schema = {
        "type": "object",
        "properties": {
            "beta_penalty": {"type": "number", "minimum": 0.50, "maximum": 2.00},
            "uncertainty_penalty": {"type": "number", "minimum": 0.00, "maximum": 1.50},
            "volatility_penalty": {"type": "number", "minimum": 0.00, "maximum": 1.50},
            "tilt_strength": {"type": "number", "minimum": 0.00, "maximum": 1.50},
            "rationale": {"type": "string"},
        },
        "required": [
            "beta_penalty",
            "uncertainty_penalty",
            "volatility_penalty",
            "tilt_strength",
            "rationale",
        ],
    }
    body = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You control only bounded risk penalties for a long-only beta portfolio. "
                    "Use only the supplied point-in-time summary. Do not predict returns, request "
                    "future information, select securities, or output weights. Increase penalties "
                    "only when the supplied dispersion or trailing risk justifies it."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
            },
        ],
        "topP": 0.1,
        "topK": 1,
        "maxCompletionTokens": 300,
        "temperature": 0.0,
        "repetitionPenalty": 1.0,
        "seed": 20260830,
        "thinking": {"effort": "none"},
        "responseFormat": {"type": "json", "schema": schema},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"CLOVA Studio HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("CLOVA Studio connection failed") from exc

    result = response_data.get("result", response_data)
    message = result.get("message", {})
    content = message.get("content", "")
    if not isinstance(content, str):
        raise RuntimeError("Unexpected CLOVA Studio response format")
    parsed = json.loads(content)
    validated = validate_policy(parsed, "clova_api")
    cache[cache_key] = validated
    return validated


def make_weights(selected: pd.DataFrame, policy: dict[str, object]) -> pd.Series:
    beta_z = robust_z(selected["beta_mkt"])
    uncertainty_z = robust_z(selected["beta_se"])
    volatility_z = robust_z(selected["recent_vol"])
    penalties = (
        float(policy["beta_penalty"]) * beta_z
        + float(policy["uncertainty_penalty"]) * uncertainty_z
        + float(policy["volatility_penalty"]) * volatility_z
    )
    coefficient_sum = (
        float(policy["beta_penalty"])
        + float(policy["uncertainty_penalty"])
        + float(policy["volatility_penalty"])
    )
    normalized_penalty = penalties / max(coefficient_sum, 1e-12)
    raw = np.exp(-float(policy["tilt_strength"]) * normalized_penalty.to_numpy())
    raw = raw / raw.sum()

    count = len(selected)
    lower = MIN_WEIGHT_MULTIPLE / count
    upper = MAX_WEIGHT_MULTIPLE / count
    weights = project_box_simplex(raw, lower, upper)
    return pd.Series(weights, index=selected.index, name="strategy_weight")


def target_turnover(current: pd.Series, previous: pd.Series | None) -> float:
    if previous is None:
        return np.nan
    union = current.index.union(previous.index)
    return float(0.5 * (current.reindex(union, fill_value=0.0) - previous.reindex(union, fill_value=0.0)).abs().sum())


def performance_stats(returns: pd.Series, rf: pd.Series) -> dict[str, float]:
    aligned = pd.concat([returns.rename("return"), rf.rename("rf")], axis=1).dropna()
    values = aligned["return"]
    wealth = (1.0 + values).cumprod()
    years = max((values.index[-1] - values.index[0]).days / 365.25, len(values) / 12.0)
    drawdown = wealth / wealth.cummax() - 1.0
    excess = aligned["return"] - aligned["rf"]
    return {
        "CAGR": float(wealth.iloc[-1] ** (1.0 / years) - 1.0),
        "Annualized Vol": float(values.std(ddof=1) * np.sqrt(12.0)),
        "Sharpe (excess RF)": float(excess.mean() / values.std(ddof=1) * np.sqrt(12.0)),
        "Max Drawdown": float(drawdown.min()),
        "Positive Month Ratio": float((values > 0).mean()),
        "Total Return": float(wealth.iloc[-1] - 1.0),
        "Months": float(len(values)),
    }


def save_charts(
    output_dir: Path,
    returns: pd.DataFrame,
    decisions: pd.DataFrame,
) -> None:
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    strategy_columns = ["Point-in-time Equal Weight", "Beta Risk Budget", "KOSPI200"]
    cumulative = (1.0 + returns[strategy_columns]).cumprod()
    fig, ax = plt.subplots(figsize=(12, 6.2))
    cumulative.plot(ax=ax, linewidth=1.7)
    ax.set_title("Point-in-time FF4 Beta Portfolio: Cumulative Wealth")
    ax.set_ylabel("Growth of 1.0")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(chart_dir / "ai_beta_cumulative_wealth.png", dpi=180)
    plt.close(fig)

    drawdown = cumulative.div(cumulative.cummax()).sub(1.0)
    fig, ax = plt.subplots(figsize=(12, 5.4))
    drawdown.plot(ax=ax, linewidth=1.4)
    ax.set_title("Point-in-time FF4 Beta Portfolio: Drawdown")
    ax.set_ylabel("Drawdown")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(chart_dir / "ai_beta_drawdown.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    decisions[["equal_signal_beta", "strategy_signal_beta"]].plot(ax=axes[0], linewidth=1.4)
    axes[0].set_title("Signal-date Market Beta")
    axes[0].set_ylabel("Weighted beta")
    axes[0].grid(alpha=0.25)
    decisions[["equal_target_turnover", "strategy_target_turnover"]].plot(ax=axes[1], linewidth=1.2)
    axes[1].set_title("Target-weight Turnover")
    axes[1].set_ylabel("One-way turnover")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(chart_dir / "ai_beta_beta_and_turnover.png", dpi=180)
    plt.close(fig)


def run_backtest(root: Path, args: argparse.Namespace) -> None:
    output_dir = root / "output"
    table_dir = output_dir / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)

    price, mcap, factor = read_inputs(root)
    stock_ret, ff4, daily_rf, benchmark_ret = prepare_returns(price, factor)
    rebalance_dates = make_rebalance_dates(stock_ret.index)
    rebalance_dates = rebalance_dates[rebalance_dates.isin(mcap.index)]
    if args.start:
        rebalance_dates = rebalance_dates[rebalance_dates >= pd.Timestamp(args.start)]
    elif args.years > 0:
        trailing_start = rebalance_dates.max() - pd.DateOffset(years=args.years)
        rebalance_dates = rebalance_dates[rebalance_dates >= trailing_start]
    if args.end:
        rebalance_dates = rebalance_dates[rebalance_dates <= pd.Timestamp(args.end)]

    position_map = pd.Series(np.arange(len(price.index)), index=price.index)
    first_valid_date = price.apply(pd.Series.first_valid_index)
    first_valid_position = first_valid_date.map(position_map)

    cache_path = root / args.cache
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        cache = {}

    return_rows: list[dict[str, object]] = []
    decision_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    latest_weights = pd.DataFrame()
    previous_equal: pd.Series | None = None
    previous_strategy: pd.Series | None = None
    last_clova_policy: dict[str, object] | None = None
    api_frequency = max(int(args.api_frequency_months), 1)

    for position, signal_date in enumerate(rebalance_dates[:-1]):
        realized_date = rebalance_dates[position + 1]
        candidates = point_in_time_universe(signal_date, price, mcap, first_valid_position)
        features, observation_start, observation_end = estimate_features(
            signal_date, candidates, stock_ret, ff4, daily_rf
        )
        selected = defensive_selection(features)
        if len(selected) < 10:
            continue

        context = market_context(
            signal_date, benchmark_ret, selected, observation_start
        )
        policy = deterministic_policy(context)
        if args.policy == "clova":
            if last_clova_policy is None or position % api_frequency == 0:
                last_clova_policy = clova_policy(context, cache)
                policy = last_clova_policy
            else:
                policy = dict(last_clova_policy)
                policy["policy_source"] = "clova_carry"

        equal_weight = pd.Series(1.0 / len(selected), index=selected.index, name="equal_weight")
        strategy_weight = make_weights(selected, policy)
        holding_index = stock_ret.index[(stock_ret.index > signal_date) & (stock_ret.index <= realized_date)]
        if len(holding_index) == 0:
            continue
        holding_returns = stock_ret.loc[holding_index, selected.index].fillna(0.0)
        equal_daily = holding_returns.mul(equal_weight, axis=1).sum(axis=1)
        strategy_daily = holding_returns.mul(strategy_weight, axis=1).sum(axis=1)
        benchmark_daily = benchmark_ret.loc[holding_index].fillna(0.0)
        rf_daily = daily_rf.loc[holding_index].fillna(0.0)

        equal_turnover = target_turnover(equal_weight, previous_equal)
        strategy_turnover = target_turnover(strategy_weight, previous_strategy)
        equal_beta = float(equal_weight @ selected.loc[equal_weight.index, "beta_mkt"])
        strategy_beta = float(strategy_weight @ selected.loc[strategy_weight.index, "beta_mkt"])

        return_rows.append(
            {
                "signal_date": signal_date,
                "realized_date": realized_date,
                "Point-in-time Equal Weight": float((1.0 + equal_daily).prod() - 1.0),
                "Beta Risk Budget": float((1.0 + strategy_daily).prod() - 1.0),
                "KOSPI200": float((1.0 + benchmark_daily).prod() - 1.0),
                "RF": float((1.0 + rf_daily).prod() - 1.0),
            }
        )
        decision_rows.append(
            {
                "signal_date": signal_date,
                "realized_date": realized_date,
                "policy_source": policy["policy_source"],
                "selected_count": len(selected),
                "beta_penalty": policy["beta_penalty"],
                "uncertainty_penalty": policy["uncertainty_penalty"],
                "volatility_penalty": policy["volatility_penalty"],
                "tilt_strength": policy["tilt_strength"],
                "equal_signal_beta": equal_beta,
                "strategy_signal_beta": strategy_beta,
                "equal_target_turnover": equal_turnover,
                "strategy_target_turnover": strategy_turnover,
                "equal_max_weight": float(equal_weight.max()),
                "strategy_max_weight": float(strategy_weight.max()),
                "market_return_63d": context["market_return_63d"],
                "market_vol_63d": context["market_vol_63d"],
                "rationale": policy["rationale"],
            }
        )
        audit_rows.append(
            {
                "signal_date": signal_date,
                "observation_start": observation_start,
                "max_input_date": observation_end,
                "holding_start": holding_index.min(),
                "holding_end": holding_index.max(),
                "realized_date": realized_date,
                "max_input_lte_signal": bool(observation_end <= signal_date),
                "holding_strictly_after_signal": bool(holding_index.min() > signal_date),
            }
        )

        latest_weights = selected.join(equal_weight).join(strategy_weight)
        latest_weights.insert(0, "signal_date", signal_date)
        latest_weights.insert(1, "asset", latest_weights.index)
        previous_equal = equal_weight
        previous_strategy = strategy_weight

    if not return_rows:
        raise RuntimeError("Backtest produced no valid periods")

    returns = pd.DataFrame(return_rows).set_index("realized_date").sort_index()
    decisions = pd.DataFrame(decision_rows).set_index("signal_date").sort_index()
    audit = pd.DataFrame(audit_rows).sort_values("signal_date")

    performance = pd.DataFrame(
        {
            column: performance_stats(returns[column], returns["RF"])
            for column in ("Point-in-time Equal Weight", "Beta Risk Budget", "KOSPI200")
        }
    )
    performance.loc["Average Signal Beta", "Point-in-time Equal Weight"] = decisions[
        "equal_signal_beta"
    ].mean()
    performance.loc["Average Signal Beta", "Beta Risk Budget"] = decisions[
        "strategy_signal_beta"
    ].mean()
    performance.loc["Average Target Turnover", "Point-in-time Equal Weight"] = decisions[
        "equal_target_turnover"
    ].mean()
    performance.loc["Average Target Turnover", "Beta Risk Budget"] = decisions[
        "strategy_target_turnover"
    ].mean()

    returns.to_csv(table_dir / "ai_beta_monthly_returns.csv", encoding="utf-8-sig")
    performance.to_csv(table_dir / "ai_beta_performance.csv", encoding="utf-8-sig")
    decisions.to_csv(table_dir / "ai_beta_policy_decisions.csv", encoding="utf-8-sig")
    audit.to_csv(table_dir / "ai_beta_lookahead_audit.csv", index=False, encoding="utf-8-sig")
    latest_weights.to_csv(table_dir / "ai_beta_latest_weights.csv", index=False, encoding="utf-8-sig")
    save_charts(output_dir, returns, decisions)

    if args.policy == "clova":
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    preview_columns = [
        "signal_date",
        "Point-in-time Equal Weight",
        "Beta Risk Budget",
        "KOSPI200",
        "RF",
    ]
    print("regression input preview (signal date -> realized holding-period end):")
    print(returns[preview_columns].head(5).to_string())
    print("\nlook-ahead audit:")
    print(audit[["max_input_lte_signal", "holding_strictly_after_signal"]].all().to_string())
    print("\nperformance:")
    print(performance.to_string(float_format=lambda value: f"{value:.6f}"))


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    run_backtest(root, args)


if __name__ == "__main__":
    main()
