from datetime import datetime, timezone
import json

from src.mt5_broker import AccountSnapshot, BrokerDeal, BrokerPosition, BrokerWorkingOrder
from src.order_recovery import assess_pending_order_recovery
from src.restart_reconcile import RestartReconcileConfig, restart_reconciliation_report


def _intent(action="ENTRY", **overrides):
    value = {
        "version": 3,
        "intent_id": "phase18-intent",
        "created_at": "2026-09-18T09:00:00+00:00",
        "deal_cursor_before_submit": {"time_msc": 1000, "ticket": 10},
        "action": action,
        "strategy": "ema_trend",
        "symbol": "EURUSD",
        "magic": 56001,
        "side": 1 if action == "ENTRY" else -1,
        "lots": 0.10,
        "stop_loss": 1.09 if action == "ENTRY" else 0.0,
        "take_profit": 1.12 if action == "ENTRY" else 0.0,
        "position_ticket": 0 if action == "ENTRY" else 900,
        "position_id": 0 if action == "ENTRY" else 900,
        "broker_order_ticket": 55,
        "broker_receipt_status": "PLACED",
        "bar_time": "2026-09-18T08:00:00+00:00",
    }
    value.update(overrides)
    return value


def _order(*, ticket=55, state="PLACED", side=1, initial=0.10, current=0.10):
    return BrokerWorkingOrder(
        ticket=ticket,
        time_setup_msc=1_789_722_001_000,
        time_done_msc=0,
        symbol="EURUSD",
        side=side,
        volume_initial=initial,
        volume_current=current,
        price_open=1.1001,
        stop_loss=1.09,
        take_profit=1.12,
        magic=56001,
        comment="fat:ema_trend",
        state=state,
    )


def _position(*, volume=0.10, side=1, ticket=900, identifier=900):
    return BrokerPosition(
        ticket=ticket,
        symbol="EURUSD",
        side=side,
        volume=volume,
        price_open=1.1001,
        stop_loss=1.09,
        take_profit=1.12,
        magic=56001,
        comment="fat:ema_trend",
        identifier=identifier,
    )


def _deal(*, ticket=11, order=55, position_id=900, side=1, volume=0.10, entry=0):
    return BrokerDeal(
        ticket=ticket,
        order=order,
        position_id=position_id,
        time_msc=1_789_722_005_000,
        symbol="EURUSD",
        side=side,
        volume=volume,
        price=1.1001,
        profit=0.0,
        commission=-0.2,
        swap=0.0,
        magic=56001,
        comment="fat:ema_trend",
        entry=entry,
    )


def test_exact_working_order_is_waiting_and_never_resendable():
    result = assess_pending_order_recovery(_intent(), [], [], [_order()], [])
    assert result.waiting is True
    assert result.resolved is False
    assert result.code == "PENDING_INTENT_ORDER_WORKING"
    assert result.order == 55


def test_working_order_partial_is_fail_closed():
    result = assess_pending_order_recovery(_intent(), [], [], [_order(state="PARTIAL", current=0.04)], [])
    assert result.resolved is False
    assert result.waiting is False
    assert result.code == "WORKING_ORDER_PARTIAL"


def test_cancelled_entry_with_zero_deals_and_no_position_resolves_no_fill():
    result = assess_pending_order_recovery(_intent(), [], [], [], [_order(state="CANCELED", current=0.0)])
    assert result.resolved is True
    assert result.effect == "NO_FILL"
    assert result.code == "ORDER_TERMINAL_NO_FILL_PROVEN"


def test_cancelled_close_requires_target_position_unchanged():
    intent = _intent("CLOSE")
    result = assess_pending_order_recovery(
        intent,
        [],
        [_position()],
        [],
        [_order(state="CANCELED", side=-1, current=0.0)],
    )
    assert result.resolved is True
    assert result.effect == "NO_FILL"

    missing = assess_pending_order_recovery(
        intent,
        [],
        [],
        [],
        [_order(state="CANCELED", side=-1, current=0.0)],
    )
    assert missing.resolved is False
    assert missing.code == "TERMINAL_NO_FILL_POSITION_MISMATCH"


def test_terminal_order_with_any_linked_deal_never_auto_clears():
    result = assess_pending_order_recovery(
        _intent(),
        [_deal(volume=0.02)],
        [_position(volume=0.02)],
        [],
        [_order(state="CANCELED", current=0.0)],
    )
    assert result.resolved is False
    assert result.code in {"PENDING_INTENT_ENTRY_EVIDENCE_MISMATCH", "TERMINAL_ORDER_HAS_DEALS"}


def test_exact_full_fill_must_match_persisted_order_ticket():
    filled = assess_pending_order_recovery(_intent(), [_deal()], [_position()], [], [_order(state="FILLED", current=0.0)])
    assert filled.resolved is True
    assert filled.effect == "FILLED"

    wrong = assess_pending_order_recovery(
        _intent(broker_order_ticket=99),
        [_deal(order=55)],
        [_position()],
        [],
        [_order(ticket=99, state="FILLED", current=0.0)],
    )
    assert wrong.resolved is False
    assert wrong.code == "BROKER_ORDER_TICKET_MISMATCH"


class FakeBroker:
    def __init__(self, *, working=(), history=(), deals=(), positions=()):
        self._working = list(working)
        self._history = list(history)
        self._deals = list(deals)
        self._positions = list(positions)

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

    def open_positions(self, symbol=None, magic=None):
        return list(self._positions)

    def history_deals(self, start, end=None, symbol=None, magic=None):
        return list(self._deals)

    def open_orders(self, symbol=None, magic=None):
        return list(self._working)

    def history_orders(self, start, end=None, symbol=None, magic=None):
        return list(self._history)


def _cfg(tmp_path):
    return RestartReconcileConfig(
        lookback_hours=2160.0,
        report_path=str(tmp_path / "restart_report.json"),
        checkpoint_path=str(tmp_path / "restart_checkpoint.json"),
        event_backups=2,
        volume_tolerance=1e-8,
        require_local_entry=True,
    )


def _write_state(path, intent, halted=True):
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "peak_equity": 10000.0,
                "start_of_day_equity": 10000.0,
                "current_day": "2026-09-18",
                "last_bar_time": "2026-09-18T08:00:00+00:00",
                "last_deal_time_msc": 1000,
                "last_deal_ticket": 10,
                "last_incident_fingerprint": None,
                "pending_order_intent": intent,
                "halted": halted,
                "halt_reason": "ops:ORDER_ACCEPTED_UNCONFIRMED" if halted else None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_restart_reports_exact_working_order_without_mutating_intent(tmp_path):
    state = tmp_path / "live_state.json"
    events = tmp_path / "live_events.csv"
    _write_state(state, _intent())
    report = restart_reconciliation_report(
        FakeBroker(working=[_order()]),
        symbol="EURUSD",
        magic=56001,
        allowed_strategies={"ema_trend"},
        state_path=state,
        events_path=events,
        cfg=_cfg(tmp_path),
        now=datetime(2026, 9, 18, 9, 5, tzinfo=timezone.utc),
        persist=True,
    )
    assert report["ok"] is False
    assert report["pending_intent_resolution"]["waiting"] is True
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["pending_order_intent"]["broker_order_ticket"] == 55


def test_restart_auto_clears_terminal_no_fill_and_execution_halt(tmp_path):
    state = tmp_path / "live_state.json"
    events = tmp_path / "live_events.csv"
    _write_state(state, _intent())
    report = restart_reconciliation_report(
        FakeBroker(history=[_order(state="CANCELED", current=0.0)]),
        symbol="EURUSD",
        magic=56001,
        allowed_strategies={"ema_trend"},
        state_path=state,
        events_path=events,
        cfg=_cfg(tmp_path),
        now=datetime(2026, 9, 18, 9, 5, tzinfo=timezone.utc),
        persist=True,
    )
    assert report["ok"] is True
    assert report["pending_intent_resolution"]["effect"] == "NO_FILL"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["pending_order_intent"] is None
    assert saved["halted"] is False
    assert "phase18_recovered_no_fill:phase18-intent" in events.read_text(encoding="utf-8")


def test_untracked_working_order_is_critical(tmp_path):
    state = tmp_path / "live_state.json"
    events = tmp_path / "live_events.csv"
    _write_state(state, None, halted=False)
    report = restart_reconciliation_report(
        FakeBroker(working=[_order(ticket=77)]),
        symbol="EURUSD",
        magic=56001,
        allowed_strategies={"ema_trend"},
        state_path=state,
        events_path=events,
        cfg=_cfg(tmp_path),
        now=datetime(2026, 9, 18, 9, 5, tzinfo=timezone.utc),
        persist=False,
    )
    codes = {item["code"] for item in report["incidents"]}
    assert "UNTRACKED_WORKING_ORDER" in codes
