import numpy as np
import pandas as pd

from src.strategy import available_strategies, build_signals


def sample_ohlc(rows: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    close = 1.10 + np.cumsum(rng.normal(0, 0.0004, rows))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.0001, 0.0005, rows)
    low = np.minimum(open_, close) - rng.uniform(0.0001, 0.0005, rows)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_catalog_has_multiple_strategy_families():
    names = available_strategies()
    assert len(names) >= 10
    assert "ema_trend" in names
    assert "donchian_breakout" in names
    assert "zscore_reversion" in names


def test_every_strategy_returns_standard_signal_contract():
    df = sample_ohlc()
    for name in available_strategies():
        out = build_signals(df, name)
        assert len(out) == len(df)
        assert {"signal", "stop_distance", "take_profit_distance"}.issubset(out.columns)
        assert set(out["signal"].dropna().astype(int).unique()).issubset({-1, 0, 1})
        valid_stop = out["stop_distance"].dropna()
        valid_tp = out["take_profit_distance"].dropna()
        assert (valid_stop > 0).all()
        assert (valid_tp > 0).all()
