from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    side: int  # -1 short, 0 flat, +1 long
    stop_distance: float
    take_profit_distance: float


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    description: str
    defaults: dict[str, Any]
    builder: Callable[..., pd.DataFrame]


def _validate_ohlc(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    if len(df) < 10:
        raise ValueError("not enough bars")


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def _event_signal(long_condition: pd.Series, short_condition: pd.Series) -> pd.Series:
    long_condition = long_condition.fillna(False).astype(bool)
    short_condition = short_condition.fillna(False).astype(bool)
    long_event = long_condition & ~long_condition.shift(1, fill_value=False)
    short_event = short_condition & ~short_condition.shift(1, fill_value=False)
    signal = pd.Series(0, index=long_condition.index, dtype=int)
    signal.loc[long_event & ~short_event] = 1
    signal.loc[short_event & ~long_event] = -1
    return signal


def _risk_columns(
    out: pd.DataFrame,
    atr_period: int,
    stop_atr: float,
    take_profit_atr: float,
) -> pd.DataFrame:
    if atr_period < 2:
        raise ValueError("atr_period must be >= 2")
    if stop_atr <= 0 or take_profit_atr <= 0:
        raise ValueError("ATR risk multiples must be positive")
    out["atr"] = atr(out, atr_period)
    out["stop_distance"] = out["atr"] * float(stop_atr)
    out["take_profit_distance"] = out["atr"] * float(take_profit_atr)
    return out


def ema_trend_signals(
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 50,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    if fast >= slow:
        raise ValueError("fast EMA must be smaller than slow EMA")
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=slow, adjust=False).mean()
    out["signal"] = _event_signal(out["ema_fast"] > out["ema_slow"], out["ema_fast"] < out["ema_slow"])
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def sma_trend_signals(
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 100,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    if fast >= slow:
        raise ValueError("fast SMA must be smaller than slow SMA")
    out = df.copy()
    out["sma_fast"] = out["close"].rolling(fast).mean()
    out["sma_slow"] = out["close"].rolling(slow).mean()
    out["signal"] = _event_signal(out["sma_fast"] > out["sma_slow"], out["sma_fast"] < out["sma_slow"])
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def donchian_breakout_signals(
    df: pd.DataFrame,
    lookback: int = 55,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 4.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    out["channel_high"] = out["high"].rolling(lookback).max().shift(1)
    out["channel_low"] = out["low"].rolling(lookback).min().shift(1)
    out["signal"] = _event_signal(out["close"] > out["channel_high"], out["close"] < out["channel_low"])
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def bollinger_mean_reversion_signals(
    df: pd.DataFrame,
    period: int = 20,
    z_entry: float = 2.0,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 1.5,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    out["bb_mid"] = out["close"].rolling(period).mean()
    std = out["close"].rolling(period).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + z_entry * std
    out["bb_lower"] = out["bb_mid"] - z_entry * std
    out["signal"] = _event_signal(out["close"] < out["bb_lower"], out["close"] > out["bb_upper"])
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def rsi_mean_reversion_signals(
    df: pd.DataFrame,
    period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 2.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    if not 0 < oversold < overbought < 100:
        raise ValueError("RSI thresholds must satisfy 0 < oversold < overbought < 100")
    out = df.copy()
    out["rsi"] = rsi(out["close"], period)
    out["signal"] = _event_signal(out["rsi"] < oversold, out["rsi"] > overbought)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def momentum_signals(
    df: pd.DataFrame,
    lookback: int = 24,
    threshold: float = 0.002,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    out["momentum"] = out["close"].pct_change(lookback)
    out["signal"] = _event_signal(out["momentum"] > threshold, out["momentum"] < -threshold)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def macd_trend_signals(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    if fast >= slow:
        raise ValueError("MACD fast must be smaller than slow")
    out = df.copy()
    ema_fast = out["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = out["close"].ewm(span=slow, adjust=False).mean()
    out["macd"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd"].ewm(span=signal_period, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["signal"] = _event_signal(out["macd_hist"] > 0, out["macd_hist"] < 0)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def ema_pullback_signals(
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 100,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    if fast >= slow:
        raise ValueError("fast EMA must be smaller than slow EMA")
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=slow, adjust=False).mean()
    prev_close = out["close"].shift(1)
    prev_fast = out["ema_fast"].shift(1)
    uptrend = out["ema_fast"] > out["ema_slow"]
    downtrend = out["ema_fast"] < out["ema_slow"]
    long_condition = uptrend & (prev_close <= prev_fast) & (out["close"] > out["ema_fast"])
    short_condition = downtrend & (prev_close >= prev_fast) & (out["close"] < out["ema_fast"])
    out["signal"] = _event_signal(long_condition, short_condition)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def volatility_breakout_signals(
    df: pd.DataFrame,
    breakout_atr: float = 1.0,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 3.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = _risk_columns(df.copy(), atr_period, stop_atr, take_profit_atr)
    previous_close = out["close"].shift(1)
    long_condition = out["close"] > previous_close + breakout_atr * out["atr"].shift(1)
    short_condition = out["close"] < previous_close - breakout_atr * out["atr"].shift(1)
    out["signal"] = _event_signal(long_condition, short_condition)
    return out


def zscore_mean_reversion_signals(
    df: pd.DataFrame,
    period: int = 48,
    entry_z: float = 2.0,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 1.5,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    mean = out["close"].rolling(period).mean()
    std = out["close"].rolling(period).std(ddof=0).replace(0.0, np.nan)
    out["zscore"] = (out["close"] - mean) / std
    out["signal"] = _event_signal(out["zscore"] < -entry_z, out["zscore"] > entry_z)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def trend_filtered_breakout_signals(
    df: pd.DataFrame,
    ema_period: int = 100,
    lookback: int = 40,
    atr_period: int = 14,
    stop_atr: float = 2.0,
    take_profit_atr: float = 4.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    out["ema_filter"] = out["close"].ewm(span=ema_period, adjust=False).mean()
    out["channel_high"] = out["high"].rolling(lookback).max().shift(1)
    out["channel_low"] = out["low"].rolling(lookback).min().shift(1)
    long_condition = (out["close"] > out["channel_high"]) & (out["close"] > out["ema_filter"])
    short_condition = (out["close"] < out["channel_low"]) & (out["close"] < out["ema_filter"])
    out["signal"] = _event_signal(long_condition, short_condition)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def stochastic_range_reversion_signals(
    df: pd.DataFrame,
    period: int = 20,
    oversold: float = 10.0,
    overbought: float = 90.0,
    atr_period: int = 14,
    stop_atr: float = 1.5,
    take_profit_atr: float = 1.5,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    rolling_low = out["low"].rolling(period).min()
    rolling_high = out["high"].rolling(period).max()
    width = (rolling_high - rolling_low).replace(0.0, np.nan)
    out["range_position"] = 100.0 * (out["close"] - rolling_low) / width
    out["signal"] = _event_signal(out["range_position"] < oversold, out["range_position"] > overbought)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


def long_term_momentum_signals(
    df: pd.DataFrame,
    lookback: int = 120,
    smoothing: int = 10,
    atr_period: int = 14,
    stop_atr: float = 2.5,
    take_profit_atr: float = 5.0,
) -> pd.DataFrame:
    _validate_ohlc(df)
    out = df.copy()
    raw = out["close"].pct_change(lookback)
    out["lt_momentum"] = raw.rolling(smoothing).mean()
    out["signal"] = _event_signal(out["lt_momentum"] > 0, out["lt_momentum"] < 0)
    return _risk_columns(out, atr_period, stop_atr, take_profit_atr)


STRATEGY_CATALOG: dict[str, StrategySpec] = {
    "ema_trend": StrategySpec("ema_trend", "trend", "Fast/slow EMA crossover.", {"fast": 20, "slow": 50, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 3.0}, ema_trend_signals),
    "sma_trend": StrategySpec("sma_trend", "trend", "Fast/slow SMA crossover.", {"fast": 20, "slow": 100, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 3.0}, sma_trend_signals),
    "donchian_breakout": StrategySpec("donchian_breakout", "breakout", "Break prior rolling price channel.", {"lookback": 55, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 4.0}, donchian_breakout_signals),
    "bollinger_reversion": StrategySpec("bollinger_reversion", "mean_reversion", "Fade moves outside Bollinger-style bands.", {"period": 20, "z_entry": 2.0, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 1.5}, bollinger_mean_reversion_signals),
    "rsi_reversion": StrategySpec("rsi_reversion", "mean_reversion", "Fade RSI extremes.", {"period": 14, "oversold": 30.0, "overbought": 70.0, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 2.0}, rsi_mean_reversion_signals),
    "momentum": StrategySpec("momentum", "momentum", "Trade direction of medium-horizon return.", {"lookback": 24, "threshold": 0.002, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 3.0}, momentum_signals),
    "macd_trend": StrategySpec("macd_trend", "trend", "MACD histogram zero-cross trend signal.", {"fast": 12, "slow": 26, "signal_period": 9, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 3.0}, macd_trend_signals),
    "ema_pullback": StrategySpec("ema_pullback", "trend_pullback", "Enter pullbacks in the direction of a slower EMA trend.", {"fast": 20, "slow": 100, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 3.0}, ema_pullback_signals),
    "volatility_breakout": StrategySpec("volatility_breakout", "breakout", "One-bar ATR expansion breakout.", {"breakout_atr": 1.0, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 3.0}, volatility_breakout_signals),
    "zscore_reversion": StrategySpec("zscore_reversion", "mean_reversion", "Fade standardized price deviations from rolling mean.", {"period": 48, "entry_z": 2.0, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 1.5}, zscore_mean_reversion_signals),
    "trend_breakout": StrategySpec("trend_breakout", "trend_breakout", "Donchian breakout filtered by long-term EMA direction.", {"ema_period": 100, "lookback": 40, "atr_period": 14, "stop_atr": 2.0, "take_profit_atr": 4.0}, trend_filtered_breakout_signals),
    "range_reversion": StrategySpec("range_reversion", "mean_reversion", "Fade extremes of rolling high-low range position.", {"period": 20, "oversold": 10.0, "overbought": 90.0, "atr_period": 14, "stop_atr": 1.5, "take_profit_atr": 1.5}, stochastic_range_reversion_signals),
    "long_term_momentum": StrategySpec("long_term_momentum", "momentum", "Slow time-series momentum sign with smoothing.", {"lookback": 120, "smoothing": 10, "atr_period": 14, "stop_atr": 2.5, "take_profit_atr": 5.0}, long_term_momentum_signals),
}


def available_strategies() -> list[str]:
    return sorted(STRATEGY_CATALOG)


def strategy_spec(name: str) -> StrategySpec:
    try:
        return STRATEGY_CATALOG[name]
    except KeyError as exc:
        raise ValueError(f"unknown strategy {name!r}; choose from {available_strategies()}") from exc


def build_signals(df: pd.DataFrame, name: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    spec = strategy_spec(name)
    merged = dict(spec.defaults)
    if params:
        merged.update(params)
    return spec.builder(df, **merged)
