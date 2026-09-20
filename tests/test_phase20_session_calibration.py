from datetime import datetime, timezone

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
