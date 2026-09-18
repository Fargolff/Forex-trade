from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.mt5_broker import AccountSnapshot, BrokerDeal, BrokerPosition
from src.pending_intent import (
    append_recovered_event,
    execution_halt_reason_resolvable,
    resolve_pending_order_intent,
)
from src.restart_reconcile import RestartReconcileConfig, restart_reconciliation_report


def _intent(action="ENTRY", **overrides):
    value = {
        "version": 2,
        "intent_id": "intent-abc",
        "created_at": "2026-09-18T09:00:00+00:00",
        "deal_cursor_before_submit": {"time_msc": 1000, "ticket": 10},
        "action": action,
        "strategy": "ema_trend",
        "symbol": "EURUSD",
        "side": 1 if action == "ENTRY" else -1,
        "lots": 0.10,
        "stop_loss": 1.09 if action == "ENTRY" else 0.0,
        "take_profit": 1.12 if action == "ENTRY" else 0.0,
        "position_ticket": 0 if action == "ENTRY" else 900,
        "position_id": 0 if action == "ENTRY" else 900,
        "bar_time": "2026-09-18T08:00:00+00:00",
    }
    value.update(overrides)
    return value


def _deal(*, ticket=11, order=55, position_id=900, time_msc=1_789_718_405_000, side=1, volume=0.10, entry=0):
    return BrokerDeal(
        ticket=ticket,
        order=order,
        position_id=position_id,
        time_msc=time_msc,
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


def test_exact_entry_deal_and_position_resolve_deterministically():
    result = resolve_pending_order_intent(_intent(), [_deal()], [_position()])
    assert result.resolved is True
    assert result.code == "ENTRY_FULL_FILL_PROVEN"
    assert result.order == 55
    assert result.position_id == 900
    assert result.deal_tickets == (11,)


def test_entry_partial_volume_does_not_auto_resolve():
    result = resolve_pending_order_intent(_intent(), [_deal(volume=0.04)], [_position(volume=0.04)])
    assert result.resolved is False
    assert result.code == "PENDING_INTENT_ENTRY_EVIDENCE_MISMATCH"


def test_same_millisecond_cursor_uses_ticket_tiebreaker():
    intent = _intent(deal_cursor_before_submit={"time_msc": 1_789_718_405_000, "ticket": 20})
    old = _deal(ticket=19, order=54, time_msc=1_789_718_405_000)
    new = _deal(ticket=21, order=55, time_msc=1_789_718_405_000)
    result = resolve_pending_order_intent(intent, [old, new], [_position()])
    assert result.resolved is True
    assert result.order == 55
    assert result.deal_tickets == (21,)


def test_exact_close_resolves_only_when_target_position_is_gone():
    result = resolve_pending_order_intent(
        _intent("CLOSE"),
        [_deal(side=-1, entry=1)],
        [],
    )
    assert result.resolved is True
    assert result.code == "CLOSE_FULL_EXIT_PROVEN"

    still_open = resolve_pending_order_intent(
        _intent("CLOSE"),
        [_deal(side=-1, entry=1)],
        [_position()],
    )
    assert still_open.resolved is False
    assert still_open.code == "PENDING_INTENT_POSITION_STILL_OPEN"


def test_phase16_legacy_intent_without_metadata_requires_manual_reconcile():
    legacy = {
        "action": "ENTRY",
        "strategy": "ema_trend",
        "symbol": "EURUSD",
        "side": 1,
        "lots": 0.10,
    }
    result = resolve_pending_order_intent(legacy, [_deal()], [_position()])
    assert result.resolved is False
    assert result.code == "PENDING_INTENT_METADATA_INSUFFICIENT"


def test_recovered_event_append_is_idempotent(tmp_path):
    intent = _intent()
    result = resolve_pending_order_intent(intent, [_deal()], [_position()])
    path = tmp_path / "live_events.csv"
    assert append_recovered_event(path, intent, result) is True
    assert append_recovered_event(path, intent, result) is False
    text = path.read_text(encoding="utf-8")
    assert text.count("phase17_recovered_pending_intent:intent-abc") == 1


def test_only_execution_halts_are_eligible_for_auto_unhalt():
    assert execution_halt_reason_resolvable("ops:ORDER_SUBMISSION_AMBIGUOUS") is True
    assert execution_halt_reason_resolvable("ops:PARTIAL_FILL,PENDING_ORDER_INTENT") is True
    assert execution_halt_reason_resolvable("daily_loss") is False
    assert execution_halt_reason_resolvable("ops:ACCOUNT_LOGIN_NOT_ALLOWED") is False


class FakeBroker:
    def account_snapshot(self):
        return AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            margin=100.0,
            margin_free=9900.0,
            margin_level=10000.0,
            currency="USD",
            login=123,
            margin_mode=2,
            hedging=True,
        )

    def open_positions(self, symbol=None, magic=None):
        return [_position()]

    def history_deals(self, start, end=None, symbol=None, magic=None):
        return [_deal()]


def test_restart_verify_auto_resolves_exact_pending_entry(tmp_path):
    state_path = tmp_path / "live_state.json"
    events_path = tmp_path / "live_events.csv"
    report_path = tmp_path / "restart_report.json"
    checkpoint_path = tmp_path / "restart_checkpoint.json"
    state_path.write_text(
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
                "pending_order_intent": _intent(),
                "halted": True,
                "halt_reason": "ops:ORDER_SUBMISSION_AMBIGUOUS",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    cfg = RestartReconcileConfig(
        lookback_hours=2160.0,
        report_path=str(report_path),
        checkpoint_path=str(checkpoint_path),
        event_backups=2,
        volume_tolerance=1e-8,
        require_local_entry=True,
    )
    report = restart_reconciliation_report(
        FakeBroker(),
        symbol="EURUSD",
        magic=56001,
        allowed_strategies={"ema_trend"},
        state_path=state_path,
        events_path=events_path,
        cfg=cfg,
        now=datetime(2026, 9, 18, 9, 5, tzinfo=timezone.utc),
        persist=True,
    )
    assert report["ok"] is True
    assert report["pending_intent_resolution"]["resolved"] is True
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["pending_order_intent"] is None
    assert saved["halted"] is False
    assert saved["halt_reason"] is None
    assert saved["last_deal_time_msc"] == 1_789_718_405_000
    assert saved["last_deal_ticket"] == 11
    assert "phase17_recovered_pending_intent:intent-abc" in events_path.read_text(encoding="utf-8")
