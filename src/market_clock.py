from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

import pandas as pd


_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm_utc(value: str) -> tuple[int, int]:
    text = str(value).strip()
    if not _HHMM.fullmatch(text):
        raise ValueError(f"UTC session time must be HH:MM, got {value!r}")
    hour, minute = text.split(":", 1)
    return int(hour), int(minute)


def _as_utc(value: pd.Timestamp | datetime) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


@dataclass(frozen=True)
class WeeklyFxSessionPolicy:
    enabled: bool = True
    sunday_open_utc: str = "22:00"
    friday_close_utc: str = "22:00"
    transition_grace_seconds: float = 3600.0
    max_future_tick_seconds: float = 5.0
    max_future_bar_seconds: float = 300.0

    def __post_init__(self) -> None:
        parse_hhmm_utc(self.sunday_open_utc)
        parse_hhmm_utc(self.friday_close_utc)
        if self.transition_grace_seconds < 0:
            raise ValueError("transition_grace_seconds cannot be negative")
        if self.max_future_tick_seconds < 0:
            raise ValueError("max_future_tick_seconds cannot be negative")
        if self.max_future_bar_seconds < 0:
            raise ValueError("max_future_bar_seconds cannot be negative")


def _week_start(current: datetime) -> datetime:
    return (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _boundary(week_start: datetime, weekday: int, hhmm: str, weeks: int = 0) -> datetime:
    hour, minute = parse_hhmm_utc(hhmm)
    return week_start + timedelta(weeks=weeks, days=weekday, hours=hour, minutes=minute)


def _nominal_market_open(current: datetime, policy: WeeklyFxSessionPolicy) -> bool:
    if not policy.enabled:
        return True
    weekday = current.weekday()  # Monday=0 ... Sunday=6
    minute_of_day = current.hour * 60 + current.minute
    friday_hour, friday_minute = parse_hhmm_utc(policy.friday_close_utc)
    sunday_hour, sunday_minute = parse_hhmm_utc(policy.sunday_open_utc)
    friday_close = friday_hour * 60 + friday_minute
    sunday_open = sunday_hour * 60 + sunday_minute

    if weekday <= 3:
        return True
    if weekday == 4:
        return minute_of_day < friday_close
    if weekday == 5:
        return False
    return minute_of_day >= sunday_open


def market_session_state(
    now: datetime | None = None,
    policy: WeeklyFxSessionPolicy | None = None,
) -> dict[str, str | float | bool | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cfg = policy or WeeklyFxSessionPolicy()

    if not cfg.enabled:
        return {
            "state": "OPEN",
            "reason": "session_filter_disabled",
            "next_transition_utc": None,
            "staleness_suppressed": False,
        }

    week = _week_start(current)
    boundaries: list[tuple[datetime, str]] = []
    for shift in (-1, 0, 1):
        boundaries.append((_boundary(week, 4, cfg.friday_close_utc, shift), "weekly_close"))
        boundaries.append((_boundary(week, 6, cfg.sunday_open_utc, shift), "weekly_open"))
    boundaries.sort(key=lambda item: item[0])

    grace = float(cfg.transition_grace_seconds)
    if grace > 0:
        near = min(boundaries, key=lambda item: abs((current - item[0]).total_seconds()))
        distance = abs((current - near[0]).total_seconds())
        if distance <= grace:
            next_boundary = next((stamp for stamp, _ in boundaries if stamp > current), None)
            return {
                "state": "TRANSITION",
                "reason": f"near_{near[1]}",
                "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
                "staleness_suppressed": True,
            }

    opened = _nominal_market_open(current, cfg)
    next_boundary = next((stamp for stamp, _ in boundaries if stamp > current), None)
    return {
        "state": "OPEN" if opened else "CLOSED",
        "reason": "weekly_session_open" if opened else "weekly_session_closed",
        "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
        "staleness_suppressed": not opened,
    }


def evaluate_market_clock(
    bar_time: pd.Timestamp | datetime,
    tick_time_msc: int,
    *,
    now: datetime | None = None,
    policy: WeeklyFxSessionPolicy | None = None,
) -> dict[str, str | float | bool | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cfg = policy or WeeklyFxSessionPolicy()
    session = market_session_state(current, cfg)

    tick_offset: float | None = None
    if int(tick_time_msc) > 0:
        tick_time = datetime.fromtimestamp(int(tick_time_msc) / 1000.0, tz=timezone.utc)
        tick_offset = (tick_time - current).total_seconds()

    bar_utc = _as_utc(bar_time)
    bar_offset = (bar_utc - current).total_seconds()
    return {
        **session,
        "tick_clock_offset_seconds": tick_offset,
        "bar_clock_offset_seconds": bar_offset,
        "tick_future_violation": bool(tick_offset is not None and tick_offset > cfg.max_future_tick_seconds),
        "bar_future_violation": bool(bar_offset > cfg.max_future_bar_seconds),
    }
