from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .strategy import build_signals, strategy_spec


@dataclass(frozen=True)
class ValidationConfig:
    train_fraction: float = 0.60
    validation_fraction: float = 0.20
    walk_forward_train_bars: int = 2000
    walk_forward_test_bars: int = 500
    walk_forward_step_bars: int = 500
    parameter_perturbation: float = 0.20
    monte_carlo_runs: int = 1000
    monte_carlo_ruin_drawdown: float = 0.30
    min_oos_trades: int = 20
    min_oos_profit_factor: float = 1.0
    min_oos_sharpe: float = 0.0
    max_oos_drawdown: float = 0.20
    min_walk_forward_positive_fraction: float = 0.50
    min_parameter_stability_fraction: float = 0.50
    max_monte_carlo_ruin_probability: float = 0.05
    seed: int = 42


@dataclass(frozen=True)
class DataSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    out_of_sample: pd.DataFrame


def chronological_split(
    df: pd.DataFrame,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
) -> DataSplit:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train + validation fractions must leave untouched out-of-sample data")
    if len(df) < 100:
        raise ValueError("at least 100 bars are required for chronological validation")

    n = len(df)
    train_end = max(1, int(n * train_fraction))
    validation_end = max(train_end + 1, int(n * (train_fraction + validation_fraction)))
    validation_end = min(validation_end, n - 1)
    return DataSplit(
        train=df.iloc[:train_end].copy(),
        validation=df.iloc[train_end:validation_end].copy(),
        out_of_sample=df.iloc[validation_end:].copy(),
    )


def _candidate_score(result: dict[str, Any]) -> float:
    if result["trades"] <= 0:
        return -1e9
    sharpe = float(result["sharpe_approx"])
    drawdown = float(result["max_drawdown_pct"])
    profit_factor = float(result["profit_factor"])
    if not np.isfinite(profit_factor):
        profit_factor = 3.0
    return sharpe - 2.0 * drawdown + 0.10 * min(profit_factor, 3.0)


def _is_integer_parameter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_candidate(name: str, params: dict[str, Any]) -> bool:
    try:
        # Build-time validation catches relations such as fast >= slow.
        # A tiny dummy frame is intentionally avoided because some indicators need long lookbacks.
        if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
            return False
        if "oversold" in params and "overbought" in params and params["oversold"] >= params["overbought"]:
            return False
        for key, value in params.items():
            if key in {"period", "lookback", "smoothing", "ema_period", "atr_period", "signal_period", "fast", "slow"}:
                if int(value) < 2:
                    return False
            if key in {"stop_atr", "take_profit_atr", "breakout_atr", "z_entry", "entry_z"} and float(value) <= 0:
                return False
        strategy_spec(name)
        return True
    except Exception:
        return False


def parameter_neighborhood(
    strategy_name: str,
    base_params: dict[str, Any] | None = None,
    perturbation: float = 0.20,
    max_candidates: int = 21,
) -> list[dict[str, Any]]:
    if not 0 < perturbation <= 0.75:
        raise ValueError("perturbation must be in (0, 0.75]")
    spec = strategy_spec(strategy_name)
    base = dict(spec.defaults)
    if base_params:
        base.update(base_params)

    # Keep execution/risk parameters fixed during signal robustness search. This reduces
    # degrees of freedom and makes the test less prone to finding a lucky stop/TP pair.
    excluded = {"atr_period", "stop_atr", "take_profit_atr"}
    candidates: list[dict[str, Any]] = [dict(base)]

    for key, value in base.items():
        if key in excluded or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for multiplier in (1.0 - perturbation, 1.0 + perturbation):
            candidate = dict(base)
            varied = float(value) * multiplier
            if _is_integer_parameter(value):
                candidate[key] = max(2, int(round(varied)))
            else:
                candidate[key] = varied
            if _valid_candidate(strategy_name, candidate) and candidate not in candidates:
                candidates.append(candidate)
            if len(candidates) >= max_candidates:
                return candidates

    return candidates


def _run(df: pd.DataFrame, backtest_cfg: BacktestConfig, strategy_name: str, params: dict[str, Any]) -> dict[str, Any]:
    signals = build_signals(df, strategy_name, params)
    return run_backtest(signals, backtest_cfg)


def select_parameters(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None = None,
    perturbation: float = 0.20,
) -> tuple[dict[str, Any], pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    candidates = parameter_neighborhood(strategy_name, base_params, perturbation)

    for idx, params in enumerate(candidates):
        try:
            train_result = _run(train, backtest_cfg, strategy_name, params)
            validation_result = _run(validation, backtest_cfg, strategy_name, params)
            train_score = _candidate_score(train_result)
            validation_score = _candidate_score(validation_result)
            # Validation gets higher weight than train because it was not used to create
            # the candidate's observed train score.
            robust_score = 0.40 * train_score + 0.60 * validation_score
            rows.append(
                {
                    "candidate": idx,
                    "params": json.dumps(params, sort_keys=True),
                    "train_score": train_score,
                    "validation_score": validation_score,
                    "robust_score": robust_score,
                    "train_return": train_result["return_pct"],
                    "validation_return": validation_result["return_pct"],
                    "train_sharpe": train_result["sharpe_approx"],
                    "validation_sharpe": validation_result["sharpe_approx"],
                    "validation_drawdown": validation_result["max_drawdown_pct"],
                    "validation_trades": validation_result["trades"],
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "candidate": idx,
                    "params": json.dumps(params, sort_keys=True),
                    "train_score": -1e9,
                    "validation_score": -1e9,
                    "robust_score": -1e9,
                    "error": str(exc),
                }
            )

    table = pd.DataFrame(rows).sort_values("robust_score", ascending=False).reset_index(drop=True)
    if table.empty or float(table.iloc[0]["robust_score"]) <= -1e8:
        raise RuntimeError(f"no valid parameter candidate for {strategy_name}")
    best = json.loads(str(table.iloc[0]["params"]))
    return best, table


def walk_forward_evaluation(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None,
    cfg: ValidationConfig,
) -> pd.DataFrame:
    train_bars = cfg.walk_forward_train_bars
    test_bars = cfg.walk_forward_test_bars
    step = cfg.walk_forward_step_bars
    if train_bars < 100 or test_bars < 20 or step < 1:
        raise ValueError("walk-forward windows are too small")

    rows: list[dict[str, Any]] = []
    start = 0
    window = 0
    while start + train_bars + test_bars <= len(df):
        train = df.iloc[start : start + train_bars]
        test = df.iloc[start + train_bars : start + train_bars + test_bars]
        # Split the rolling train block internally so parameter choice still has a validation slice.
        inner_cut = max(50, int(len(train) * 0.75))
        inner_cut = min(inner_cut, len(train) - 20)
        train_inner = train.iloc[:inner_cut]
        validation_inner = train.iloc[inner_cut:]
        try:
            selected, selection = select_parameters(
                train_inner,
                validation_inner,
                backtest_cfg,
                strategy_name,
                base_params,
                cfg.parameter_perturbation,
            )
            result = _run(test, backtest_cfg, strategy_name, selected)
            rows.append(
                {
                    "window": window,
                    "train_start": train.index[0],
                    "train_end": train.index[-1],
                    "test_start": test.index[0],
                    "test_end": test.index[-1],
                    "selected_params": json.dumps(selected, sort_keys=True),
                    "selection_score": float(selection.iloc[0]["robust_score"]),
                    "test_return": result["return_pct"],
                    "test_sharpe": result["sharpe_approx"],
                    "test_drawdown": result["max_drawdown_pct"],
                    "test_profit_factor": result["profit_factor"],
                    "test_trades": result["trades"],
                    "positive": bool(result["net_profit"] > 0),
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "window": window,
                    "train_start": train.index[0],
                    "train_end": train.index[-1],
                    "test_start": test.index[0],
                    "test_end": test.index[-1],
                    "selected_params": "{}",
                    "selection_score": -1e9,
                    "test_return": np.nan,
                    "test_sharpe": np.nan,
                    "test_drawdown": np.nan,
                    "test_profit_factor": np.nan,
                    "test_trades": 0,
                    "positive": False,
                    "error": str(exc),
                }
            )
        start += step
        window += 1

    return pd.DataFrame(rows)


def parameter_stability(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None,
    perturbation: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for params in parameter_neighborhood(strategy_name, base_params, perturbation):
        try:
            result = _run(df, backtest_cfg, strategy_name, params)
            rows.append(
                {
                    "params": json.dumps(params, sort_keys=True),
                    "return_pct": result["return_pct"],
                    "sharpe": result["sharpe_approx"],
                    "drawdown": result["max_drawdown_pct"],
                    "profit_factor": result["profit_factor"],
                    "trades": result["trades"],
                    "positive": bool(result["net_profit"] > 0),
                }
            )
        except Exception:
            continue

    table = pd.DataFrame(rows)
    if table.empty:
        return {"positive_fraction": 0.0, "median_sharpe": np.nan, "sharpe_std": np.nan, "table": table}
    return {
        "positive_fraction": float(table["positive"].mean()),
        "median_sharpe": float(table["sharpe"].median()),
        "sharpe_std": float(table["sharpe"].std(ddof=0)),
        "table": table,
    }


def monte_carlo_trade_paths(
    trade_pnls: Iterable[float],
    initial_equity: float,
    runs: int = 1000,
    ruin_drawdown: float = 0.30,
    seed: int = 42,
) -> dict[str, float]:
    pnl = np.asarray(list(trade_pnls), dtype=float)
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if not 0 < ruin_drawdown < 1:
        raise ValueError("ruin_drawdown must be between 0 and 1")
    if len(pnl) == 0:
        return {
            "runs": float(runs),
            "trades": 0.0,
            "median_final_equity": float(initial_equity),
            "p05_final_equity": float(initial_equity),
            "p95_final_equity": float(initial_equity),
            "median_max_drawdown": 0.0,
            "p95_max_drawdown": 0.0,
            "loss_probability": 0.0,
            "ruin_probability": 0.0,
        }

    rng = np.random.default_rng(seed)
    finals = np.empty(runs, dtype=float)
    max_dds = np.empty(runs, dtype=float)
    ruined = np.zeros(runs, dtype=bool)
    ruin_floor = initial_equity * (1.0 - ruin_drawdown)

    for i in range(runs):
        sampled = rng.choice(pnl, size=len(pnl), replace=True)
        curve = initial_equity + np.cumsum(sampled)
        curve = np.r_[initial_equity, curve]
        peaks = np.maximum.accumulate(curve)
        drawdowns = np.divide(peaks - curve, peaks, out=np.zeros_like(curve), where=peaks > 0)
        finals[i] = curve[-1]
        max_dds[i] = float(np.max(drawdowns))
        ruined[i] = bool(np.min(curve) <= ruin_floor or np.min(curve) <= 0)

    return {
        "runs": float(runs),
        "trades": float(len(pnl)),
        "median_final_equity": float(np.median(finals)),
        "p05_final_equity": float(np.quantile(finals, 0.05)),
        "p95_final_equity": float(np.quantile(finals, 0.95)),
        "median_max_drawdown": float(np.median(max_dds)),
        "p95_max_drawdown": float(np.quantile(max_dds, 0.95)),
        "loss_probability": float(np.mean(finals < initial_equity)),
        "ruin_probability": float(np.mean(ruined)),
    }


def validate_strategy(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None = None,
    cfg: ValidationConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or ValidationConfig()
    split = chronological_split(df, cfg.train_fraction, cfg.validation_fraction)

    selected_params, selection_table = select_parameters(
        split.train,
        split.validation,
        backtest_cfg,
        strategy_name,
        base_params,
        cfg.parameter_perturbation,
    )

    train_result = _run(split.train, backtest_cfg, strategy_name, selected_params)
    validation_result = _run(split.validation, backtest_cfg, strategy_name, selected_params)
    oos_result = _run(split.out_of_sample, backtest_cfg, strategy_name, selected_params)

    pre_oos = pd.concat([split.train, split.validation])
    stability = parameter_stability(
        pre_oos,
        backtest_cfg,
        strategy_name,
        selected_params,
        cfg.parameter_perturbation,
    )

    wf = walk_forward_evaluation(pre_oos, backtest_cfg, strategy_name, selected_params, cfg)
    wf_valid = wf[wf["error"] == ""] if not wf.empty else wf
    wf_positive_fraction = float(wf_valid["positive"].mean()) if not wf_valid.empty else 0.0

    oos_trade_pnls = [float(t.pnl) for t in oos_result["trade_log"]]
    mc = monte_carlo_trade_paths(
        oos_trade_pnls,
        backtest_cfg.initial_equity,
        cfg.monte_carlo_runs,
        cfg.monte_carlo_ruin_drawdown,
        cfg.seed,
    )

    checks = {
        "oos_min_trades": oos_result["trades"] >= cfg.min_oos_trades,
        "oos_profit_factor": oos_result["profit_factor"] >= cfg.min_oos_profit_factor,
        "oos_sharpe": oos_result["sharpe_approx"] >= cfg.min_oos_sharpe,
        "oos_drawdown": oos_result["max_drawdown_pct"] <= cfg.max_oos_drawdown,
        "walk_forward_consistency": wf_positive_fraction >= cfg.min_walk_forward_positive_fraction,
        "parameter_stability": stability["positive_fraction"] >= cfg.min_parameter_stability_fraction,
        "monte_carlo_ruin": mc["ruin_probability"] <= cfg.max_monte_carlo_ruin_probability,
    }
    passed = sum(bool(x) for x in checks.values())
    if passed == len(checks):
        verdict = "PASS"
    elif passed >= len(checks) - 2:
        verdict = "WATCH"
    else:
        verdict = "REJECT"

    train_sharpe = float(train_result["sharpe_approx"])
    oos_sharpe = float(oos_result["sharpe_approx"])
    sharpe_decay = train_sharpe - oos_sharpe

    return {
        "strategy": strategy_name,
        "family": strategy_spec(strategy_name).family,
        "verdict": verdict,
        "checks_passed": passed,
        "checks_total": len(checks),
        "checks": checks,
        "selected_params": selected_params,
        "train": train_result,
        "validation": validation_result,
        "out_of_sample": oos_result,
        "sharpe_decay": sharpe_decay,
        "walk_forward_positive_fraction": wf_positive_fraction,
        "parameter_stability_fraction": stability["positive_fraction"],
        "parameter_stability_median_sharpe": stability["median_sharpe"],
        "monte_carlo": mc,
        "selection_table": selection_table,
        "walk_forward": wf,
        "stability_table": stability["table"],
    }


def validation_summary(report: dict[str, Any]) -> dict[str, Any]:
    oos = report["out_of_sample"]
    mc = report["monte_carlo"]
    return {
        "strategy": report["strategy"],
        "family": report["family"],
        "verdict": report["verdict"],
        "checks_passed": report["checks_passed"],
        "checks_total": report["checks_total"],
        "oos_return_pct": oos["return_pct"],
        "oos_sharpe": oos["sharpe_approx"],
        "oos_max_drawdown_pct": oos["max_drawdown_pct"],
        "oos_profit_factor": oos["profit_factor"],
        "oos_trades": oos["trades"],
        "walk_forward_positive_fraction": report["walk_forward_positive_fraction"],
        "parameter_stability_fraction": report["parameter_stability_fraction"],
        "monte_carlo_ruin_probability": mc["ruin_probability"],
        "monte_carlo_p95_drawdown": mc["p95_max_drawdown"],
        "sharpe_decay": report["sharpe_decay"],
        "selected_params": json.dumps(report["selected_params"], sort_keys=True),
    }


def validate_strategy_batch(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_names: Iterable[str],
    cfg: ValidationConfig | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name in strategy_names:
        try:
            report = validate_strategy(df, backtest_cfg, name, cfg=cfg)
            row = validation_summary(report)
            row["error"] = ""
            rows.append(row)
        except Exception as exc:
            rows.append(
                {
                    "strategy": name,
                    "family": strategy_spec(name).family,
                    "verdict": "ERROR",
                    "checks_passed": 0,
                    "checks_total": 7,
                    "error": str(exc),
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    verdict_order = pd.Categorical(out["verdict"], categories=["PASS", "WATCH", "REJECT", "ERROR"], ordered=True)
    out = out.assign(_verdict_order=verdict_order)
    sort_cols = ["_verdict_order"]
    ascending = [True]
    if "oos_sharpe" in out.columns:
        sort_cols.append("oos_sharpe")
        ascending.append(False)
    return out.sort_values(sort_cols, ascending=ascending).drop(columns=["_verdict_order"]).reset_index(drop=True)
