from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "PHASE18_WORKING_ORDER_RECOVERY"


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"Phase 18 patch anchor not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def write_once(path: str, marker: str, content: str) -> None:
    target = ROOT / path
    if target.exists() and marker in target.read_text(encoding="utf-8"):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if marker in text:
        return
    target.write_text(text.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")


ORDER_RECOVERY = r'''from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import csv
from typing import Any, Iterable

from .mt5_broker import BrokerDeal, BrokerPosition, BrokerWorkingOrder
from .pending_intent import PendingIntentResolution, resolve_pending_order_intent


PHASE18_MARKER = "PHASE18_WORKING_ORDER_RECOVERY"
TERMINAL_NO_FILL_STATES = {"CANCELED", "EXPIRED", "REJECTED"}
ACTIVE_STATES = {"STARTED", "PLACED", "REQUEST_ADD", "REQUEST_MODIFY", "REQUEST_CANCEL"}


@dataclass(frozen=True)
class PendingOrderRecovery:
    status: str
    code: str
    detail: str
    action: str = ""
    strategy: str = ""
    intent_id: str = ""
    order: int = 0
    effect: str = "UNKNOWN"
    broker_order_state: str = ""
    deal_tickets: tuple[int, ...] = ()
    position_id: int = 0
    volume: float = 0.0
    price: float = 0.0
    cursor_time_msc: int = 0
    cursor_ticket: int = 0

    @property
    def resolved(self) -> bool:
        return self.status == "RESOLVED"

    @property
    def waiting(self) -> bool:
        return self.status == "WAITING"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["deal_tickets"] = list(self.deal_tickets)
        payload["resolved"] = self.resolved
        payload["waiting"] = self.waiting
        return payload

    def as_phase17_resolution(self) -> PendingIntentResolution:
        if not self.resolved or self.effect != "FILLED":
            raise ValueError("only FILLED recovery can be converted to a Phase 17 resolution")
        return PendingIntentResolution(
            status="RESOLVED",
            code=self.code,
            detail=self.detail,
            action=self.action,
            strategy=self.strategy,
            intent_id=self.intent_id,
            order=self.order,
            deal_tickets=self.deal_tickets,
            position_id=self.position_id,
            volume=self.volume,
            price=self.price,
            cursor_time_msc=self.cursor_time_msc,
            cursor_ticket=self.cursor_ticket,
        )


def _from_phase17(value: PendingIntentResolution) -> PendingOrderRecovery:
    return PendingOrderRecovery(
        status=value.status,
        code=value.code,
        detail=value.detail,
        action=value.action,
        strategy=value.strategy,
        intent_id=value.intent_id,
        order=value.order,
        effect="FILLED" if value.resolved else "UNKNOWN",
        deal_tickets=value.deal_tickets,
        position_id=value.position_id,
        volume=value.volume,
        price=value.price,
        cursor_time_msc=value.cursor_time_msc,
        cursor_ticket=value.cursor_ticket,
    )


def _unresolved(intent: dict[str, Any], code: str, detail: str, *, order_state: str = "") -> PendingOrderRecovery:
    return PendingOrderRecovery(
        status="UNRESOLVED",
        code=code,
        detail=detail,
        action=str(intent.get("action", "") or ""),
        strategy=str(intent.get("strategy", "") or ""),
        intent_id=str(intent.get("intent_id", "") or ""),
        order=int(intent.get("broker_order_ticket", 0) or 0),
        broker_order_state=order_state,
    )


def _strategy_from_comment(comment: str) -> str:
    return comment[4:] if comment.startswith("fat:") else ""


def _position_id(position: BrokerPosition) -> int:
    identifier = int(getattr(position, "identifier", 0) or 0)
    return identifier if identifier > 0 else int(position.ticket)


def _order_identity_error(order: BrokerWorkingOrder, intent: dict[str, Any], volume_tolerance: float) -> str | None:
    symbol = str(intent.get("symbol", "") or "")
    side = int(intent.get("side", 0) or 0)
    lots = float(intent.get("lots", 0.0) or 0.0)
    magic = int(intent.get("magic", 0) or 0)
    if order.symbol != symbol:
        return f"order symbol={order.symbol!r} intent symbol={symbol!r}"
    if int(order.side) != side:
        return f"order side={int(order.side)} intent side={side}"
    if magic > 0 and int(order.magic) != magic:
        return f"order magic={int(order.magic)} intent magic={magic}"
    if abs(float(order.volume_initial) - lots) > volume_tolerance:
        return f"order initial volume={order.volume_initial:.10f} intent lots={lots:.10f}"
    return None


def _no_fill_position_proven(intent: dict[str, Any], positions: list[BrokerPosition], volume_tolerance: float) -> tuple[bool, str]:
    action = str(intent.get("action", "") or "").upper()
    strategy = str(intent.get("strategy", "") or "")
    side = int(intent.get("side", 0) or 0)
    lots = float(intent.get("lots", 0.0) or 0.0)
    if action == "ENTRY":
        matching = [item for item in positions if _strategy_from_comment(str(item.comment)) == strategy]
        if matching:
            return False, "a managed position for the entry strategy exists, so zero-fill cannot be proven"
        return True, "no managed position for the entry strategy exists"

    target = int(intent.get("position_id", 0) or intent.get("position_ticket", 0) or 0)
    if target <= 0:
        return False, "close/flatten intent has no stable target position identifier"
    matching = [item for item in positions if _position_id(item) == target]
    if len(matching) != 1:
        return False, "target position is not present exactly once after terminal no-fill order"
    position = matching[0]
    if int(position.side) != -side:
        return False, f"target position side={int(position.side)} is inconsistent with close side={side}"
    if abs(float(position.volume) - lots) > volume_tolerance:
        return False, f"target position volume={position.volume:.10f} differs from intent lots={lots:.10f}"
    return True, "target position remains unchanged after terminal no-fill order"


def assess_pending_order_recovery(
    intent: dict[str, Any] | None,
    deals: Iterable[BrokerDeal],
    positions: Iterable[BrokerPosition],
    working_orders: Iterable[BrokerWorkingOrder] = (),
    history_orders: Iterable[BrokerWorkingOrder] = (),
    *,
    volume_tolerance: float = 1e-8,
    price_tolerance: float = 1e-8,
    clock_skew_seconds: float = 120.0,
) -> PendingOrderRecovery:
    phase17 = resolve_pending_order_intent(
        intent,
        deals,
        positions,
        volume_tolerance=volume_tolerance,
        price_tolerance=price_tolerance,
        clock_skew_seconds=clock_skew_seconds,
    )
    if not intent or not isinstance(intent, dict):
        return _from_phase17(phase17)

    ticket = int(intent.get("broker_order_ticket", 0) or 0)
    if phase17.resolved:
        if ticket > 0 and int(phase17.order) != ticket:
            return _unresolved(
                intent,
                "BROKER_ORDER_TICKET_MISMATCH",
                f"full-fill deal proof belongs to order {phase17.order}, but persisted broker order ticket is {ticket}",
            )
        return _from_phase17(phase17)

    # Phase 17 intents and ambiguous submissions without an exact broker order
    # ticket keep their previous fail-closed/manual behavior.
    if ticket <= 0:
        return _from_phase17(phase17)

    active = [item for item in working_orders if int(item.ticket) == ticket]
    historical = [item for item in history_orders if int(item.ticket) == ticket]
    if len(active) > 1 or len(historical) > 1:
        return _unresolved(intent, "BROKER_ORDER_DUPLICATE_EVIDENCE", "broker returned duplicate records for one order ticket")
    if active and historical:
        return _unresolved(
            intent,
            "BROKER_ORDER_ACTIVE_AND_HISTORY",
            "same order ticket is simultaneously present in active and history snapshots; retry reconciliation after broker state settles",
        )

    if active:
        order = active[0]
        mismatch = _order_identity_error(order, intent, volume_tolerance)
        if mismatch:
            return _unresolved(intent, "WORKING_ORDER_IDENTITY_MISMATCH", mismatch, order_state=order.state)
        if order.state == "PARTIAL" or float(order.volume_current) + volume_tolerance < float(order.volume_initial):
            return _unresolved(
                intent,
                "WORKING_ORDER_PARTIAL",
                f"working order has state={order.state} remaining={order.volume_current:.10f}/{order.volume_initial:.10f}",
                order_state=order.state,
            )
        if order.state not in ACTIVE_STATES:
            return _unresolved(
                intent,
                "WORKING_ORDER_STATE_UNKNOWN",
                f"active broker order has unexpected state {order.state!r}",
                order_state=order.state,
            )
        return PendingOrderRecovery(
            status="WAITING",
            code="PENDING_INTENT_ORDER_WORKING",
            detail=f"broker order {ticket} is still active with state={order.state}; do not resend",
            action=str(intent.get("action", "") or ""),
            strategy=str(intent.get("strategy", "") or ""),
            intent_id=str(intent.get("intent_id", "") or ""),
            order=ticket,
            effect="WORKING",
            broker_order_state=order.state,
            volume=float(order.volume_current),
            price=float(order.price_open),
        )

    if historical:
        order = historical[0]
        mismatch = _order_identity_error(order, intent, volume_tolerance)
        if mismatch:
            return _unresolved(intent, "HISTORY_ORDER_IDENTITY_MISMATCH", mismatch, order_state=order.state)
        linked_deals = [item for item in deals if int(item.order) == ticket]
        if order.state in TERMINAL_NO_FILL_STATES:
            if linked_deals:
                return _unresolved(
                    intent,
                    "TERMINAL_ORDER_HAS_DEALS",
                    f"terminal {order.state} order has {len(linked_deals)} linked deal(s); partial execution must be reconciled manually",
                    order_state=order.state,
                )
            positions_list = list(positions)
            unchanged, detail = _no_fill_position_proven(intent, positions_list, volume_tolerance)
            if not unchanged:
                return _unresolved(intent, "TERMINAL_NO_FILL_POSITION_MISMATCH", detail, order_state=order.state)
            return PendingOrderRecovery(
                status="RESOLVED",
                code="ORDER_TERMINAL_NO_FILL_PROVEN",
                detail=f"broker order {ticket} ended {order.state} with no linked deals and unchanged position state: {detail}",
                action=str(intent.get("action", "") or ""),
                strategy=str(intent.get("strategy", "") or ""),
                intent_id=str(intent.get("intent_id", "") or ""),
                order=ticket,
                effect="NO_FILL",
                broker_order_state=order.state,
            )
        if order.state == "FILLED":
            return _unresolved(
                intent,
                "HISTORY_ORDER_FILLED_WITHOUT_FULL_DEAL_PROOF",
                "broker order history says FILLED but exact full deal/position proof is incomplete",
                order_state=order.state,
            )
        if order.state == "PARTIAL":
            return _unresolved(
                intent,
                "HISTORY_ORDER_PARTIAL",
                "broker order history is PARTIAL; never auto-clear or resend",
                order_state=order.state,
            )
        return _unresolved(
            intent,
            "HISTORY_ORDER_STATE_UNKNOWN",
            f"historical broker order has unsupported state {order.state!r}",
            order_state=order.state,
        )

    return _unresolved(
        intent,
        "BROKER_ORDER_EVIDENCE_NOT_FOUND",
        f"persisted broker order ticket {ticket} is absent from active orders and order history",
    )


def append_no_fill_recovered_event(
    path: str | Path,
    intent: dict[str, Any],
    recovery: PendingOrderRecovery,
) -> bool:
    if not recovery.resolved or recovery.effect != "NO_FILL":
        raise ValueError("recovery must be a resolved NO_FILL outcome")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    needle = f"phase18_recovered_no_fill:{recovery.intent_id}"
    for candidate in sorted(target.parent.glob(target.name + "*")):
        if not candidate.is_file():
            continue
        try:
            if needle in candidate.read_text(encoding="utf-8"):
                return False
        except OSError:
            continue

    columns = [
        "time", "event", "strategy", "side", "lots", "price",
        "stop_loss", "take_profit", "spread_pips", "ticket", "reason",
    ]
    row = {
        "time": str(intent.get("created_at", "") or ""),
        "event": "RECOVERY_NO_FILL",
        "strategy": recovery.strategy,
        "side": int(intent.get("side", 0) or 0),
        "lots": float(intent.get("lots", 0.0) or 0.0),
        "price": "",
        "stop_loss": "",
        "take_profit": "",
        "spread_pips": "",
        "ticket": recovery.order,
        "reason": f"{needle};state={recovery.broker_order_state};code={recovery.code}",
    }
    exists = target.exists()
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    return True
'''


TESTS = r'''from datetime import datetime, timezone
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
'''


DOC = r'''# Phase 18 — Working-Order & Recovery-State Reconciliation

`PHASE18_WORKING_ORDER_RECOVERY`

Phase 18 closes the gap between an MT5 submission being accepted and a broker deal becoming available.

## Broker evidence

The MT5 adapter now exposes current working orders and historical orders. Every record carries the exact order ticket, side, initial/remaining volume, setup/done timestamps, symbol, magic, protection prices, comment and normalized broker state.

When `order_send` returns an order ticket, live state persists that ticket and the normalized receipt before interpreting `FILLED`, `PARTIAL` or `PLACED`. A crash after receipt persistence therefore leaves deterministic evidence for restart reconciliation.

## Recovery rules

Recovery never guesses an order identity. Phase 18 only applies order-state automation when a persisted exact broker order ticket exists.

- **Working order:** matching `STARTED`, `PLACED` or request state remains fail-closed with `PENDING_INTENT_ORDER_WORKING`. The intent is kept and the order is never resent.
- **Partial evidence:** partial state, reduced remaining volume, terminal order with linked deals, or mismatched identity remains CRITICAL and requires reconciliation.
- **Full fill:** Phase 17 deal/position proof still controls filled recovery, and the proven deal order must equal the persisted broker order ticket.
- **Terminal no-fill:** `CANCELED`, `EXPIRED` or `REJECTED` can clear an intent only when there are zero linked deals and current position state proves no execution effect. A dedicated idempotent `RECOVERY_NO_FILL` audit row is written.
- **Missing order evidence:** if a persisted broker order ticket is absent from both active and historical order snapshots, recovery remains fail-closed.

## Orphan order guard

Restart reconciliation treats any managed working order that is not the exact ticket of the current pending intent as `UNTRACKED_WORKING_ORDER` CRITICAL. This prevents a restarted process from trading while an old broker order can still execute later.

## Non-goals

Phase 18 does not cancel orders, resend orders, repair positions, resize exposure, or flatten automatically. `order_send=None` still has no deterministic order ticket and remains a manual-reconciliation case.
'''


README_ADD = r'''## Phase 18 — Working-Order & Recovery-State Reconciliation

`PHASE18_WORKING_ORDER_RECOVERY`

Phase 18 tracks MT5 active/history orders by exact broker order ticket, persists receipt evidence before interpreting `PLACED`, prevents resends while an order is still working, detects orphan managed orders, and can auto-clear only a broker-proven terminal **no-fill** outcome. Partial/mismatched/missing evidence remains fail-closed. See `docs/phase18-working-order-recovery.md`.
'''


def patch_broker() -> None:
    insert_class = '''\n\n@dataclass(frozen=True)\nclass BrokerWorkingOrder:\n    ticket: int\n    time_setup_msc: int\n    time_done_msc: int\n    symbol: str\n    side: int\n    volume_initial: float\n    volume_current: float\n    price_open: float\n    stop_loss: float\n    take_profit: float\n    magic: int\n    comment: str\n    state: str\n'''
    replace_once(
        "src/mt5_broker.py",
        '''    entry: int\n\n\nclass BrokerOrderRejected(RuntimeError):''',
        '''    entry: int''' + insert_class + '''\n\nclass BrokerOrderRejected(RuntimeError):''',
    )

    methods = r'''
    def _order_state_name(self, state: int) -> str:
        mapping = {
            int(getattr(mt5, "ORDER_STATE_STARTED", -101)): "STARTED",
            int(getattr(mt5, "ORDER_STATE_PLACED", -102)): "PLACED",
            int(getattr(mt5, "ORDER_STATE_CANCELED", -103)): "CANCELED",
            int(getattr(mt5, "ORDER_STATE_PARTIAL", -104)): "PARTIAL",
            int(getattr(mt5, "ORDER_STATE_FILLED", -105)): "FILLED",
            int(getattr(mt5, "ORDER_STATE_REJECTED", -106)): "REJECTED",
            int(getattr(mt5, "ORDER_STATE_EXPIRED", -107)): "EXPIRED",
            int(getattr(mt5, "ORDER_STATE_REQUEST_ADD", -108)): "REQUEST_ADD",
            int(getattr(mt5, "ORDER_STATE_REQUEST_MODIFY", -109)): "REQUEST_MODIFY",
            int(getattr(mt5, "ORDER_STATE_REQUEST_CANCEL", -110)): "REQUEST_CANCEL",
        }
        return mapping.get(int(state), f"UNKNOWN:{int(state)}")

    def _broker_order_side(self, order_type: int) -> int:
        buy_types = {
            int(getattr(mt5, "ORDER_TYPE_BUY", -201)),
            int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT", -202)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP", -203)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", -204)),
        }
        sell_types = {
            int(getattr(mt5, "ORDER_TYPE_SELL", -211)),
            int(getattr(mt5, "ORDER_TYPE_SELL_LIMIT", -212)),
            int(getattr(mt5, "ORDER_TYPE_SELL_STOP", -213)),
            int(getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", -214)),
        }
        if int(order_type) in buy_types:
            return 1
        if int(order_type) in sell_types:
            return -1
        return 0

    def _map_broker_order(self, order: Any) -> BrokerWorkingOrder:
        setup_msc = int(getattr(order, "time_setup_msc", 0) or 0)
        if setup_msc <= 0:
            setup_msc = int(getattr(order, "time_setup", 0) or 0) * 1000
        done_msc = int(getattr(order, "time_done_msc", 0) or 0)
        if done_msc <= 0:
            done_msc = int(getattr(order, "time_done", 0) or 0) * 1000
        return BrokerWorkingOrder(
            ticket=int(getattr(order, "ticket", 0) or 0),
            time_setup_msc=setup_msc,
            time_done_msc=done_msc,
            symbol=str(getattr(order, "symbol", "") or ""),
            side=self._broker_order_side(int(getattr(order, "type", -1))),
            volume_initial=float(getattr(order, "volume_initial", 0.0) or 0.0),
            volume_current=float(getattr(order, "volume_current", 0.0) or 0.0),
            price_open=float(getattr(order, "price_open", 0.0) or 0.0),
            stop_loss=float(getattr(order, "sl", 0.0) or 0.0),
            take_profit=float(getattr(order, "tp", 0.0) or 0.0),
            magic=int(getattr(order, "magic", 0) or 0),
            comment=str(getattr(order, "comment", "") or ""),
            state=self._order_state_name(int(getattr(order, "state", -1))),
        )

    def open_orders(self, symbol: str | None = None, magic: int | None = None) -> list[BrokerWorkingOrder]:
        raw = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if raw is None:
            raise RuntimeError(f"orders_get failed: {mt5.last_error()}")
        out: list[BrokerWorkingOrder] = []
        for order in raw:
            item = self._map_broker_order(order)
            if magic is not None and item.magic != magic:
                continue
            out.append(item)
        return sorted(out, key=lambda item: (item.time_setup_msc, item.ticket))

    def history_orders(
        self,
        start: datetime,
        end: datetime | None = None,
        symbol: str | None = None,
        magic: int | None = None,
    ) -> list[BrokerWorkingOrder]:
        finish = end or datetime.now(timezone.utc)
        raw = mt5.history_orders_get(start, finish)
        if raw is None:
            raise RuntimeError(f"history_orders_get failed: {mt5.last_error()}")
        out: list[BrokerWorkingOrder] = []
        for order in raw:
            item = self._map_broker_order(order)
            if symbol is not None and item.symbol != symbol:
                continue
            if magic is not None and item.magic != magic:
                continue
            out.append(item)
        return sorted(out, key=lambda item: (item.time_done_msc or item.time_setup_msc, item.ticket))
'''
    replace_once(
        "src/mt5_broker.py",
        '''        return sorted(out, key=lambda item: (item.time_msc, item.ticket))\n\n    def _order_type(self, side: int) -> int:''',
        '''        return sorted(out, key=lambda item: (item.time_msc, item.ticket))\n''' + methods + '''\n    def _order_type(self, side: int) -> int:''',
    )


def patch_live() -> None:
    replace_once(
        "src/live.py",
        '''            "version": 2,\n            "intent_id": uuid.uuid4().hex,''',
        '''            "version": 3,\n            "intent_id": uuid.uuid4().hex,''',
    )
    replace_once(
        "src/live.py",
        '''            "symbol": self.symbol,\n            "side": int(side),''',
        '''            "symbol": self.symbol,\n            "magic": int(self.config.magic),\n            "side": int(side),''',
    )
    receipt_method = r'''
    def _persist_execution_receipt(self, receipt: ExecutionReceipt) -> None:
        if self.state.pending_order_intent is None:
            return
        intent = dict(self.state.pending_order_intent)
        intent["broker_receipt_status"] = str(receipt.status)
        intent["broker_retcode"] = int(receipt.retcode)
        intent["broker_order_ticket"] = int(receipt.order)
        intent["broker_deal_ticket"] = int(receipt.deal)
        intent["broker_requested_volume"] = float(receipt.requested_volume)
        intent["broker_filled_volume"] = float(receipt.filled_volume)
        intent["broker_receipt_price"] = float(receipt.price)
        self.state.pending_order_intent = intent
        # Persist broker acknowledgement before interpreting PLACED/PARTIAL.
        # A crash after this save can be reconciled by exact broker order ticket.
        self.store.save(self.state)
'''
    replace_once(
        "src/live.py",
        '''    def _clear_order_intent(self) -> None:\n''',
        receipt_method + '''\n    def _clear_order_intent(self) -> None:\n''',
    )
    replace_once(
        "src/live.py",
        '''            issue = execution_receipt_issue(\n                receipt, position.volume,''',
        '''            self._persist_execution_receipt(receipt)\n            issue = execution_receipt_issue(\n                receipt, position.volume,''',
    )
    replace_once(
        "src/live.py",
        '''                close_issue = execution_receipt_issue(\n                    close_receipt, existing.volume,''',
        '''                self._persist_execution_receipt(close_receipt)\n                close_issue = execution_receipt_issue(\n                    close_receipt, existing.volume,''',
    )
    replace_once(
        "src/live.py",
        '''            issue = execution_receipt_issue(receipt, lots, max(spec.volume_step / 2.0, 1e-12))''',
        '''            self._persist_execution_receipt(receipt)\n            issue = execution_receipt_issue(receipt, lots, max(spec.volume_step / 2.0, 1e-12))''',
    )


def patch_restart() -> None:
    replace_once(
        "src/restart_reconcile.py",
        '''from .production import atomic_write_json\n''',
        '''from .order_recovery import assess_pending_order_recovery, append_no_fill_recovered_event\nfrom .production import atomic_write_json\n''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''def _snapshot_position(position: BrokerPosition) -> dict[str, Any]:\n''',
        '''def _snapshot_order(order: Any) -> dict[str, Any]:\n    return {\n        "ticket": int(order.ticket),\n        "time_setup_msc": int(order.time_setup_msc),\n        "time_done_msc": int(order.time_done_msc),\n        "symbol": str(order.symbol),\n        "side": int(order.side),\n        "volume_initial": float(order.volume_initial),\n        "volume_current": float(order.volume_current),\n        "price_open": float(order.price_open),\n        "stop_loss": float(order.stop_loss),\n        "take_profit": float(order.take_profit),\n        "magic": int(order.magic),\n        "comment": str(order.comment),\n        "state": str(order.state),\n    }\n\n\ndef _snapshot_position(position: BrokerPosition) -> dict[str, Any]:\n''',
    )
    old = '''    pending_resolution = resolve_pending_order_intent(\n        state.get("pending_order_intent") if state else None,\n        deals,\n        positions,\n        volume_tolerance=cfg.volume_tolerance,\n    )\n'''
    new = '''    open_orders_fn = getattr(broker, "open_orders", None)\n    history_orders_fn = getattr(broker, "history_orders", None)\n    working_orders = list(open_orders_fn(symbol=symbol, magic=magic)) if callable(open_orders_fn) else []\n    history_orders = (\n        list(history_orders_fn(start, current, symbol=symbol, magic=magic))\n        if callable(history_orders_fn)\n        else []\n    )\n\n    pending_intent = state.get("pending_order_intent") if state else None\n    pending_recovery = assess_pending_order_recovery(\n        pending_intent,\n        deals,\n        positions,\n        working_orders,\n        history_orders,\n        volume_tolerance=cfg.volume_tolerance,\n    )\n\n    pending_ticket = 0\n    if isinstance(pending_intent, dict):\n        try:\n            pending_ticket = int(pending_intent.get("broker_order_ticket", 0) or 0)\n        except (TypeError, ValueError):\n            pending_ticket = 0\n    if state is None and working_orders:\n        incidents.append(\n            ReconcileIncident(\n                "LOCAL_STATE_MISSING_WITH_WORKING_ORDERS",\n                "CRITICAL",\n                f"{len(working_orders)} managed working order(s) exist but local live state is missing",\n            )\n        )\n    for order in working_orders:\n        if int(order.ticket) != pending_ticket:\n            incidents.append(\n                ReconcileIncident(\n                    "UNTRACKED_WORKING_ORDER",\n                    "CRITICAL",\n                    f"managed broker order {int(order.ticket)} state={order.state} is not the exact pending intent order",\n                )\n            )\n'''
    replace_once("src/restart_reconcile.py", old, new)
    target = ROOT / "src/restart_reconcile.py"
    text = target.read_text(encoding="utf-8")
    text = text.replace("pending_resolution", "pending_recovery")
    target.write_text(text, encoding="utf-8")
    replace_once(
        "src/restart_reconcile.py",
        '''            if persist:\n                append_recovered_event(events_path, state["pending_order_intent"], pending_recovery)\n                state["pending_order_intent"] = None\n''',
        '''            if persist:\n                if pending_recovery.effect == "FILLED":\n                    append_recovered_event(\n                        events_path,\n                        state["pending_order_intent"],\n                        pending_recovery.as_phase17_resolution(),\n                    )\n                elif pending_recovery.effect == "NO_FILL":\n                    append_no_fill_recovered_event(\n                        events_path,\n                        state["pending_order_intent"],\n                        pending_recovery,\n                    )\n                state["pending_order_intent"] = None\n''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''        "version": 1,\n        "generated_at": current.isoformat(),''',
        '''        "version": 2,\n        "generated_at": current.isoformat(),''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''        "broker_deals": len(deals),\n        "pending_intent_resolution": pending_recovery.to_dict(),''',
        '''        "broker_deals": len(deals),\n        "broker_working_orders": len(working_orders),\n        "broker_history_orders": len(history_orders),\n        "working_orders": [_snapshot_order(order) for order in working_orders],\n        "pending_intent_resolution": pending_recovery.to_dict(),''',
    )


def main() -> None:
    write_once("src/order_recovery.py", MARKER, ORDER_RECOVERY)
    write_once("tests/test_phase18_order_recovery.py", "phase18-intent", TESTS)
    write_once("docs/phase18-working-order-recovery.md", MARKER, DOC)
    patch_broker()
    patch_live()
    patch_restart()
    append_once("README.md", MARKER, README_ADD)
    print("Phase 18 working-order recovery applied")


if __name__ == "__main__":
    main()
