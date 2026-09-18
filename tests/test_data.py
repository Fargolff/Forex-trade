import pandas as pd

from src.data import MarketDataStore, normalize_ohlc, resample_ohlc


def sample_df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=8, freq="15min", tz="UTC")
    close = [1.1000, 1.1002, 1.1001, 1.1005, 1.1007, 1.1006, 1.1008, 1.1010]
    open_ = [close[0], *close[:-1]]
    high = [max(o, c) + 0.0002 for o, c in zip(open_, close)]
    low = [min(o, c) - 0.0002 for o, c in zip(open_, close)]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_normalize_and_resample():
    df = normalize_ohlc(sample_df())
    out = resample_ohlc(df, "1h")
    assert isinstance(out.index, pd.DatetimeIndex)
    assert str(out.index.tz) == "UTC"
    assert {"open", "high", "low", "close"}.issubset(out.columns)
    assert len(out) >= 2


def test_market_data_store_round_trip(tmp_path):
    store = MarketDataStore(tmp_path)
    source = sample_df()
    path = store.save("EURUSD", "M15", source)
    assert path.exists()
    loaded = store.load("EURUSD", "M15")
    assert len(loaded) == len(source)
    assert loaded.index.is_monotonic_increasing
    assert loaded.index.tz is not None
