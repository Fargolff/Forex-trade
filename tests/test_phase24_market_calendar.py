from datetime import datetime

import pytest

from src.market_calendar import evaluate_market_calendar, load_market_calendar
from src.market_clock import WeeklyFxSessionPolicy, market_session_state


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write(tmp_path, text: str):
    path = tmp_path / "market_calendar.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_full_day_holiday_closes_an_otherwise_open_weekday(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-25":
    closed: true
    reason: Christmas Day
''',
    )
    policy = WeeklyFxSessionPolicy(
        transition_grace_seconds=0,
        market_calendar_path=str(path),
    )
    state = market_session_state(_utc("2026-12-25T12:00:00Z"), policy)
    assert state["state"] == "CLOSED"
    assert state["staleness_suppressed"] is True
    assert state["calendar_loaded"] is True
    assert state["calendar_applied"] is True
    assert "Christmas Day" in str(state["reason"])


def test_early_close_and_delayed_open_are_restriction_only(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-24":
    close_utc: "18:00"
    reason: Christmas Eve
  "2026-12-28":
    open_utc: "09:00"
    reason: Delayed reopen
''',
    )
    policy = WeeklyFxSessionPolicy(transition_grace_seconds=0, market_calendar_path=str(path))
    assert market_session_state(_utc("2026-12-24T17:59:00Z"), policy)["state"] == "OPEN"
    assert market_session_state(_utc("2026-12-24T18:00:00Z"), policy)["state"] == "CLOSED"
    assert market_session_state(_utc("2026-12-28T08:59:00Z"), policy)["state"] == "CLOSED"
    assert market_session_state(_utc("2026-12-28T09:00:00Z"), policy)["state"] == "OPEN"


def test_calendar_cannot_expand_weekend_session(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-26":
    open_utc: "00:00"
    close_utc: "24:00"
    reason: Attempted Saturday open
''',
    )
    policy = WeeklyFxSessionPolicy(transition_grace_seconds=0, market_calendar_path=str(path))
    state = market_session_state(_utc("2026-12-26T12:00:00Z"), policy)
    assert state["state"] == "CLOSED"
    assert "weekly_session_closed" in str(state["reason"])


def test_temporary_closure_blocks_trading_and_reports_end(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1
closures:
  - start_utc: "2026-12-29T10:00:00Z"
    end_utc: "2026-12-29T12:00:00Z"
    reason: Broker maintenance
''',
    )
    calendar = load_market_calendar(path, required=True)
    assert calendar is not None
    state = evaluate_market_calendar(
        _utc("2026-12-29T11:00:00Z"),
        calendar,
        transition_grace_seconds=0,
    )
    assert state["state"] == "CLOSED"
    assert state["next_transition_utc"] == "2026-12-29T12:00:00+00:00"
    assert "Broker maintenance" in str(state["reason"])


def test_transition_grace_blocks_near_early_close(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-24":
    close_utc: "18:00"
    reason: Early close
''',
    )
    policy = WeeklyFxSessionPolicy(
        transition_grace_seconds=1800,
        market_calendar_path=str(path),
    )
    assert market_session_state(_utc("2026-12-24T17:45:00Z"), policy)["state"] == "TRANSITION"


def test_missing_calendar_is_optional_but_required_mode_fails(tmp_path, monkeypatch):
    missing = tmp_path / "missing.yaml"
    optional = WeeklyFxSessionPolicy(transition_grace_seconds=0, market_calendar_path=str(missing))
    state = market_session_state(_utc("2026-12-21T10:00:00Z"), optional)
    assert state["state"] == "OPEN"
    assert state["calendar_loaded"] is False

    monkeypatch.setenv("FOREX_MARKET_CALENDAR_PATH", str(missing))
    monkeypatch.setenv("FOREX_REQUIRE_MARKET_CALENDAR", "1")
    with pytest.raises(FileNotFoundError):
        market_session_state(_utc("2026-12-21T10:00:00Z"), WeeklyFxSessionPolicy())


def test_environment_path_overrides_policy_path(tmp_path, monkeypatch):
    configured = tmp_path / "configured.yaml"
    actual = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-22":
    closed: true
    reason: Environment calendar
''',
    )
    monkeypatch.setenv("FOREX_MARKET_CALENDAR_PATH", str(actual))
    policy = WeeklyFxSessionPolicy(transition_grace_seconds=0, market_calendar_path=str(configured))
    state = market_session_state(_utc("2026-12-22T10:00:00Z"), policy)
    assert state["state"] == "CLOSED"
    assert state["calendar_path"] == str(actual)


def test_invalid_calendar_fails_closed_instead_of_being_ignored(tmp_path):
    path = _write(
        tmp_path,
        '''version: 1

days:
  "2026-12-24":
    open_utc: "20:00"
    close_utc: "18:00"
''',
    )
    policy = WeeklyFxSessionPolicy(transition_grace_seconds=0, market_calendar_path=str(path))
    with pytest.raises(ValueError, match="open_utc < close_utc"):
        market_session_state(_utc("2026-12-24T12:00:00Z"), policy)
