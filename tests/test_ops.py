from datetime import datetime, timezone

import pandas as pd

from src.mt5_broker import (
    AccountSnapshot,
    BrokerDeal,
    BrokerPosition,
    BrokerTick,
    SymbolSpec,
    TerminalSnapshot,
)
from src.ops import (
    HeartbeatStore,
    heartbeat_payload,
    market_freshness_incidents,
    operational_report,
    position_integrity_incidents,
    recent_managed_deals,
)


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="EURUSD",
        digits=5,
        point=0.00001,
        tick_size=0.00001,
        tick_value=1.0,
        contract_size=100000.0,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
        trade_allowed=True,
        filling_mode=1,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        balance=10000.0,
        equity=9990.0,
        margin=50.0,
        margin_free=9940.0,
        margin_level=19980.0,
        currency="USD",
        login=123,
        margin_mode=2,
        hedging=True,
    )


def _position(ticket: int, strategy: str = "ema_trend", *, stop_loss=1.09, take_profit=1.12) -> BrokerPosition:
    return BrokerPosition(
        ticket=ticket,
        symbol="EURUSD",
        side=1,
        volume=0.01,
        price_open=1.10,
        stop_loss=stop_loss,
        take_profit=take_profit,
        magic=56001,
        comment=f"fat:{strategy}",
    )


class FakeBroker:
    def __init__(self, now: datetime, positions=None, deals=None):
        self._now = now
        self._positions = positions or []
        self._deals = deals or []

    def terminal_snapshot(self):
        return TerminalSnapshot(connected=True, trade_allowed=True, dlls_allowed=False)

    def account_snapshot(self):
        return _account()

    def current_tick(self, symbol):
        return BrokerTick(bid=1.10000, ask=1.10008, time_msc=int(self._now.timestamp() * 1000))

    def symbol_spec(self, symbol):
        return _spec()

    def open_positions(self, symbol=None, magic=None):
        return [p for p in self._positions if magic is None or p.magic == magic]

    def history_deals(self, start, end, symbol=None, magic=None):
        return [
            d
            for d in self._deals
            if (symbol is None or d.symbol == symbol) and (magic is None or d.magic == magic)
        ]


def test_market_freshness_detects_stale_tick_and_bar():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    tick = BrokerTick(bid=1.1, ask=1.1001, time_msc=int(datetime(2026, 9, 18, 9, 59, tzinfo=timezone.utc).timestamp() * 1000))
    incidents = market_freshness_incidents(
        pd.Timestamp("2026-09-18T07:00:00Z"),
        tick,
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        now=now,
    )
    assert {item.code for item in incidents} == {"STALE_TICK", "STALE_BAR"}


def test_position_integrity_detects_duplicate_and_missing_exit():
    positions = [
        _position(1),
        _position(2, stop_loss=0.0),
    ]
    incidents = position_integrity_incidents(positions, {"ema_trend"})
    codes = {item.code for item in incidents}
    assert "DUPLICATE_STRATEGY_POSITION" in codes
    assert "MISSING_PROTECTIVE_EXIT" in codes
    assert all(item.severity == "CRITICAL" for item in incidents)


def test_operational_report_is_ok_for_fresh_consistent_state():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    broker = FakeBroker(now, positions=[_position(1)])
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-18T09:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        now=now,
    )
    assert report["status"] == "OK"
    assert report["ok"] is True
    assert report["incidents"] == []


def test_recent_managed_deals_advances_from_cursor():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    deals = [
        BrokerDeal(1, 11, 21, 1000, "EURUSD", 1, 0.01, 1.1, 0.0, -0.1, 0.0, 56001, "fat:ema_trend", 0),
        BrokerDeal(2, 12, 22, 2000, "EURUSD", -1, 0.01, 1.2, 5.0, -0.1, -0.2, 56001, "fat:ema_trend", 1),
    ]
    broker = FakeBroker(now, deals=deals)
    result = recent_managed_deals(
        broker,
        "EURUSD",
        56001,
        after_time_msc=1000,
        lookback_hours=72,
        now=now,
    )
    assert [item.ticket for item in result] == [2]


def test_heartbeat_store_writes_atomic_json(tmp_path):
    target = tmp_path / "heartbeat.json"
    store = HeartbeatStore(target)
    payload = heartbeat_payload(
        status="OK",
        symbol="EURUSD",
        last_bar_time="2026-09-18T09:00:00+00:00",
        account=_account(),
        positions=[_position(1)],
        incidents=[],
        spread_pips=0.8,
        tick_age_seconds_value=1.0,
        bar_age_seconds_value=3600.0,
        last_deal_time_msc=1234,
    )
    store.write(payload)
    text = target.read_text(encoding="utf-8")
    assert '"status": "OK"' in text
    assert '"last_deal_time_msc": 1234' in text
    assert not (tmp_path / "heartbeat.json.tmp").exists()
