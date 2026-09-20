from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FinancingSchedule:
    """Synthetic research/paper financing assumptions in account-currency cash.

    long/short values are cash per lot for a 1x rollover. Positive values are
    credits and negative values are costs. weekday_multipliers use Python's
    Monday=0 .. Sunday=6 convention; the default models a Wednesday triple swap.
    """

    enabled: bool = False
    long_cash_per_lot_rollover: float = 0.0
    short_cash_per_lot_rollover: float = 0.0
    rollover_hour_utc: int = 22
    weekday_multipliers: tuple[float, ...] = (1.0, 1.0, 3.0, 1.0, 1.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        multipliers = tuple(float(value) for value in self.weekday_multipliers)
        object.__setattr__(self, "weekday_multipliers", multipliers)
        if not 0 <= int(self.rollover_hour_utc) <= 23:
            raise ValueError("financing.rollover_hour_utc must be in [0,23]")
        if len(multipliers) != 7:
            raise ValueError("financing.weekday_multipliers must contain exactly 7 values")
        if any((not math.isfinite(value)) or value < 0 for value in multipliers):
            raise ValueError("financing.weekday_multipliers must be finite and non-negative")
        for value in (self.long_cash_per_lot_rollover, self.short_cash_per_lot_rollover):
            if not math.isfinite(float(value)):
                raise ValueError("financing cash assumptions must be finite")


@dataclass(frozen=True)
class FinancingAccrual:
    cash: float = 0.0
    rollovers: int = 0
    weighted_rollovers: float = 0.0


def _as_utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def financing_between(
    start: Any,
    end: Any,
    *,
    side: int,
    lots: float,
    schedule: FinancingSchedule,
) -> FinancingAccrual:
    """Return synthetic financing for rollovers in the half-open holding interval.

    A rollover is charged when ``start < rollover_timestamp <= end``. This avoids
    charging a position immediately when it is opened exactly at the configured
    rollover timestamp while still charging positions held across that boundary.
    """
    if side not in (-1, 1):
        raise ValueError("side must be -1 or 1")
    lots = float(lots)
    if lots < 0 or not math.isfinite(lots):
        raise ValueError("lots must be finite and non-negative")
    if not schedule.enabled or lots == 0:
        return FinancingAccrual()

    start_ts = _as_utc(start)
    end_ts = _as_utc(end)
    if end_ts <= start_ts:
        return FinancingAccrual()

    rate = float(
        schedule.long_cash_per_lot_rollover
        if side > 0
        else schedule.short_cash_per_lot_rollover
    )
    cash = 0.0
    rollovers = 0
    weighted = 0.0
    day = start_ts.date()
    end_day = end_ts.date()
    while day <= end_day:
        rollover = pd.Timestamp(
            year=day.year,
            month=day.month,
            day=day.day,
            hour=int(schedule.rollover_hour_utc),
            tz="UTC",
        )
        if start_ts < rollover <= end_ts:
            multiplier = float(schedule.weekday_multipliers[rollover.weekday()])
            if multiplier > 0:
                rollovers += 1
                weighted += multiplier
                cash += rate * lots * multiplier
        day += timedelta(days=1)

    return FinancingAccrual(
        cash=float(cash),
        rollovers=rollovers,
        weighted_rollovers=float(weighted),
    )
