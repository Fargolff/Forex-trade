import numpy as np
import pandas as pd

from src.backtest import BacktestConfig
from src.validation import (
    ValidationConfig,
    chronological_split,
    monte_carlo_trade_paths,
    parameter_neighborhood,
    validate_strategy,
    validation_summary,
)


def sample_ohlc(rows: int = 700) -> pd.DataFrame:
    rng = np.random.default_rng(123)
    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    regime = np.arange(rows) // 140
    drift = np.where(regime % 2 == 0, 0.00001, -0.000006)
    close = 1.10 + np.cumsum(drift + rng.normal(0, 0.0004, rows))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.0003
    low = np.minimum(open_, close) - 0.0003
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_chronological_split_is_disjoint_and_ordered():
    df = sample_ohlc(500)
    split = chronological_split(df, train_fraction=0.60, validation_fraction=0.20)

    assert len(split.train) == 300
    assert len(split.validation) == 100
    assert len(split.out_of_sample) == 100
    assert split.train.index[-1] < split.validation.index[0]
    assert split.validation.index[-1] < split.out_of_sample.index[0]


def test_parameter_neighborhood_keeps_valid_ema_relationship():
    candidates = parameter_neighborhood("ema_trend", perturbation=0.20)

    assert len(candidates) >= 3
    assert any(c["fast"] == 20 and c["slow"] == 50 for c in candidates)
    assert all(c["fast"] < c["slow"] for c in candidates)
    assert all(c["stop_atr"] == 2.0 for c in candidates)
    assert all(c["take_profit_atr"] == 3.0 for c in candidates)


def test_monte_carlo_is_reproducible_and_bounded():
    pnl = [120.0, -70.0, 40.0, -20.0, 90.0, -35.0]
    first = monte_carlo_trade_paths(pnl, 10_000.0, runs=200, seed=7)
    second = monte_carlo_trade_paths(pnl, 10_000.0, runs=200, seed=7)

    assert first == second
    assert 0.0 <= first["loss_probability"] <= 1.0
    assert 0.0 <= first["ruin_probability"] <= 1.0
    assert first["p05_final_equity"] <= first["median_final_equity"] <= first["p95_final_equity"]


def test_validation_report_has_oos_walk_forward_and_monte_carlo_sections():
    cfg = ValidationConfig(
        walk_forward_train_bars=240,
        walk_forward_test_bars=80,
        walk_forward_step_bars=80,
        monte_carlo_runs=50,
        min_oos_trades=0,
        seed=5,
    )
    report = validate_strategy(
        sample_ohlc(),
        BacktestConfig(max_drawdown_pct=0.50),
        "ema_trend",
        cfg=cfg,
    )
    summary = validation_summary(report)

    assert report["verdict"] in {"PASS", "WATCH", "REJECT"}
    assert "out_of_sample" in report
    assert "walk_forward" in report
    assert "monte_carlo" in report
    assert "parameter_stability_fraction" in report
    assert summary["strategy"] == "ema_trend"
    assert 0.0 <= summary["monte_carlo_ruin_probability"] <= 1.0
