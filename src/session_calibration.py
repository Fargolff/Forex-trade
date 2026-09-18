from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from typing import Any

from .market_clock import WeeklyFxSessionPolicy, market_session_state, parse_hhmm_utc


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _minute_of_day(value: datetime) -> int:
    return int(value.hour) * 60 + int(value.minute)


def _hhmm(minutes: int) -> str:
    value = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{value // 60:02d}:{value % 60:02d}"


def _policy_minute(value: str) -> int:
    hour, minute = parse_hhmm_utc(value)
    return hour * 60 + minute


def _week_key(value: datetime) -> str:
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


@dataclass(frozen=True)
class SessionCalibrationConfig:
    enabled: bool = True
    state_path: str = "runtime/session_calibration.json"
    min_boundary_samples: int = 3
    safety_buffer_minutes: int = 15
    max_narrowing_minutes: int = 180
    sample_retention_weeks: int = 12
    clock_window_size: int = 12
    clock_min_samples: int = 5
    max_persistent_clock_offset_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.min_boundary_samples < 1:
            raise ValueError("min_boundary_samples must be >= 1")
        if self.safety_buffer_minutes < 0:
            raise ValueError("safety_buffer_minutes cannot be negative")
        if self.max_narrowing_minutes < 0:
            raise ValueError("max_narrowing_minutes cannot be negative")
        if self.sample_retention_weeks < self.min_boundary_samples:
            raise ValueError("sample_retention_weeks must be >= min_boundary_samples")
        if self.clock_window_size < 1:
            raise ValueError("clock_window_size must be >= 1")
        if not 1 <= self.clock_min_samples <= self.clock_window_size:
            raise ValueError("clock_min_samples must be in [1, clock_window_size]")
        if self.max_persistent_clock_offset_seconds <= 0:
            raise ValueError("max_persistent_clock_offset_seconds must be positive")


@dataclass
class SessionCalibrationState:
    version: int = 1
    last_tick_msc: int = 0
    last_observed_at: str | None = None
    sunday_open_by_week: dict[str, int] = field(default_factory=dict)
    friday_last_tick_by_week: dict[str, int] = field(default_factory=dict)
    friday_close_by_week: dict[str, int] = field(default_factory=dict)
    clock_offsets_seconds: list[float] = field(default_factory=list)


class SessionCalibrationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> SessionCalibrationState:
        if not self.path.exists():
            return SessionCalibrationState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        allowed = {field.name for field in SessionCalibrationState.__dataclass_fields__.values()}
        clean = {key: value for key, value in raw.items() if key in allowed}
        return SessionCalibrationState(**clean)

    def save(self, state: SessionCalibrationState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)


def _trim_week_map(values: dict[str, int], keep: int) -> dict[str, int]:
    keys = sorted(values)[-keep:]
    return {key: int(values[key]) for key in keys}


def observe_market_timing(
    state: SessionCalibrationState,
    *,
    tick_time_msc: int,
    static_policy: WeeklyFxSessionPolicy,
    config: SessionCalibrationConfig,
    now: datetime | None = None,
) -> SessionCalibrationState:
    if not config.enabled:
        return state
    current = _utc(now)
    state.last_observed_at = current.isoformat()
    tick_msc = int(tick_time_msc)
    if tick_msc <= 0:
        return state

    static_open = _policy_minute(static_policy.sunday_open_utc)
    static_close = _policy_minute(static_policy.friday_close_utc)
    current_week = _week_key(current)

    # Once the static Friday close has passed, freeze the last observed Friday
    # tick as that week's close evidence. Only evidence inside the configured
    # narrowing window is accepted.
    current_minute = _minute_of_day(current)
    if current.weekday() >= 5 or (current.weekday() == 4 and current_minute >= static_close):
        candidate = state.friday_last_tick_by_week.get(current_week)
        if candidate is not None and current_week not in state.friday_close_by_week:
            lower = static_close - config.max_narrowing_minutes
            if lower <= int(candidate) <= static_close:
                state.friday_close_by_week[current_week] = int(candidate)

    if tick_msc <= int(state.last_tick_msc):
        return state

    tick_time = datetime.fromtimestamp(tick_msc / 1000.0, tz=timezone.utc)
    tick_week = _week_key(tick_time)
    tick_minute = _minute_of_day(tick_time)

    # Conservative session evidence: later Sunday opens and earlier Friday
    # closes may narrow the configured envelope, but can never expand it.
    if tick_time.weekday() == 6 and tick_week not in state.sunday_open_by_week:
        if static_open <= tick_minute <= static_open + config.max_narrowing_minutes:
            state.sunday_open_by_week[tick_week] = tick_minute
    if tick_time.weekday() == 4:
        lower = static_close - config.max_narrowing_minutes
        if lower <= tick_minute <= static_close:
            state.friday_last_tick_by_week[tick_week] = tick_minute

    # Persist a rolling signed offset only when broker time is actually moving
    # and the static session says the market should be open. A persistent large
    # median can mean host-clock drift or broker/feed latency; either is unsafe.
    static_state = market_session_state(current, static_policy)
    if static_state["state"] == "OPEN":
        offset = (tick_time - current).total_seconds()
        state.clock_offsets_seconds.append(float(offset))
        state.clock_offsets_seconds = state.clock_offsets_seconds[-config.clock_window_size :]

    state.last_tick_msc = tick_msc
    state.sunday_open_by_week = _trim_week_map(state.sunday_open_by_week, config.sample_retention_weeks)
    state.friday_last_tick_by_week = _trim_week_map(state.friday_last_tick_by_week, config.sample_retention_weeks)
    state.friday_close_by_week = _trim_week_map(state.friday_close_by_week, config.sample_retention_weeks)
    return state


def effective_session_policy(
    static_policy: WeeklyFxSessionPolicy,
    state: SessionCalibrationState,
    config: SessionCalibrationConfig,
) -> tuple[WeeklyFxSessionPolicy, dict[str, Any]]:
    static_open = _policy_minute(static_policy.sunday_open_utc)
    static_close = _policy_minute(static_policy.friday_close_utc)
    open_values = [int(value) for value in state.sunday_open_by_week.values()]
    close_values = [int(value) for value in state.friday_close_by_week.values()]

    effective_open = static_open
    effective_close = static_close
    open_median: float | None = None
    close_median: float | None = None

    if config.enabled and len(open_values) >= config.min_boundary_samples:
        open_median = float(median(open_values))
        proposed = int(round(open_median)) + config.safety_buffer_minutes
        effective_open = min(static_open + config.max_narrowing_minutes, max(static_open, proposed))
    if config.enabled and len(close_values) >= config.min_boundary_samples:
        close_median = float(median(close_values))
        proposed = int(round(close_median)) - config.safety_buffer_minutes
        effective_close = max(static_close - config.max_narrowing_minutes, min(static_close, proposed))

    policy = WeeklyFxSessionPolicy(
        enabled=static_policy.enabled,
        sunday_open_utc=_hhmm(effective_open),
        friday_close_utc=_hhmm(effective_close),
        transition_grace_seconds=static_policy.transition_grace_seconds,
        max_future_tick_seconds=static_policy.max_future_tick_seconds,
        max_future_bar_seconds=static_policy.max_future_bar_seconds,
    )
    meta = {
        "enabled": bool(config.enabled),
        "source": "CALIBRATED" if (effective_open != static_open or effective_close != static_close) else "STATIC",
        "static_sunday_open_utc": static_policy.sunday_open_utc,
        "static_friday_close_utc": static_policy.friday_close_utc,
        "effective_sunday_open_utc": policy.sunday_open_utc,
        "effective_friday_close_utc": policy.friday_close_utc,
        "open_samples": len(open_values),
        "close_samples": len(close_values),
        "open_median_minute": open_median,
        "close_median_minute": close_median,
    }
    return policy, meta


def clock_watchdog_status(
    state: SessionCalibrationState,
    config: SessionCalibrationConfig,
) -> dict[str, Any]:
    values = [float(value) for value in state.clock_offsets_seconds[-config.clock_window_size :]]
    med = float(median(values)) if values else None
    ready = len(values) >= config.clock_min_samples
    violation = bool(ready and med is not None and abs(med) > config.max_persistent_clock_offset_seconds)
    return {
        "ready": ready,
        "samples": len(values),
        "median_offset_seconds": med,
        "threshold_seconds": float(config.max_persistent_clock_offset_seconds),
        "violation": violation,
        "meaning": "signed broker tick time minus host UTC; large persistent values may be clock drift or feed latency",
    }
