from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .strategy import build_signals
from .validation import ValidationConfig, chronological_split, validate_strategy, validation_summary


@dataclass(frozen=True)
class PortfolioConfig:
    max_strategy_weight: float = 0.35
    min_strategies: int = 2
    allowed_verdicts: tuple[str, ...] = ("PASS", "WATCH")
    monte_carlo_runs: int = 1000
    monte_carlo_block_size: int = 24
    ruin_drawdown: float = 0.30
    seed: int = 42


def _returns_from_curves(curves: dict[str, pd.Series]) -> pd.DataFrame:
    if not curves:
        return pd.DataFrame()
    aligned = pd.concat({name: curve for name, curve in curves.items()}, axis=1).sort_index().ffill()
    return aligned.pct_change().fillna(0.0)


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    if returns.empty:
        return pd.DataFrame()
    return returns.corr().fillna(0.0)


def _cap_weights(weights: pd.Series, max_weight: float) -> pd.Series:
    if not 0 < max_weight <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    n = int((weights > 0).sum())
    if n == 0:
        raise ValueError("no positive portfolio weights")
    if n * max_weight < 1.0 - 1e-12:
        raise ValueError("max_weight is too small for the number of active strategies")

    w = weights.clip(lower=0.0).astype(float)
    if w.sum() <= 0:
        raise ValueError("portfolio scores must sum to a positive value")
    w = w / w.sum()

    # Iteratively cap oversized names and redistribute the remainder in proportion
    # to the uncapped scores. This keeps the rule deterministic and transparent.
    for _ in range(len(w) + 2):
        over = w > max_weight + 1e-12
        if not bool(over.any()):
            break
        w.loc[over] = max_weight
        fixed = float(w.loc[over].sum())
        free = ~over
        remaining = 1.0 - fixed
        if remaining < -1e-12 or not bool(free.any()):
            raise ValueError("unable to satisfy max-weight constraint")
        base = weights.loc[free].clip(lower=0.0)
        if base.sum() <= 0:
            w.loc[free] = remaining / int(free.sum())
        else:
            w.loc[free] = remaining * base / base.sum()

    return w / w.sum()


def diversification_weights(returns: pd.DataFrame, max_weight: float = 0.35) -> pd.Series:
    if returns.shape[1] < 2:
        raise ValueError("at least two strategy return series are required")

    vol = returns.std(ddof=0)
    valid = vol[vol > 1e-12].index
    if len(valid) < 2:
        raise ValueError("at least two strategies need non-zero return volatility")
    clean = returns.loc[:, valid]
    vol = clean.std(ddof=0)
    corr = clean.corr().fillna(0.0).abs()

    avg_abs_corr = pd.Series(index=clean.columns, dtype=float)
    for name in clean.columns:
        others = corr.loc[name].drop(index=name, errors="ignore")
        avg_abs_corr.loc[name] = float(others.mean()) if len(others) else 0.0

    # Prefer lower volatility and lower average correlation. This is deliberately
    # not a maximum-Sharpe optimizer, which would be much easier to overfit.
    score = 1.0 / (vol * (1.0 + avg_abs_corr))
    return _cap_weights(score, max_weight)


def risk_contributions(returns: pd.DataFrame, weights: pd.Series) -> pd.Series:
    clean = returns.loc[:, weights.index]
    cov = clean.cov(ddof=0).to_numpy(dtype=float)
    w = weights.to_numpy(dtype=float)
    portfolio_var = float(w @ cov @ w)
    if portfolio_var <= 1e-20:
        return pd.Series(0.0, index=weights.index, name="risk_contribution")
    marginal = cov @ w
    contribution = w * marginal / portfolio_var
    return pd.Series(contribution, index=weights.index, name="risk_contribution")


def portfolio_metrics(
    strategy_returns: pd.DataFrame,
    weights: pd.Series,
    periods_per_year: float,
    initial_equity: float = 10_000.0,
) -> dict[str, Any]:
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    clean = strategy_returns.loc[:, weights.index].fillna(0.0)
    portfolio_returns = clean.mul(weights, axis=1).sum(axis=1)
    equity = initial_equity * (1.0 + portfolio_returns).cumprod()
    if len(equity) == 0:
        equity = pd.Series([initial_equity], dtype=float)

    std = float(portfolio_returns.std(ddof=0)) if len(portfolio_returns) else 0.0
    sharpe = 0.0
    if std > 0:
        sharpe = float(np.sqrt(periods_per_year) * portfolio_returns.mean() / std)
    annualized_vol = float(std * np.sqrt(periods_per_year))
    drawdown = (equity.cummax() - equity) / equity.cummax()
    max_drawdown = float(drawdown.max()) if len(drawdown) else 0.0
    total_return = float(equity.iloc[-1] / initial_equity - 1.0)

    component_vol = clean.std(ddof=0).reindex(weights.index).fillna(0.0)
    weighted_component_vol = float((weights * component_vol).sum())
    diversification_ratio = weighted_component_vol / std if std > 0 else 0.0
    hhi = float((weights**2).sum())
    effective_strategies = 1.0 / hhi if hhi > 0 else 0.0

    return {
        "return_pct": total_return,
        "sharpe": sharpe,
        "annualized_volatility": annualized_vol,
        "max_drawdown_pct": max_drawdown,
        "diversification_ratio": float(diversification_ratio),
        "effective_strategies": float(effective_strategies),
        "equity_curve": equity,
        "returns": portfolio_returns,
    }


def portfolio_monte_carlo(
    portfolio_returns: Iterable[float],
    initial_equity: float,
    runs: int = 1000,
    block_size: int = 24,
    ruin_drawdown: float = 0.30,
    seed: int = 42,
) -> dict[str, float]:
    values = np.asarray(list(portfolio_returns), dtype=float)
    values = values[np.isfinite(values)]
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    if not 0 < ruin_drawdown < 1:
        raise ValueError("ruin_drawdown must be between 0 and 1")
    if len(values) == 0:
        return {
            "runs": float(runs),
            "median_final_equity": float(initial_equity),
            "p05_final_equity": float(initial_equity),
            "p95_max_drawdown": 0.0,
            "loss_probability": 0.0,
            "ruin_probability": 0.0,
        }

    rng = np.random.default_rng(seed)
    n = len(values)
    block = min(block_size, n)
    finals = np.empty(runs, dtype=float)
    max_dds = np.empty(runs, dtype=float)
    ruined = np.zeros(runs, dtype=bool)
    ruin_floor = initial_equity * (1.0 - ruin_drawdown)

    for i in range(runs):
        sampled_parts: list[np.ndarray] = []
        collected = 0
        while collected < n:
            if n == block:
                start = 0
            else:
                start = int(rng.integers(0, n - block + 1))
            part = values[start : start + block]
            sampled_parts.append(part)
            collected += len(part)
        sampled = np.concatenate(sampled_parts)[:n]
        curve = initial_equity * np.cumprod(1.0 + sampled)
        curve = np.r_[initial_equity, curve]
        peaks = np.maximum.accumulate(curve)
        dd = np.divide(peaks - curve, peaks, out=np.zeros_like(curve), where=peaks > 0)
        finals[i] = curve[-1]
        max_dds[i] = float(np.max(dd))
        ruined[i] = bool(np.min(curve) <= ruin_floor or np.min(curve) <= 0)

    return {
        "runs": float(runs),
        "median_final_equity": float(np.median(finals)),
        "p05_final_equity": float(np.quantile(finals, 0.05)),
        "p95_max_drawdown": float(np.quantile(max_dds, 0.95)),
        "loss_probability": float(np.mean(finals < initial_equity)),
        "ruin_probability": float(np.mean(ruined)),
    }


def research_portfolio(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_names: Iterable[str],
    validation_cfg: ValidationConfig | None = None,
    portfolio_cfg: PortfolioConfig | None = None,
) -> dict[str, Any]:
    validation_cfg = validation_cfg or ValidationConfig()
    portfolio_cfg = portfolio_cfg or PortfolioConfig()
    split = chronological_split(df, validation_cfg.train_fraction, validation_cfg.validation_fraction)
    pre_oos = pd.concat([split.train, split.validation])

    pre_curves: dict[str, pd.Series] = {}
    oos_curves: dict[str, pd.Series] = {}
    candidate_rows: list[dict[str, Any]] = []

    for name in strategy_names:
        try:
            report = validate_strategy(df, backtest_cfg, name, cfg=validation_cfg)
            summary = validation_summary(report)
            selected = report["selected_params"]
            row = dict(summary)
            row["included"] = report["verdict"] in portfolio_cfg.allowed_verdicts
            row["error"] = ""
            candidate_rows.append(row)

            if not row["included"]:
                continue

            pre_result = run_backtest(build_signals(pre_oos, name, selected), backtest_cfg)
            oos_result = run_backtest(build_signals(split.out_of_sample, name, selected), backtest_cfg)
            pre_curves[name] = pre_result["equity_curve"]
            oos_curves[name] = oos_result["equity_curve"]
        except Exception as exc:
            candidate_rows.append(
                {
                    "strategy": name,
                    "verdict": "ERROR",
                    "included": False,
                    "error": str(exc),
                }
            )

    candidate_table = pd.DataFrame(candidate_rows)
    if len(pre_curves) < portfolio_cfg.min_strategies:
        raise RuntimeError(
            f"only {len(pre_curves)} strategies passed the portfolio gate; "
            f"at least {portfolio_cfg.min_strategies} are required"
        )

    pre_returns = _returns_from_curves(pre_curves)
    oos_returns = _returns_from_curves(oos_curves)
    weights = diversification_weights(pre_returns, portfolio_cfg.max_strategy_weight)
    oos_returns = oos_returns.reindex(columns=weights.index).fillna(0.0)

    corr = correlation_matrix(pre_returns.loc[:, weights.index])
    risk = risk_contributions(pre_returns, weights)
    metrics = portfolio_metrics(
        oos_returns,
        weights,
        backtest_cfg.periods_per_year,
        initial_equity=backtest_cfg.initial_equity,
    )
    mc = portfolio_monte_carlo(
        metrics["returns"],
        backtest_cfg.initial_equity,
        runs=portfolio_cfg.monte_carlo_runs,
        block_size=portfolio_cfg.monte_carlo_block_size,
        ruin_drawdown=portfolio_cfg.ruin_drawdown,
        seed=portfolio_cfg.seed,
    )

    weight_table = pd.DataFrame(
        {
            "strategy": weights.index,
            "weight": weights.values,
            "risk_contribution": risk.reindex(weights.index).values,
        }
    )
    if not candidate_table.empty and "strategy" in candidate_table.columns:
        cols = [c for c in ("strategy", "family", "verdict", "oos_return_pct", "oos_sharpe", "oos_max_drawdown_pct", "selected_params") if c in candidate_table.columns]
        weight_table = weight_table.merge(candidate_table[cols], on="strategy", how="left")

    summary = {
        "strategies": int(len(weights)),
        "return_pct": metrics["return_pct"],
        "sharpe": metrics["sharpe"],
        "annualized_volatility": metrics["annualized_volatility"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "diversification_ratio": metrics["diversification_ratio"],
        "effective_strategies": metrics["effective_strategies"],
        "monte_carlo_p95_drawdown": mc["p95_max_drawdown"],
        "monte_carlo_loss_probability": mc["loss_probability"],
        "monte_carlo_ruin_probability": mc["ruin_probability"],
        "weights_json": json.dumps({k: float(v) for k, v in weights.items()}, sort_keys=True),
    }

    return {
        "summary": summary,
        "weights": weight_table,
        "correlation": corr,
        "candidate_table": candidate_table,
        "oos_equity_curve": metrics["equity_curve"],
        "oos_returns": metrics["returns"],
        "monte_carlo": mc,
    }


def save_portfolio_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": root / "portfolio_summary.csv",
        "weights": root / "portfolio_weights.csv",
        "correlation": root / "strategy_correlation.csv",
        "candidates": root / "portfolio_candidates.csv",
        "equity": root / "portfolio_oos_equity.csv",
    }
    pd.DataFrame([report["summary"]]).to_csv(paths["summary"], index=False)
    report["weights"].to_csv(paths["weights"], index=False)
    report["correlation"].to_csv(paths["correlation"])
    report["candidate_table"].to_csv(paths["candidates"], index=False)
    report["oos_equity_curve"].rename("equity").to_csv(paths["equity"], header=True)
    return paths
