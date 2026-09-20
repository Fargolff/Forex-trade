from __future__ import annotations

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
