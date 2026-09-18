from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Phase 20 patch anchor missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


SESSION_CALIBRATION = r'''from __future__ import annotations

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
'''

TESTS = r'''from datetime import datetime, timezone

from src.market_clock import WeeklyFxSessionPolicy
from src.session_calibration import (
    SessionCalibrationConfig,
    SessionCalibrationState,
    SessionCalibrationStore,
    clock_watchdog_status,
    effective_session_policy,
    observe_market_timing,
)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _msc(value: str) -> int:
    return int(_utc(value).timestamp() * 1000)


def test_calibration_never_expands_static_envelope():
    state = SessionCalibrationState(
        sunday_open_by_week={"2026-W35": 1330, "2026-W36": 1340, "2026-W37": 1350},
        friday_close_by_week={"2026-W35": 1315, "2026-W36": 1320, "2026-W37": 1325},
    )
    cfg = SessionCalibrationConfig(min_boundary_samples=3, safety_buffer_minutes=10, max_narrowing_minutes=120)
    static = WeeklyFxSessionPolicy(sunday_open_utc="22:00", friday_close_utc="22:00")
    policy, meta = effective_session_policy(static, state, cfg)
    assert policy.sunday_open_utc >= static.sunday_open_utc
    assert policy.friday_close_utc <= static.friday_close_utc
    assert meta["source"] == "CALIBRATED"


def test_insufficient_evidence_keeps_static_policy():
    state = SessionCalibrationState(sunday_open_by_week={"2026-W37": 1340})
    cfg = SessionCalibrationConfig(min_boundary_samples=3)
    static = WeeklyFxSessionPolicy(sunday_open_utc="22:00", friday_close_utc="22:00")
    policy, meta = effective_session_policy(static, state, cfg)
    assert policy.sunday_open_utc == "22:00"
    assert policy.friday_close_utc == "22:00"
    assert meta["source"] == "STATIC"


def test_observation_collects_later_sunday_open_and_friday_close():
    cfg = SessionCalibrationConfig(min_boundary_samples=1, safety_buffer_minutes=0)
    static = WeeklyFxSessionPolicy(sunday_open_utc="22:00", friday_close_utc="22:00", transition_grace_seconds=0)
    state = SessionCalibrationState()
    observe_market_timing(
        state,
        tick_time_msc=_msc("2026-09-20T22:35:00Z"),
        static_policy=static,
        config=cfg,
        now=_utc("2026-09-20T22:35:01Z"),
    )
    observe_market_timing(
        state,
        tick_time_msc=_msc("2026-09-25T21:40:00Z"),
        static_policy=static,
        config=cfg,
        now=_utc("2026-09-25T21:40:01Z"),
    )
    # Finalize the Friday candidate after the static close.
    observe_market_timing(
        state,
        tick_time_msc=_msc("2026-09-25T21:40:00Z"),
        static_policy=static,
        config=cfg,
        now=_utc("2026-09-25T22:05:00Z"),
    )
    policy, meta = effective_session_policy(static, state, cfg)
    assert policy.sunday_open_utc == "22:35"
    assert policy.friday_close_utc == "21:40"
    assert meta["open_samples"] == 1
    assert meta["close_samples"] == 1


def test_persistent_clock_offset_requires_multiple_moving_tick_samples():
    cfg = SessionCalibrationConfig(clock_window_size=5, clock_min_samples=3, max_persistent_clock_offset_seconds=60)
    state = SessionCalibrationState(clock_offsets_seconds=[-90.0, -95.0])
    assert clock_watchdog_status(state, cfg)["violation"] is False
    state.clock_offsets_seconds.append(-100.0)
    result = clock_watchdog_status(state, cfg)
    assert result["ready"] is True
    assert result["violation"] is True
    assert result["median_offset_seconds"] == -95.0


def test_store_roundtrip(tmp_path):
    path = tmp_path / "session.json"
    store = SessionCalibrationStore(path)
    state = SessionCalibrationState(last_tick_msc=123, sunday_open_by_week={"2026-W37": 1330})
    store.save(state)
    restored = store.load()
    assert restored.last_tick_msc == 123
    assert restored.sunday_open_by_week == {"2026-W37": 1330}
'''

DOC = r'''# Phase 20 — Broker Session Auto-Calibration & Time-Sync Watchdog

Phase 20 learns conservative weekly-session evidence from broker tick timestamps while keeping the Phase 19 static session as a hard safety envelope.

## Safety model

Calibration may only **narrow** the configured session. It can move the effective Sunday open later or the effective Friday close earlier. It can never open trading earlier or keep trading later than the static Phase 19 boundary.

A boundary is not used until `session_calibration_min_samples` independent weekly observations exist. A safety buffer is then added inside the observed market window.

The default static envelope remains:

- Sunday open: `22:00 UTC`
- Friday close: `22:00 UTC`

## Persistent time-offset watchdog

Every new broker tick observed while the static session is OPEN contributes one signed sample:

`broker tick UTC - host UTC`

A rolling median is used. If enough moving-tick samples exist and the median absolute offset exceeds the configured threshold, live operation fails closed with `PERSISTENT_BROKER_TIME_OFFSET`.

This signal deliberately does not claim to distinguish OS clock drift from sustained broker/feed latency. Either condition makes deterministic timing unsafe and requires operator investigation.

Immediate future-timestamp checks from Phase 19 remain active independently.

## State

Calibration evidence is stored atomically in:

`runtime/session_calibration.json`

The file contains only timing evidence. It contains no credentials or trading secrets.

## Configuration

```yaml
live:
  session_calibration_enabled: true
  session_calibration_state_path: runtime/session_calibration.json
  session_calibration_min_samples: 3
  session_calibration_safety_buffer_minutes: 15
  session_calibration_max_narrowing_minutes: 180
  session_calibration_retention_weeks: 12
  clock_watchdog_window_size: 12
  clock_watchdog_min_samples: 5
  max_persistent_clock_offset_seconds: 60
```

Do not reduce the static Phase 19 safety envelope merely because calibration observes a wider broker session. Wider evidence is intentionally ignored.
'''


def apply() -> None:
    (ROOT / "src/session_calibration.py").write_text(SESSION_CALIBRATION, encoding="utf-8")
    (ROOT / "tests/test_phase20_session_calibration.py").write_text(TESTS, encoding="utf-8")
    (ROOT / "docs/phase20-session-calibration.md").write_text(DOC, encoding="utf-8")

    replace_once(
        "src/config.py",
        '    max_future_bar_seconds: float = 300.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
        '    max_future_bar_seconds: float = 300.0\n    session_calibration_enabled: bool = True\n    session_calibration_state_path: str = "runtime/session_calibration.json"\n    session_calibration_min_samples: int = 3\n    session_calibration_safety_buffer_minutes: int = 15\n    session_calibration_max_narrowing_minutes: int = 180\n    session_calibration_retention_weeks: int = 12\n    clock_watchdog_window_size: int = 12\n    clock_watchdog_min_samples: int = 5\n    max_persistent_clock_offset_seconds: float = 60.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
    )
    replace_once(
        "src/config.py",
        '    if cfg.max_future_tick_seconds < 0 or cfg.max_future_bar_seconds < 0:\n        raise ValueError("live future timestamp tolerances cannot be negative")\n',
        '    if cfg.max_future_tick_seconds < 0 or cfg.max_future_bar_seconds < 0:\n        raise ValueError("live future timestamp tolerances cannot be negative")\n    if cfg.session_calibration_min_samples < 1:\n        raise ValueError("live.session_calibration_min_samples must be >= 1")\n    if cfg.session_calibration_safety_buffer_minutes < 0 or cfg.session_calibration_max_narrowing_minutes < 0:\n        raise ValueError("live session calibration minute limits cannot be negative")\n    if cfg.session_calibration_retention_weeks < cfg.session_calibration_min_samples:\n        raise ValueError("live.session_calibration_retention_weeks must be >= min samples")\n    if cfg.clock_watchdog_window_size < 1:\n        raise ValueError("live.clock_watchdog_window_size must be >= 1")\n    if not 1 <= cfg.clock_watchdog_min_samples <= cfg.clock_watchdog_window_size:\n        raise ValueError("live.clock_watchdog_min_samples must be within the watchdog window")\n    if cfg.max_persistent_clock_offset_seconds <= 0:\n        raise ValueError("live.max_persistent_clock_offset_seconds must be positive")\n',
    )

    replace_once(
        "src/live.py",
        'from .ops import (\n',
        'from .market_clock import WeeklyFxSessionPolicy\nfrom .session_calibration import (\n    SessionCalibrationConfig,\n    SessionCalibrationStore,\n    clock_watchdog_status,\n    effective_session_policy,\n    observe_market_timing,\n)\nfrom .ops import (\n',
    )
    replace_once(
        "src/live.py",
        '    max_future_bar_seconds: float = 300.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
        '    max_future_bar_seconds: float = 300.0\n    session_calibration_enabled: bool = True\n    session_calibration_state_path: str = "runtime/session_calibration.json"\n    session_calibration_min_samples: int = 3\n    session_calibration_safety_buffer_minutes: int = 15\n    session_calibration_max_narrowing_minutes: int = 180\n    session_calibration_retention_weeks: int = 12\n    clock_watchdog_window_size: int = 12\n    clock_watchdog_min_samples: int = 5\n    max_persistent_clock_offset_seconds: float = 60.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
    )
    replace_once(
        "src/live.py",
        '        self.heartbeat = HeartbeatStore(config.heartbeat_path)\n        self.state = self.store.load()\n',
        '        self.heartbeat = HeartbeatStore(config.heartbeat_path)\n        self.session_calibration_config = SessionCalibrationConfig(\n            enabled=config.session_calibration_enabled,\n            state_path=config.session_calibration_state_path,\n            min_boundary_samples=config.session_calibration_min_samples,\n            safety_buffer_minutes=config.session_calibration_safety_buffer_minutes,\n            max_narrowing_minutes=config.session_calibration_max_narrowing_minutes,\n            sample_retention_weeks=config.session_calibration_retention_weeks,\n            clock_window_size=config.clock_watchdog_window_size,\n            clock_min_samples=config.clock_watchdog_min_samples,\n            max_persistent_clock_offset_seconds=config.max_persistent_clock_offset_seconds,\n        )\n        self.session_calibration_store = SessionCalibrationStore(config.session_calibration_state_path)\n        self.session_calibration_state = self.session_calibration_store.load()\n        self.state = self.store.load()\n',
    )

    old_report = '''    def _operational_report(self, ts: pd.Timestamp) -> dict[str, Any]:\n        return operational_report(\n            self.broker,\n            self.symbol,\n            self.config.magic,\n            set(self.strategies),\n            ts,\n            max_tick_age_seconds=self.config.max_tick_age_seconds,\n            max_bar_age_seconds=self.config.max_bar_age_seconds,\n            market_session_enabled=self.config.market_session_enabled,\n            market_sunday_open_utc=self.config.market_sunday_open_utc,\n            market_friday_close_utc=self.config.market_friday_close_utc,\n            market_transition_grace_seconds=self.config.market_transition_grace_seconds,\n            max_future_tick_seconds=self.config.max_future_tick_seconds,\n            max_future_bar_seconds=self.config.max_future_bar_seconds,\n        )\n'''
    new_report = '''    def _operational_report(self, ts: pd.Timestamp) -> dict[str, Any]:\n        static_policy = WeeklyFxSessionPolicy(\n            enabled=self.config.market_session_enabled,\n            sunday_open_utc=self.config.market_sunday_open_utc,\n            friday_close_utc=self.config.market_friday_close_utc,\n            transition_grace_seconds=self.config.market_transition_grace_seconds,\n            max_future_tick_seconds=self.config.max_future_tick_seconds,\n            max_future_bar_seconds=self.config.max_future_bar_seconds,\n        )\n        observed_tick = self.broker.current_tick(self.symbol)\n        observe_market_timing(\n            self.session_calibration_state,\n            tick_time_msc=observed_tick.time_msc,\n            static_policy=static_policy,\n            config=self.session_calibration_config,\n        )\n        self.session_calibration_store.save(self.session_calibration_state)\n        policy, calibration = effective_session_policy(\n            static_policy, self.session_calibration_state, self.session_calibration_config\n        )\n        clock_watchdog = clock_watchdog_status(self.session_calibration_state, self.session_calibration_config)\n        report = operational_report(\n            self.broker,\n            self.symbol,\n            self.config.magic,\n            set(self.strategies),\n            ts,\n            max_tick_age_seconds=self.config.max_tick_age_seconds,\n            max_bar_age_seconds=self.config.max_bar_age_seconds,\n            market_session_enabled=policy.enabled,\n            market_sunday_open_utc=policy.sunday_open_utc,\n            market_friday_close_utc=policy.friday_close_utc,\n            market_transition_grace_seconds=policy.transition_grace_seconds,\n            max_future_tick_seconds=policy.max_future_tick_seconds,\n            max_future_bar_seconds=policy.max_future_bar_seconds,\n        )\n        if clock_watchdog["violation"]:\n            report["incidents"].append(\n                Incident(\n                    "PERSISTENT_BROKER_TIME_OFFSET",\n                    "CRITICAL",\n                    f"rolling broker/host offset median {float(clock_watchdog['median_offset_seconds']):.1f}s "\n                    f"exceeds {float(clock_watchdog['threshold_seconds']):.1f}s; investigate OS clock or feed latency",\n                )\n            )\n            report["status"] = "CRITICAL"\n            report["ok"] = False\n        report["session_calibration"] = calibration\n        report["clock_watchdog"] = clock_watchdog\n        return report\n'''
    replace_once("src/live.py", old_report, new_report)

    old_hb = '''    def _write_heartbeat(self, report: dict[str, Any], status: str | None = None) -> None:\n        self.heartbeat.write(\n            heartbeat_payload(\n'''
    new_hb = '''    def _write_heartbeat(self, report: dict[str, Any], status: str | None = None) -> None:\n        payload = heartbeat_payload(\n'''
    replace_once("src/live.py", old_hb, new_hb)
    replace_once(
        "src/live.py",
        '''                last_deal_ticket=self.state.last_deal_ticket,\n            )\n        )\n\n    def _reconcile_deals''',
        '''                last_deal_ticket=self.state.last_deal_ticket,\n            )\n        payload["session_calibration"] = report.get("session_calibration")\n        payload["clock_watchdog"] = report.get("clock_watchdog")\n        self.heartbeat.write(payload)\n\n    def _reconcile_deals''',
    )

    replace_once(
        "src/main.py",
        '        max_future_bar_seconds=cfg.live.max_future_bar_seconds,\n        deal_reconcile_lookback_hours=cfg.live.deal_reconcile_lookback_hours,\n',
        '        max_future_bar_seconds=cfg.live.max_future_bar_seconds,\n        session_calibration_enabled=cfg.live.session_calibration_enabled,\n        session_calibration_state_path=cfg.live.session_calibration_state_path,\n        session_calibration_min_samples=cfg.live.session_calibration_min_samples,\n        session_calibration_safety_buffer_minutes=cfg.live.session_calibration_safety_buffer_minutes,\n        session_calibration_max_narrowing_minutes=cfg.live.session_calibration_max_narrowing_minutes,\n        session_calibration_retention_weeks=cfg.live.session_calibration_retention_weeks,\n        clock_watchdog_window_size=cfg.live.clock_watchdog_window_size,\n        clock_watchdog_min_samples=cfg.live.clock_watchdog_min_samples,\n        max_persistent_clock_offset_seconds=cfg.live.max_persistent_clock_offset_seconds,\n        deal_reconcile_lookback_hours=cfg.live.deal_reconcile_lookback_hours,\n',
    )

    replace_once(
        "config.example.yaml",
        '  max_future_bar_seconds: 300\n  deal_reconcile_lookback_hours: 72\n',
        '  max_future_bar_seconds: 300\n\n  # Phase 20 conservative broker-session calibration. Learned evidence may only\n  # narrow the Phase 19 static session; it can never expand trading hours.\n  session_calibration_enabled: true\n  session_calibration_state_path: runtime/session_calibration.json\n  session_calibration_min_samples: 3\n  session_calibration_safety_buffer_minutes: 15\n  session_calibration_max_narrowing_minutes: 180\n  session_calibration_retention_weeks: 12\n  clock_watchdog_window_size: 12\n  clock_watchdog_min_samples: 5\n  max_persistent_clock_offset_seconds: 60\n\n  deal_reconcile_lookback_hours: 72\n',
    )

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if "## Phase 20 — Broker session calibration" not in text:
        text += '''\n\n## Phase 20 — Broker session calibration\n\nPhase 20 persists conservative broker timing evidence in `runtime/session_calibration.json`. After enough independent weekly samples, the effective Sunday open may move later and the effective Friday close may move earlier, but calibration can never expand beyond the static Phase 19 envelope. A rolling moving-tick offset watchdog halts new trading on persistent broker/host time disagreement. See `docs/phase20-session-calibration.md`.\n'''
        readme.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    apply()
