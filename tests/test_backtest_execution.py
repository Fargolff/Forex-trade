import numpy as np
import pandas as pd

from src.backtest import BacktestConfig, run_backtest


def execution_frame(signal_on_first: bool = True, signal_on_last: bool = False) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    signal = [1 if signal_on_first else 0, 0, 1 if signal_on_last else 0]
    return pd.DataFrame(
        {
            "open": [1.0000, 1.0100, 1.0150],
            "high": [1.0020, 1.0120, 1.0180],
            "low": [0.9980, 1.0080, 1.0120],
            "close": [1.0010, 1.0110, 1.0160],
            "signal": signal,
            "stop_distance": [0.0100, 0.0100, 0.0100],
            "take_profit_distance": [0.0200, 0.0200, 0.0200],
        },
        index=idx,
    )


def zero_cost_config() -> BacktestConfig:
    return BacktestConfig(
        spread_pips=0.0,
        slippage_pips=0.0,
        commission_per_lot_round_turn=0.0,
    )


def test_signal_enters_on_next_bar_open_not_signal_close():
    df = execution_frame(signal_on_first=True)
    result = run_backtest(df, zero_cost_config())

    assert result["trades"] == 1
    trade = result["trade_log"][0]
    assert trade.entry_time == df.index[1]
    assert np.isclose(trade.entry, df.iloc[1]["open"])
    assert trade.reason == "end_of_test"


def test_final_bar_signal_is_not_entered_without_next_bar_price():
    df = execution_frame(signal_on_first=False, signal_on_last=True)
    result = run_backtest(df, zero_cost_config())

    assert result["trades"] == 0
    assert np.isclose(result["final_equity"], result["initial_equity"])
