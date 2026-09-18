from datetime import datetime, timezone

import pandas as pd

from src.market_clock import WeeklyFxSessionPolicy, evaluate_market_clock, market_session_state
from src.mt5_broker import AccountSnapshot, BrokerTick, SymbolSpec, TerminalSnapshot
from src.ops import operational_report


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tick(value: str) -> BrokerTick:
    stamp = int(_utc(value).timestamp() * 1000)
    return BrokerTick(bid=1.1000, ask=1.1001, time_msc=stamp)


def test_weekend_session_is_closed_and_boundaries_are_transition():
    policy = WeeklyFxSessionPolicy(
        sunday_open_utc="22:00",
        friday_close_utc="22:00",
        transition_grace_seconds=3600,
    )
    assert market_session_state(_utc("2026-09-19T12:00:00Z"), policy)["state"] == "CLOSED"
    assert market_session_state(_utc("2026-09-18T21:30:00Z"), policy)["state"] == "TRANSITION"
    assert market_session_state(_utc("2026-09-20T21:30:00Z"), policy)["state"] == "TRANSITION"
    assert market_session_state(_utc("2026-09-21T10:00:00Z"), policy)["state"] == "OPEN"


def test_future_tick_and_future_completed_bar_are_detected():
    policy = WeeklyFxSessionPolicy(max_future_tick_seconds=5, max_future_bar_seconds=60)
    report = evaluate_market_clock(
        pd.Timestamp("2026-09-21T10:02:00Z"),
        int(_utc("2026-09-21T10:00:30Z").timestamp() * 1000),
        now=_utc("2026-09-21T10:00:00Z"),
        policy=policy,
    )
    assert report["tick_future_violation"] is True
    assert report["bar_future_violation"] is True
    assert report["tick_clock_offset_seconds"] == 30.0
    assert report["bar_clock_offset_seconds"] == 120.0


class FakeBroker:
    def __init__(self, tick: BrokerTick):
        self._tick = tick

    def terminal_snapshot(self):
        return TerminalSnapshot(connected=True, trade_allowed=True, dlls_allowed=True)

    def account_snapshot(self):
        return AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            currency="USD",
            login=123,
            margin_mode=2,
            hedging=True,
        )

    def current_tick(self, symbol):
        return self._tick

    def symbol_spec(self, symbol):
        return SymbolSpec(
            symbol=symbol,
            digits=5,
            point=0.00001,
            tick_size=0.00001,
            tick_value=1.0,
            contract_size=100000.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_allowed=True,
            filling_mode=0,
        )

    def open_positions(self, symbol=None, magic=None):
        return []


def test_weekend_suppresses_stale_market_data_only():
    broker = FakeBroker(_tick("2026-09-18T21:59:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-18T21:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        market_session_enabled=True,
        market_sunday_open_utc="22:00",
        market_friday_close_utc="22:00",
        market_transition_grace_seconds=3600,
        now=_utc("2026-09-19T12:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "CLOSED"
    assert report["staleness_suppressed"] is True
    assert "STALE_TICK" not in codes
    assert "STALE_BAR" not in codes
    assert report["status"] == "OK"


def test_open_market_keeps_stale_data_fail_closed_warning():
    broker = FakeBroker(_tick("2026-09-21T09:50:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-21T08:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=3600,
        now=_utc("2026-09-21T10:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "OPEN"
    assert "STALE_TICK" in codes
    assert "STALE_BAR" in codes
    assert report["status"] == "WARN"


def test_clock_violation_remains_critical_even_when_market_closed():
    broker = FakeBroker(_tick("2026-09-19T12:01:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-18T21:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        max_future_tick_seconds=5,
        now=_utc("2026-09-19T12:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "CLOSED"
    assert "BROKER_TICK_IN_FUTURE" in codes
    assert report["status"] == "CRITICAL"
