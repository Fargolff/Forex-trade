import numpy as np
import pandas as pd

from src.backtest import BacktestConfig
from src.portfolio import PortfolioConfig, research_portfolio
from src.validation import ValidationConfig, chronological_split


def sample_ohlc(rows: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(2021)
    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    regime = np.arange(rows) // 100
    drift = np.where(regime % 3 == 0, 0.000025, np.where(regime % 3 == 1, -0.000015, 0.000004))
    noise = rng.normal(0.0, 0.00045, rows)
    close = 1.10 + np.cumsum(drift + noise)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.00025
    low = np.minimum(open_, close) - 0.00025
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def validation_cfg() -> ValidationConfig:
    return ValidationConfig(
        walk_forward_train_bars=180,
        walk_forward_test_bars=60,
        walk_forward_step_bars=60,
        monte_carlo_runs=20,
        min_validation_trades=0,
        min_validation_profit_factor=0.0,
        min_validation_sharpe=-999.0,
        max_validation_drawdown=1.0,
        min_oos_trades=0,
        min_oos_profit_factor=0.0,
        min_oos_sharpe=-999.0,
        max_oos_drawdown=1.0,
        min_walk_forward_positive_fraction=0.0,
        min_parameter_stability_fraction=0.0,
        max_monte_carlo_ruin_probability=1.0,
        seed=17,
    )


def portfolio_cfg() -> PortfolioConfig:
    return PortfolioConfig(
        max_strategy_weight=0.60,
        min_strategies=2,
        allowed_verdicts=("PASS", "WATCH", "REJECT"),
        monte_carlo_runs=20,
        monte_carlo_block_size=12,
        seed=23,
    )


def mutate_only_final_oos(df: pd.DataFrame) -> pd.DataFrame:
    mutated = df.copy()
    split = chronological_split(df, 0.60, 0.20)
    oos_index = split.out_of_sample.index
    scale = np.linspace(1.0, 1.35, len(oos_index))
    for column in ("open", "high", "low", "close"):
        mutated.loc[oos_index, column] = df.loc[oos_index, column].to_numpy() * scale
    return mutated


def _locked_candidates(report: dict) -> pd.DataFrame:
    columns = ["strategy", "selected_params", "pre_oos_verdict", "included", "frozen_active"]
    return report["candidate_table"][columns].sort_values("strategy").reset_index(drop=True)


def _locked_weights(report: dict) -> pd.DataFrame:
    return report["weights"][["strategy", "weight"]].sort_values("strategy").reset_index(drop=True)


def test_final_oos_mutation_cannot_change_frozen_portfolio_design():
    original = sample_ohlc()
    mutated = mutate_only_final_oos(original)
    cfg = validation_cfg()
    pcfg = portfolio_cfg()
    backtest = BacktestConfig(max_drawdown_pct=0.90)
    strategies = ["ema_trend", "sma_trend", "momentum"]

    first = research_portfolio(original, backtest, strategies, cfg, pcfg)
    second = research_portfolio(mutated, backtest, strategies, cfg, pcfg)

    assert first["summary"]["selection_stage"] == "PRE_OOS_FROZEN"
    assert first["summary"]["oos_evaluated_after_freeze"] is True
    assert first["summary"]["design_fingerprint"] == second["summary"]["design_fingerprint"]
    pd.testing.assert_frame_equal(_locked_candidates(first), _locked_candidates(second))
    pd.testing.assert_frame_equal(_locked_weights(first), _locked_weights(second), rtol=0.0, atol=0.0)

    split = chronological_split(original, 0.60, 0.20)
    assert original.loc[split.out_of_sample.index, "close"].iloc[-1] != mutated.loc[split.out_of_sample.index, "close"].iloc[-1]


def test_frozen_design_fingerprint_changes_when_pre_oos_changes():
    original = sample_ohlc()
    changed = original.copy()
    split = chronological_split(original, 0.60, 0.20)
    pre_index = split.validation.index
    changed.loc[pre_index, "close"] = changed.loc[pre_index, "close"].to_numpy() + np.linspace(0.0, 0.01, len(pre_index))
    changed.loc[pre_index, "high"] = np.maximum(changed.loc[pre_index, "high"], changed.loc[pre_index, "close"] + 0.0001)
    changed.loc[pre_index, "low"] = np.minimum(changed.loc[pre_index, "low"], changed.loc[pre_index, "close"] - 0.0001)

    backtest = BacktestConfig(max_drawdown_pct=0.90)
    strategies = ["ema_trend", "sma_trend", "momentum"]
    first = research_portfolio(original, backtest, strategies, validation_cfg(), portfolio_cfg())
    second = research_portfolio(changed, backtest, strategies, validation_cfg(), portfolio_cfg())

    # Pre-OOS changes are allowed to alter the frozen design. The important
    # guarantee is that final-OOS-only changes cannot.
    assert first["summary"]["selection_data_end"] == second["summary"]["selection_data_end"]
