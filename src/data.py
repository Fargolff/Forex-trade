from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


REQUIRED_OHLC = ("open", "high", "low", "close")


@dataclass(frozen=True)
class DataKey:
    symbol: str
    timeframe: str

    @property
    def filename(self) -> str:
        safe_symbol = self.symbol.replace("/", "_").replace(" ", "_")
        return f"{safe_symbol}_{self.timeframe.upper()}.csv.gz"


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        raise ValueError("market data is empty")

    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    missing = set(REQUIRED_OHLC).difference(out.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")

    if not isinstance(out.index, pd.DatetimeIndex):
        if "time" in out.columns:
            out["time"] = pd.to_datetime(out["time"], utc=True)
            out = out.set_index("time")
        else:
            raise ValueError("market data requires DatetimeIndex or time column")

    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")

    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    for col in REQUIRED_OHLC:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=list(REQUIRED_OHLC))

    invalid = (
        (out["high"] < out[["open", "close", "low"]].max(axis=1))
        | (out["low"] > out[["open", "close", "high"]].min(axis=1))
        | (out["high"] < out["low"])
    )
    if invalid.any():
        raise ValueError(f"invalid OHLC geometry in {int(invalid.sum())} bars")

    return out


def merge_bars(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    left = normalize_ohlc(existing)
    right = normalize_ohlc(incoming)
    merged = pd.concat([left, right], axis=0)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return normalize_ohlc(merged)


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    src = normalize_ohlc(df)
    agg: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for optional in ("tick_volume", "real_volume", "volume"):
        if optional in src.columns:
            agg[optional] = "sum"
    if "spread" in src.columns:
        agg["spread"] = "mean"
    out = src.resample(rule, label="right", closed="right").agg(agg)
    return normalize_ohlc(out.dropna(subset=list(REQUIRED_OHLC)))


class MarketDataStore:
    def __init__(self, root: str | Path = "data") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self.root / DataKey(symbol, timeframe).filename

    def save(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Path:
        normalized = normalize_ohlc(df)
        path = self.path_for(symbol, timeframe)
        normalized.to_csv(path, compression="gzip", index_label="time")
        return path

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self.path_for(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path, compression="gzip", parse_dates=["time"])
        return normalize_ohlc(df)

    def upsert(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Path:
        path = self.path_for(symbol, timeframe)
        if path.exists():
            combined = merge_bars(self.load(symbol, timeframe), df)
        else:
            combined = normalize_ohlc(df)
        return self.save(symbol, timeframe, combined)

    def coverage(self, symbol: str, timeframe: str) -> dict[str, object]:
        df = self.load(symbol, timeframe)
        return {
            "rows": len(df),
            "start": df.index.min(),
            "end": df.index.max(),
            "duplicates": int(df.index.duplicated().sum()),
        }
