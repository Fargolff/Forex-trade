from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .data import normalize_ohlc
from .strategy import build_signals

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


DEFAULT_PARAMS: dict[str, Any] = {
    "breakout_atr": 0.70,
    "atr_period": 56,
    "stop_atr": 0.75,
    "take_profit_atr": 50.0,
}


def _parse_utc_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _json_default(value: Any):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def load_mt5_range(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package is not installed. Run: pip install -r requirements.txt")

    timeframe_name = f"TIMEFRAME_{timeframe.upper()}"
    if not hasattr(mt5, timeframe_name):
        raise ValueError(f"unsupported MT5 timeframe: {timeframe}")

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 could not select symbol: {symbol}")

        rates = mt5.copy_rates_range(symbol, getattr(mt5, timeframe_name), start, end)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 returned no rates for {symbol} {timeframe}: {mt5.last_error()}")

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        keep = [c for c in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume") if c in df.columns]
        df = normalize_ohlc(df[keep])
        # copy_rates_range can include the exact end timestamp; this workflow treats end as exclusive.
        return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))].copy()
    finally:
        mt5.shutdown()


def validate_coverage(df: pd.DataFrame, start: datetime, end: datetime) -> None:
    if df.empty:
        raise RuntimeError("MT5 data is empty after date filtering")
    first = df.index.min()
    last = df.index.max()
    expected_first_deadline = pd.Timestamp(start) + pd.Timedelta(days=7)
    expected_last_floor = pd.Timestamp(end) - pd.Timedelta(days=7)
    if first > expected_first_deadline or last < expected_last_floor:
        raise RuntimeError(
            "MT5 history coverage is incomplete. "
            f"Requested {start.date()} to {end.date()} (end exclusive), received {first} to {last}. "
            "In MT5, increase Tools > Options > Charts > Max bars in chart, open EURUSD H1, "
            "download/scroll history, then rerun."
        )


def make_cfg(
    initial_equity: float,
    risk_per_trade: float,
    max_daily_loss_pct: float,
    max_drawdown_pct: float,
    spread_pips: float,
    slippage_pips: float,
    commission_per_lot_round_turn: float,
) -> BacktestConfig:
    return BacktestConfig(
        initial_equity=initial_equity,
        risk_per_trade=risk_per_trade,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        pip_size=0.0001,
        pip_value_per_lot=10.0,
        spread_pips=spread_pips,
        slippage_pips=slippage_pips,
        commission_per_lot_round_turn=commission_per_lot_round_turn,
        periods_per_year=252.0 * 24.0,
    )


def backtest(df: pd.DataFrame, cfg: BacktestConfig, params: dict[str, Any]) -> dict[str, Any]:
    signals = build_signals(df, "volatility_breakout", params)
    return run_backtest(signals, cfg)


def compact_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "initial_equity": float(result["initial_equity"]),
        "final_equity": float(result["final_equity"]),
        "net_profit": float(result["net_profit"]),
        "return_pct": float(result["return_pct"]),
        "max_drawdown_pct": float(result["max_drawdown_pct"]),
        "sharpe_approx": float(result["sharpe_approx"]),
        "trades": int(result["trades"]),
        "win_rate": float(result["win_rate"]),
        "profit_factor": float(result["profit_factor"]),
    }


def yearly_evaluation(df: pd.DataFrame, cfg: BacktestConfig, params: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year in sorted(df.index.year.unique()):
        segment = df[df.index.year == year]
        if len(segment) < 100:
            continue
        result = backtest(segment, cfg, params)
        rows.append({"year": int(year), "bars": len(segment), **compact_metrics(result)})
    return pd.DataFrame(rows)


def walk_forward_fixed(
    df: pd.DataFrame,
    cfg: BacktestConfig,
    params: dict[str, Any],
    train_months: int = 12,
    test_months: int = 3,
) -> pd.DataFrame:
    """Expanding chronological check with a fixed, pre-specified strategy.

    No real-data parameter tuning occurs here. Each test window is strictly after its
    reference training window, which makes this a leakage-free forward-stability check
    for the parameters that were selected before seeing this MT5 data.
    """
    if train_months < 3 or test_months < 1:
        raise ValueError("walk-forward months are too small")

    start = df.index.min().to_period("M").to_timestamp().tz_localize("UTC")
    end = df.index.max()
    test_start = start + pd.DateOffset(months=train_months)
    rows: list[dict[str, Any]] = []
    window = 0

    while test_start < end:
        train_start = test_start - pd.DateOffset(months=train_months)
        test_end = test_start + pd.DateOffset(months=test_months)
        train = df[(df.index >= train_start) & (df.index < test_start)]
        test = df[(df.index >= test_start) & (df.index < test_end)]
        if len(train) >= 100 and len(test) >= 100:
            result = backtest(test, cfg, params)
            rows.append(
                {
                    "window": window,
                    "train_start": train.index.min(),
                    "train_end": train.index.max(),
                    "test_start": test.index.min(),
                    "test_end": test.index.max(),
                    "test_bars": len(test),
                    **{f"test_{k}": v for k, v in compact_metrics(result).items()},
                    "positive": bool(result["net_profit"] > 0),
                }
            )
            window += 1
        test_start = test_end

    return pd.DataFrame(rows)


def trade_returns(result: dict[str, Any]) -> np.ndarray:
    equity = float(result["initial_equity"])
    returns: list[float] = []
    for trade in result["trade_log"]:
        if equity <= 0:
            break
        r = float(trade.pnl) / equity
        # Defensive guard against corrupted paths; the normal risk engine never reaches this.
        r = max(r, -0.999999)
        returns.append(r)
        equity += float(trade.pnl)
    return np.asarray(returns, dtype=float)


def monte_carlo_trade_returns(
    returns: np.ndarray,
    initial_equity: float,
    runs: int,
    max_drawdown_limit: float,
    seed: int,
) -> dict[str, float]:
    if runs < 1:
        raise ValueError("mc-runs must be >= 1")
    if len(returns) == 0:
        return {
            "runs": float(runs),
            "trades": 0.0,
            "median_final_equity": float(initial_equity),
            "p05_final_equity": float(initial_equity),
            "p95_final_equity": float(initial_equity),
            "median_max_drawdown": 0.0,
            "p95_max_drawdown": 0.0,
            "loss_probability": 0.0,
            "drawdown_limit_breach_probability": 0.0,
        }

    rng = np.random.default_rng(seed)
    finals = np.empty(runs, dtype=float)
    max_dds = np.empty(runs, dtype=float)

    for i in range(runs):
        sampled = rng.choice(returns, size=len(returns), replace=True)
        curve = initial_equity * np.cumprod(1.0 + sampled)
        curve = np.r_[initial_equity, curve]
        peaks = np.maximum.accumulate(curve)
        dd = np.divide(peaks - curve, peaks, out=np.zeros_like(curve), where=peaks > 0)
        finals[i] = curve[-1]
        max_dds[i] = float(dd.max())

    return {
        "runs": float(runs),
        "trades": float(len(returns)),
        "median_final_equity": float(np.median(finals)),
        "p05_final_equity": float(np.quantile(finals, 0.05)),
        "p95_final_equity": float(np.quantile(finals, 0.95)),
        "median_max_drawdown": float(np.median(max_dds)),
        "p95_max_drawdown": float(np.quantile(max_dds, 0.95)),
        "loss_probability": float(np.mean(finals < initial_equity)),
        "drawdown_limit_breach_probability": float(np.mean(max_dds > max_drawdown_limit)),
    }


def cost_stress(
    df: pd.DataFrame,
    base_cfg: BacktestConfig,
    params: dict[str, Any],
    multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for multiplier in multipliers:
        cfg = BacktestConfig(
            initial_equity=base_cfg.initial_equity,
            risk_per_trade=base_cfg.risk_per_trade,
            max_daily_loss_pct=base_cfg.max_daily_loss_pct,
            max_drawdown_pct=base_cfg.max_drawdown_pct,
            pip_size=base_cfg.pip_size,
            pip_value_per_lot=base_cfg.pip_value_per_lot,
            spread_pips=base_cfg.spread_pips * multiplier,
            slippage_pips=base_cfg.slippage_pips * multiplier,
            commission_per_lot_round_turn=base_cfg.commission_per_lot_round_turn * multiplier,
            periods_per_year=base_cfg.periods_per_year,
        )
        result = backtest(df, cfg, params)
        rows.append(
            {
                "cost_multiplier": multiplier,
                "spread_pips": cfg.spread_pips,
                "slippage_pips": cfg.slippage_pips,
                "commission_per_lot_round_turn": cfg.commission_per_lot_round_turn,
                **compact_metrics(result),
            }
        )
    return pd.DataFrame(rows)


def parameter_stability(df: pd.DataFrame, cfg: BacktestConfig, params: dict[str, Any]) -> pd.DataFrame:
    atr_values = sorted({max(2, int(round(params["atr_period"] * x))) for x in (0.9, 1.0, 1.1)})
    breakout_values = sorted({round(float(params["breakout_atr"]) * x, 4) for x in (0.9, 1.0, 1.1)})
    stop_values = sorted({round(float(params["stop_atr"]) * x, 4) for x in (0.9, 1.0, 1.1)})
    tp_values = sorted({round(float(params["take_profit_atr"]) * x, 4) for x in (0.9, 1.0, 1.1)})

    rows: list[dict[str, Any]] = []
    for atr_period in atr_values:
        for breakout_atr in breakout_values:
            for stop_atr in stop_values:
                for take_profit_atr in tp_values:
                    candidate = {
                        "atr_period": atr_period,
                        "breakout_atr": breakout_atr,
                        "stop_atr": stop_atr,
                        "take_profit_atr": take_profit_atr,
                    }
                    result = backtest(df, cfg, candidate)
                    rows.append(
                        {
                            **candidate,
                            **compact_metrics(result),
                            "positive": bool(result["net_profit"] > 0),
                            "within_dd_limit": bool(result["max_drawdown_pct"] <= cfg.max_drawdown_pct),
                        }
                    )
    return pd.DataFrame(rows)


def trade_log_frame(result: dict[str, Any]) -> pd.DataFrame:
    rows = [asdict(t) for t in result["trade_log"]]
    return pd.DataFrame(rows)


def choose_verdict(
    holdout: dict[str, Any],
    wf: pd.DataFrame,
    stress: pd.DataFrame,
    stability: pd.DataFrame,
    mc_holdout: dict[str, float],
    max_dd_limit: float,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    hard_fail = False

    if holdout["net_profit"] <= 0:
        hard_fail = True
        reasons.append("2025 holdout net profit is not positive")
    if holdout["profit_factor"] <= 1.0:
        hard_fail = True
        reasons.append("2025 holdout profit factor is <= 1.0")
    if holdout["max_drawdown_pct"] > max_dd_limit:
        hard_fail = True
        reasons.append("2025 holdout exceeds the configured max drawdown")
    if holdout["trades"] < 30:
        hard_fail = True
        reasons.append("2025 holdout has fewer than 30 trades")

    stress_15 = stress.loc[np.isclose(stress["cost_multiplier"], 1.5)]
    if stress_15.empty or float(stress_15.iloc[0]["net_profit"]) <= 0:
        hard_fail = True
        reasons.append("2025 fails 1.5x execution-cost stress")

    if hard_fail:
        return "REJECT", reasons

    watch = False
    wf_positive = float(wf["positive"].mean()) if not wf.empty else 0.0
    if wf_positive < 0.60:
        watch = True
        reasons.append(f"walk-forward positive-window fraction is only {wf_positive:.1%}")

    stress_20 = stress.loc[np.isclose(stress["cost_multiplier"], 2.0)]
    if stress_20.empty or float(stress_20.iloc[0]["net_profit"]) <= 0:
        watch = True
        reasons.append("2025 does not remain profitable at 2.0x execution costs")

    stability_pass = float((stability["positive"] & stability["within_dd_limit"]).mean()) if not stability.empty else 0.0
    if stability_pass < 0.60:
        watch = True
        reasons.append(f"only {stability_pass:.1%} of +/-10% parameter variants are positive and within DD limit")

    if mc_holdout["loss_probability"] > 0.10:
        watch = True
        reasons.append(f"Monte Carlo loss probability is {mc_holdout['loss_probability']:.1%}")
    if mc_holdout["p95_max_drawdown"] > max_dd_limit * 2.0:
        watch = True
        reasons.append(f"Monte Carlo 95th-percentile max DD is {mc_holdout['p95_max_drawdown']:.1%}")

    return ("WATCH" if watch else "PASS"), reasons


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Leakage-resistant MT5 validation for the fixed EURUSD H1 volatility-breakout candidate."
    )
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-01-01", help="Exclusive end date; default covers through 2025-12-31.")
    parser.add_argument("--holdout-start", default="2025-01-01")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.0025)
    parser.add_argument("--max-daily-loss", type=float, default=0.01)
    parser.add_argument("--max-drawdown", type=float, default=0.05)
    parser.add_argument("--spread-pips", type=float, default=1.2)
    parser.add_argument("--slippage-pips", type=float, default=0.2)
    parser.add_argument("--commission", type=float, default=7.0)
    parser.add_argument("--mc-runs", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", default="results/volatility_mt5_2021_2025")
    args = parser.parse_args()

    start = _parse_utc_date(args.start)
    end = _parse_utc_date(args.end)
    holdout_start = _parse_utc_date(args.holdout_start)
    if not start < holdout_start < end:
        raise ValueError("dates must satisfy start < holdout-start < end")

    cfg = make_cfg(
        initial_equity=args.initial_equity,
        risk_per_trade=args.risk_per_trade,
        max_daily_loss_pct=args.max_daily_loss,
        max_drawdown_pct=args.max_drawdown,
        spread_pips=args.spread_pips,
        slippage_pips=args.slippage_pips,
        commission_per_lot_round_turn=args.commission,
    )

    print(f"Loading REAL MT5 rates: {args.symbol} {args.timeframe} {args.start} -> {args.end} ...")
    raw = load_mt5_range(args.symbol, args.timeframe, start, end)
    validate_coverage(raw, start, end)
    development = raw[(raw.index >= pd.Timestamp(start)) & (raw.index < pd.Timestamp(holdout_start))]
    holdout_df = raw[(raw.index >= pd.Timestamp(holdout_start)) & (raw.index < pd.Timestamp(end))]
    if len(development) < 1000 or len(holdout_df) < 1000:
        raise RuntimeError("not enough bars in development or 2025 holdout period")

    params = dict(DEFAULT_PARAMS)
    development_result = backtest(development, cfg, params)
    holdout_result = backtest(holdout_df, cfg, params)
    full_result = backtest(raw, cfg, params)

    wf = walk_forward_fixed(raw, cfg, params, train_months=12, test_months=3)
    yearly = yearly_evaluation(raw, cfg, params)
    stress = cost_stress(holdout_df, cfg, params)
    stability = parameter_stability(holdout_df, cfg, params)
    mc_development = monte_carlo_trade_returns(
        trade_returns(development_result), cfg.initial_equity, args.mc_runs, cfg.max_drawdown_pct, args.seed
    )
    mc_holdout = monte_carlo_trade_returns(
        trade_returns(holdout_result), cfg.initial_equity, args.mc_runs, cfg.max_drawdown_pct, args.seed + 1
    )

    verdict, reasons = choose_verdict(
        holdout_result, wf, stress, stability, mc_holdout, cfg.max_drawdown_pct
    )

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw.to_csv(out / f"{args.symbol}_{args.timeframe}_2021_2025_mt5.csv.gz", compression="gzip", index_label="time")
    yearly.to_csv(out / "yearly_metrics.csv", index=False)
    wf.to_csv(out / "walk_forward_fixed.csv", index=False)
    stress.to_csv(out / "cost_stress_2025.csv", index=False)
    stability.to_csv(out / "parameter_stability_2025.csv", index=False)
    trade_log_frame(holdout_result).to_csv(out / "holdout_2025_trades.csv", index=False)
    full_result["equity_curve"].rename("equity").to_csv(out / "full_equity_curve.csv", index_label="time")

    summary = {
        "data": {
            "source": "MetaTrader5.copy_rates_range",
            "symbol": args.symbol,
            "timeframe": args.timeframe,
            "requested_start": start,
            "requested_end_exclusive": end,
            "bars": len(raw),
            "actual_start": raw.index.min(),
            "actual_end": raw.index.max(),
        },
        "strategy": {"name": "volatility_breakout", "params": params},
        "risk_and_costs": {
            "initial_equity": cfg.initial_equity,
            "risk_per_trade": cfg.risk_per_trade,
            "max_daily_loss_pct": cfg.max_daily_loss_pct,
            "max_drawdown_pct": cfg.max_drawdown_pct,
            "spread_pips": cfg.spread_pips,
            "slippage_pips": cfg.slippage_pips,
            "commission_per_lot_round_turn": cfg.commission_per_lot_round_turn,
        },
        "development_2021_2024": compact_metrics(development_result),
        "holdout_2025": compact_metrics(holdout_result),
        "full_2021_2025": compact_metrics(full_result),
        "walk_forward": {
            "windows": int(len(wf)),
            "positive_fraction": float(wf["positive"].mean()) if not wf.empty else 0.0,
        },
        "parameter_stability_2025": {
            "variants": int(len(stability)),
            "positive_fraction": float(stability["positive"].mean()) if not stability.empty else 0.0,
            "positive_within_dd_fraction": float((stability["positive"] & stability["within_dd_limit"]).mean()) if not stability.empty else 0.0,
        },
        "monte_carlo_development": mc_development,
        "monte_carlo_holdout": mc_holdout,
        "verdict": verdict,
        "verdict_reasons": reasons,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8"
    )

    h = summary["holdout_2025"]
    print("\n=== REAL MT5 EURUSD H1 ROBUST VALIDATION ===")
    print(f"Data            : {raw.index.min()} -> {raw.index.max()} ({len(raw):,} bars)")
    print(f"Strategy        : ATR(56), breakout 0.70 ATR, SL 0.75 ATR, TP 50 ATR")
    print(f"Costs           : spread {cfg.spread_pips:.2f} pips + slippage {cfg.slippage_pips:.2f} pips + commission ${cfg.commission_per_lot_round_turn:.2f}/lot RT")
    print(f"2025 return     : {h['return_pct'] * 100:.2f}%")
    print(f"2025 max DD     : {h['max_drawdown_pct'] * 100:.2f}%")
    print(f"2025 trades     : {h['trades']}")
    print(f"2025 win rate   : {h['win_rate'] * 100:.2f}%")
    print(f"2025 PF         : {h['profit_factor']:.2f}")
    print(f"WF positive     : {summary['walk_forward']['positive_fraction'] * 100:.1f}%")
    print(f"MC loss prob.   : {mc_holdout['loss_probability'] * 100:.1f}%")
    print(f"MC p95 max DD   : {mc_holdout['p95_max_drawdown'] * 100:.2f}%")
    print(f"VERDICT         : {verdict}")
    if reasons:
        for reason in reasons:
            print(f"  - {reason}")
    print(f"Results         : {out}")


if __name__ == "__main__":
    main()
