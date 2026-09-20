from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
from pathlib import Path
from typing import Any, Iterable

from .mt5_broker import BrokerDeal, BrokerPosition


COMMENT_PREFIX = "fat:"
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_OUT_BY = 3
PHASE17_MARKER = "PHASE17_DETERMINISTIC_PENDING_INTENT_RESOLVER"

EXECUTION_HALT_CODES = {
    "PENDING_ORDER_INTENT",
    "PARTIAL_FILL",
    "ORDER_ACCEPTED_UNCONFIRMED",
    "ORDER_SUBMISSION_AMBIGUOUS",
    "ORDER_EXECUTION_EXCEPTION",
    "FILL_VOLUME_MISMATCH",
    "ORDER_STATUS_UNKNOWN",
    "CLOSE_SUBMISSION_AMBIGUOUS",
    "CLOSE_EXECUTION_EXCEPTION",
    "EMERGENCY_CLOSE_AMBIGUOUS",
    "EMERGENCY_CLOSE_EXCEPTION",
}


@dataclass(frozen=True)
class PendingIntentResolution:
    status: str
    code: str
    detail: str
    action: str = ""
    strategy: str = ""
    intent_id: str = ""
    order: int = 0
    deal_tickets: tuple[int, ...] = ()
    position_id: int = 0
    volume: float = 0.0
    price: float = 0.0
    cursor_time_msc: int = 0
    cursor_ticket: int = 0

    @property
    def resolved(self) -> bool:
        return self.status == "RESOLVED"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["deal_tickets"] = list(self.deal_tickets)
        payload["resolved"] = self.resolved
        return payload


def _strategy_from_comment(comment: str) -> str:
    return comment[len(COMMENT_PREFIX) :] if comment.startswith(COMMENT_PREFIX) else ""


def _position_id(position: BrokerPosition) -> int:
    identifier = int(getattr(position, "identifier", 0) or 0)
    return identifier if identifier > 0 else int(position.ticket)


def _parse_created_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _intent_cursor(intent: dict[str, Any]) -> tuple[int, int] | None:
    raw = intent.get("deal_cursor_before_submit")
    if not isinstance(raw, dict):
        return None
    try:
        return (int(raw.get("time_msc", 0) or 0), int(raw.get("ticket", 0) or 0))
    except (TypeError, ValueError):
        return None


def _after_cursor(deal: BrokerDeal, cursor: tuple[int, int]) -> bool:
    return (int(deal.time_msc), int(deal.ticket)) > cursor


def _weighted_price(items: Iterable[BrokerDeal]) -> float:
    deals = list(items)
    volume = sum(float(item.volume) for item in deals)
    if volume <= 0:
        return 0.0
    return sum(float(item.price) * float(item.volume) for item in deals) / volume


def _unresolved(intent: dict[str, Any] | None, code: str, detail: str) -> PendingIntentResolution:
    value = intent or {}
    return PendingIntentResolution(
        status="UNRESOLVED",
        code=code,
        detail=detail,
        action=str(value.get("action", "") or ""),
        strategy=str(value.get("strategy", "") or ""),
        intent_id=str(value.get("intent_id", "") or ""),
    )


def resolve_pending_order_intent(
    intent: dict[str, Any] | None,
    deals: Iterable[BrokerDeal],
    positions: Iterable[BrokerPosition],
    *,
    volume_tolerance: float = 1e-8,
    price_tolerance: float = 1e-8,
    clock_skew_seconds: float = 120.0,
) -> PendingIntentResolution:
    if not intent:
        return PendingIntentResolution("NO_INTENT", "NO_PENDING_INTENT", "no pending order intent")
    if not isinstance(intent, dict):
        return _unresolved(None, "PENDING_INTENT_INVALID", "pending intent is not a mapping")

    action = str(intent.get("action", "") or "").upper()
    strategy = str(intent.get("strategy", "") or "")
    symbol = str(intent.get("symbol", "") or "")
    intent_id = str(intent.get("intent_id", "") or "")
    created_at = _parse_created_at(intent.get("created_at"))
    cursor = _intent_cursor(intent)
    try:
        side = int(intent.get("side", 0) or 0)
        lots = float(intent.get("lots", 0.0) or 0.0)
        position_ticket = int(intent.get("position_ticket", 0) or 0)
        position_id = int(intent.get("position_id", 0) or 0)
        stop_loss = float(intent.get("stop_loss", 0.0) or 0.0)
        take_profit = float(intent.get("take_profit", 0.0) or 0.0)
    except (TypeError, ValueError):
        return _unresolved(intent, "PENDING_INTENT_INVALID", "pending intent numeric fields are invalid")

    if action not in {"ENTRY", "CLOSE", "FLATTEN"}:
        return _unresolved(intent, "PENDING_INTENT_ACTION_INVALID", f"unsupported action {action!r}")
    if not intent_id or created_at is None or cursor is None:
        return _unresolved(
            intent,
            "PENDING_INTENT_METADATA_INSUFFICIENT",
            "intent lacks Phase 17 intent_id/created_at/deal_cursor_before_submit metadata; manual reconciliation required",
        )
    if not strategy or not symbol or side not in (-1, 1) or lots <= 0:
        return _unresolved(intent, "PENDING_INTENT_INVALID", "strategy/symbol/side/lots are incomplete")

    created_floor_msc = int(created_at.timestamp() * 1000) - int(max(0.0, clock_skew_seconds) * 1000)
    all_deals = sorted(deals, key=lambda item: (int(item.time_msc), int(item.ticket)))
    post = [
        item
        for item in all_deals
        if item.symbol == symbol
        and _after_cursor(item, cursor)
        and int(item.time_msc) >= created_floor_msc
    ]
    current_positions = list(positions)

    if action == "ENTRY":
        matches = [
            item
            for item in post
            if int(item.entry) == DEAL_ENTRY_IN
            and int(item.side) == side
            and _strategy_from_comment(str(item.comment)) == strategy
        ]
        groups: dict[tuple[int, int], list[BrokerDeal]] = {}
        for item in matches:
            groups.setdefault((int(item.order), int(item.position_id)), []).append(item)

        eligible: list[tuple[int, int, list[BrokerDeal], BrokerPosition]] = []
        for (order_id, broker_position_id), items in groups.items():
            if order_id <= 0 or broker_position_id <= 0:
                continue
            total = sum(float(item.volume) for item in items)
            if abs(total - lots) > volume_tolerance:
                continue
            current = [item for item in current_positions if _position_id(item) == broker_position_id]
            if len(current) != 1:
                continue
            position = current[0]
            if int(position.side) != side:
                continue
            if _strategy_from_comment(str(position.comment)) != strategy:
                continue
            if abs(float(position.volume) - lots) > volume_tolerance:
                continue
            if stop_loss > 0 and abs(float(position.stop_loss) - stop_loss) > price_tolerance:
                continue
            if take_profit > 0 and abs(float(position.take_profit) - take_profit) > price_tolerance:
                continue
            eligible.append((order_id, broker_position_id, items, position))

        if len(eligible) == 1:
            order_id, broker_position_id, items, _position = eligible[0]
            cursor_value = max((int(item.time_msc), int(item.ticket)) for item in items)
            return PendingIntentResolution(
                status="RESOLVED",
                code="ENTRY_FULL_FILL_PROVEN",
                detail="exact post-intent broker entry deals and one matching protected open position prove the submission completed",
                action=action,
                strategy=strategy,
                intent_id=intent_id,
                order=order_id,
                deal_tickets=tuple(int(item.ticket) for item in items),
                position_id=broker_position_id,
                volume=sum(float(item.volume) for item in items),
                price=_weighted_price(items),
                cursor_time_msc=cursor_value[0],
                cursor_ticket=cursor_value[1],
            )
        if len(eligible) > 1:
            return _unresolved(intent, "PENDING_INTENT_MULTIPLE_ENTRY_MATCHES", "multiple broker entry groups satisfy the same intent")
        if matches:
            return _unresolved(
                intent,
                "PENDING_INTENT_ENTRY_EVIDENCE_MISMATCH",
                "post-intent entry deals exist but volume/order/position/protection evidence does not exactly match the intent",
            )
        return _unresolved(intent, "PENDING_INTENT_ENTRY_EVIDENCE_NOT_FOUND", "no exact post-intent broker entry evidence was found")

    target_position_id = position_id or position_ticket
    if target_position_id <= 0:
        return _unresolved(intent, "PENDING_INTENT_POSITION_ID_MISSING", "close/flatten intent has no stable broker position identifier")

    exits = [
        item
        for item in post
        if int(item.position_id) == target_position_id
        and int(item.entry) in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)
        and int(item.side) == side
    ]
    if not exits:
        return _unresolved(intent, "PENDING_INTENT_EXIT_EVIDENCE_NOT_FOUND", "no post-intent broker exit deal was found for the target position")

    order_ids = {int(item.order) for item in exits if int(item.order) > 0}
    if len(order_ids) != 1:
        return _unresolved(intent, "PENDING_INTENT_EXIT_ORDER_AMBIGUOUS", f"expected one exit order but found {sorted(order_ids)}")
    total = sum(float(item.volume) for item in exits)
    if abs(total - lots) > volume_tolerance:
        return _unresolved(
            intent,
            "PENDING_INTENT_EXIT_VOLUME_MISMATCH",
            f"exit deal volume {total:.10f} does not match intent volume {lots:.10f}",
        )
    if any(_position_id(item) == target_position_id for item in current_positions):
        return _unresolved(intent, "PENDING_INTENT_POSITION_STILL_OPEN", "target position still exists after the candidate exit deals")

    cursor_value = max((int(item.time_msc), int(item.ticket)) for item in exits)
    return PendingIntentResolution(
        status="RESOLVED",
        code=f"{action}_FULL_EXIT_PROVEN",
        detail="exact post-intent exit deal volume and absence of the target position prove the close completed",
        action=action,
        strategy=strategy,
        intent_id=intent_id,
        order=next(iter(order_ids)),
        deal_tickets=tuple(int(item.ticket) for item in exits),
        position_id=target_position_id,
        volume=total,
        price=_weighted_price(exits),
        cursor_time_msc=cursor_value[0],
        cursor_ticket=cursor_value[1],
    )


def execution_halt_reason_resolvable(reason: str | None) -> bool:
    if not reason:
        return False
    value = str(reason)
    if value.startswith("ops:"):
        value = value[len("ops:") :]
    codes = {item for item in value.split(",") if item}
    return bool(codes) and codes.issubset(EXECUTION_HALT_CODES)


def recovered_event_row(intent: dict[str, Any], resolution: PendingIntentResolution) -> dict[str, Any]:
    action = resolution.action
    side = int(intent.get("side", 0) or 0)
    event_side = side if action == "ENTRY" else -side
    ticket = resolution.order or (resolution.deal_tickets[0] if resolution.deal_tickets else 0)
    reason = (
        f"phase17_recovered_pending_intent:{resolution.intent_id};"
        f"code={resolution.code};deals={','.join(str(item) for item in resolution.deal_tickets)};"
        f"position_id={resolution.position_id}"
    )
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": "ENTRY" if action == "ENTRY" else action,
        "strategy": resolution.strategy,
        "side": event_side,
        "lots": resolution.volume,
        "price": resolution.price,
        "stop_loss": float(intent.get("stop_loss", 0.0) or 0.0) if action == "ENTRY" else "",
        "take_profit": float(intent.get("take_profit", 0.0) or 0.0) if action == "ENTRY" else "",
        "spread_pips": "",
        "ticket": ticket,
        "reason": reason,
    }


def append_recovered_event(
    path: str | Path,
    intent: dict[str, Any],
    resolution: PendingIntentResolution,
) -> bool:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    needle = f"phase17_recovered_pending_intent:{resolution.intent_id}"
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
    exists = target.exists()
    row = recovered_event_row(intent, resolution)
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in columns})
    return True
