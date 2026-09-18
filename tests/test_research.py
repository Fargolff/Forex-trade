import numpy as np
import pandas as pd

from src.backtest import BacktestConfig
from src.research import run_strategy_batch


def sample_ohlc(rows: int = 500) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    close = 1.10 + np.cumsum(rng.normal(0, 0.00035, rows))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.0003
    low = np.minimum(open_, close) - 0.0003
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_batch_research_returns_ranked_rows():
    names = ["ema_trend", "donchian_breakout", "zscore_reversion"]
    results = run_strategy_batch(sample_ohlc(), BacktestConfig(), names)
    assert len(results) == len(names)
    assert set(results["strategy"]) == set(names)
    assert set(results["status"]) == {"ok"}
    assert "rank_score" in results.columns
