from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKER = "PHASE17_DETERMINISTIC_PENDING_INTENT_RESOLVER"


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"Phase 17 patch anchor not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if marker in text:
        return
    target.write_text(text.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")


pending_intent_module = r'''from __future__ import annotations

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
'''

phase17_tests = r'''from datetime import datetime, timezone
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
'''

phase17_docs = r'''# Phase 17 — Deterministic Pending-Intent Resolver

Phase 17 upgrades the Phase 16 fail-closed order journal into a deterministic restart resolver. The resolver is intentionally conservative: it may clear a pending intent only when broker evidence uniquely proves what happened.

## Evidence required

Every new live intent now records an `intent_id`, UTC `created_at`, the exact `(time_msc, ticket)` broker-deal cursor observed before submission, and a stable position identifier for close/flatten operations. Legacy Phase 16 intents that lack this metadata are never guessed; they remain a manual-reconciliation case.

For an ENTRY, automatic resolution requires all of the following: post-cursor opening deal evidence for the same strategy and side, exactly one broker order/position group whose aggregate filled volume equals the requested lots, exactly one matching current managed position, matching side/strategy/volume, and matching protective stop/take-profit levels.

For CLOSE or FLATTEN, automatic resolution requires post-cursor exit deal evidence for the exact target position, exactly one broker order, aggregate exit volume equal to the requested lots, and proof that the target position no longer exists.

## Recovery behavior

When `python -m src.restart_reconcile --mode verify` proves an intent exactly, it writes one idempotent recovered audit event, clears `pending_order_intent`, advances the full `(time_msc, ticket)` deal cursor, and may clear the halted state only when the halt reason is exclusively an execution-ambiguity code from Phase 16/17. Risk kills, account-identity failures, operational integrity failures, and other unrelated halts are never auto-cleared.

`--mode health` stays read-only. A resolvable pending intent is reported but not mutated.

## Cases that remain fail-closed

- legacy intent without Phase 17 metadata
- partial volume or protection mismatch
- more than one broker order can satisfy the intent
- target close position is still open
- missing broker deal evidence
- broker deals with unsupported/contradictory geometry
- unrelated halt reason remains active

There is still no automatic order resend, position repair, resize, or flatten during restart reconciliation.
'''


def main() -> None:
    if (ROOT / "src/pending_intent.py").exists() and MARKER in (ROOT / "src/pending_intent.py").read_text(encoding="utf-8"):
        print("Phase 17 already applied")
        return

    # Broker positions now expose the stable MT5 identifier while preserving
    # backwards-compatible test constructors through a default value.
    replace_once(
        "src/mt5_broker.py",
        '''@dataclass(frozen=True)\nclass BrokerPosition:\n    ticket: int\n    symbol: str\n    side: int\n    volume: float\n    price_open: float\n    stop_loss: float\n    take_profit: float\n    magic: int\n    comment: str\n''',
        '''@dataclass(frozen=True)\nclass BrokerPosition:\n    ticket: int\n    symbol: str\n    side: int\n    volume: float\n    price_open: float\n    stop_loss: float\n    take_profit: float\n    magic: int\n    comment: str\n    identifier: int = 0\n''',
    )
    replace_once(
        "src/mt5_broker.py",
        '''                    magic=pos_magic,\n                    comment=str(getattr(pos, "comment", "") or ""),\n                )''',
        '''                    magic=pos_magic,\n                    comment=str(getattr(pos, "comment", "") or ""),\n                    identifier=int(getattr(pos, "identifier", 0) or getattr(pos, "ticket", 0) or 0),\n                )''',
    )

    # Use the full broker-deal cursor everywhere. This closes the same-millisecond
    # loss window that exists when only time_msc is persisted.
    replace_once(
        "src/ops.py",
        '''def recent_managed_deals(\n    broker: Any,\n    symbol: str,\n    magic: int,\n    *,\n    after_time_msc: int = 0,\n    lookback_hours: float = 48.0,\n    now: datetime | None = None,\n) -> list[BrokerDeal]:\n    current = now or datetime.now(timezone.utc)\n    if after_time_msc > 0:\n        start = datetime.fromtimestamp(max(0, after_time_msc - 1) / 1000.0, tz=timezone.utc)\n    else:\n        start = current - timedelta(hours=lookback_hours)\n    deals = broker.history_deals(start, current, symbol=symbol, magic=magic)\n    return [deal for deal in deals if deal.time_msc > after_time_msc]\n''',
        '''def recent_managed_deals(\n    broker: Any,\n    symbol: str,\n    magic: int,\n    *,\n    after_time_msc: int = 0,\n    after_ticket: int = 0,\n    lookback_hours: float = 48.0,\n    now: datetime | None = None,\n) -> list[BrokerDeal]:\n    current = now or datetime.now(timezone.utc)\n    if after_time_msc > 0:\n        start = datetime.fromtimestamp(max(0, after_time_msc - 1) / 1000.0, tz=timezone.utc)\n    else:\n        start = current - timedelta(hours=lookback_hours)\n    deals = broker.history_deals(start, current, symbol=symbol, magic=magic)\n    cursor = (int(after_time_msc), int(after_ticket))\n    return sorted(\n        [deal for deal in deals if (int(deal.time_msc), int(deal.ticket)) > cursor],\n        key=lambda item: (int(item.time_msc), int(item.ticket)),\n    )\n''',
    )
    replace_once(
        "src/ops.py",
        '''    last_deal_time_msc: int = 0,\n) -> dict[str, Any]:''',
        '''    last_deal_time_msc: int = 0,\n    last_deal_ticket: int = 0,\n) -> dict[str, Any]:''',
    )
    replace_once(
        "src/ops.py",
        '''        "last_deal_time_msc": int(last_deal_time_msc),\n        "account": {''',
        '''        "last_deal_time_msc": int(last_deal_time_msc),\n        "last_deal_ticket": int(last_deal_ticket),\n        "account": {''',
    )

    replace_once("src/live.py", "import json\n", "import json\nimport uuid\n")
    replace_once(
        "src/live.py",
        '''class LiveState:\n    version: int = 3\n    peak_equity: float = 0.0\n    start_of_day_equity: float = 0.0\n    current_day: str | None = None\n    last_bar_time: str | None = None\n    last_deal_time_msc: int = 0\n    last_incident_fingerprint: str | None = None\n    pending_order_intent: dict[str, Any] | None = None''',
        '''class LiveState:\n    version: int = 4\n    peak_equity: float = 0.0\n    start_of_day_equity: float = 0.0\n    current_day: str | None = None\n    last_bar_time: str | None = None\n    last_deal_time_msc: int = 0\n    last_deal_ticket: int = 0\n    last_incident_fingerprint: str | None = None\n    pending_order_intent: dict[str, Any] | None = None''',
    )
    replace_once(
        "src/live.py",
        '''        position_ticket: int = 0,\n    ) -> dict[str, Any]:\n        intent = {\n            "action": action,\n            "strategy": strategy,\n            "symbol": self.symbol,\n            "side": int(side),\n            "lots": float(lots),\n            "stop_loss": float(stop_loss),\n            "take_profit": float(take_profit),\n            "position_ticket": int(position_ticket),\n            "bar_time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),\n        }''',
        '''        position_ticket: int = 0,\n        position_id: int = 0,\n    ) -> dict[str, Any]:\n        intent = {\n            "version": 2,\n            "intent_id": uuid.uuid4().hex,\n            "created_at": datetime.now(timezone.utc).isoformat(),\n            "deal_cursor_before_submit": {\n                "time_msc": int(self.state.last_deal_time_msc),\n                "ticket": int(self.state.last_deal_ticket),\n            },\n            "action": action,\n            "strategy": strategy,\n            "symbol": self.symbol,\n            "side": int(side),\n            "lots": float(lots),\n            "stop_loss": float(stop_loss),\n            "take_profit": float(take_profit),\n            "position_ticket": int(position_ticket),\n            "position_id": int(position_id or position_ticket),\n            "bar_time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),\n        }''',
    )
    replace_once(
        "src/live.py",
        '''                lots=position.volume, position_ticket=position.ticket,\n            )''',
        '''                lots=position.volume, position_ticket=position.ticket,\n                position_id=int(getattr(position, "identifier", 0) or position.ticket),\n            )''',
    )
    replace_once(
        "src/live.py",
        '''                    lots=existing.volume,\n                    position_ticket=existing.ticket,\n                )''',
        '''                    lots=existing.volume,\n                    position_ticket=existing.ticket,\n                    position_id=int(getattr(existing, "identifier", 0) or existing.ticket),\n                )''',
    )
    replace_once(
        "src/live.py",
        '''                last_deal_time_msc=self.state.last_deal_time_msc,\n            )''',
        '''                last_deal_time_msc=self.state.last_deal_time_msc,\n                last_deal_ticket=self.state.last_deal_ticket,\n            )''',
    )
    replace_once(
        "src/live.py",
        '''            after_time_msc=self.state.last_deal_time_msc,\n            lookback_hours=self.config.deal_reconcile_lookback_hours,''',
        '''            after_time_msc=self.state.last_deal_time_msc,\n            after_ticket=self.state.last_deal_ticket,\n            lookback_hours=self.config.deal_reconcile_lookback_hours,''',
    )
    replace_once(
        "src/live.py",
        '''        if deals:\n            self.state.last_deal_time_msc = max(item.time_msc for item in deals)\n        return deal_totals(deals)''',
        '''        if deals:\n            latest = max((int(item.time_msc), int(item.ticket)) for item in deals)\n            self.state.last_deal_time_msc = latest[0]\n            self.state.last_deal_ticket = latest[1]\n        return deal_totals(deals)''',
    )
    replace_once(
        "src/live.py",
        '''            "last_deal_time_msc": self.state.last_deal_time_msc,\n            "managed_positions": len(managed),''',
        '''            "last_deal_time_msc": self.state.last_deal_time_msc,\n            "last_deal_ticket": self.state.last_deal_ticket,\n            "pending_order_intent_id": (self.state.pending_order_intent or {}).get("intent_id"),\n            "managed_positions": len(managed),''',
    )

    # Integrate deterministic resolution into the Phase 14 restart gate.
    replace_once(
        "src/restart_reconcile.py",
        '''from .paper import load_portfolio_bundle\nfrom .production import atomic_write_json\n''',
        '''from .paper import load_portfolio_bundle\nfrom .pending_intent import (\n    append_recovered_event,\n    execution_halt_reason_resolvable,\n    resolve_pending_order_intent,\n)\nfrom .production import atomic_write_json\n''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''    if state and bool(state.get("halted", False)):\n        incidents.append(\n            ReconcileIncident(\n                "LOCAL_LIVE_STATE_HALTED",\n                "CRITICAL",\n                f"local live state is halted: {state.get('halt_reason') or 'unspecified'}",\n            )\n        )\n\n    if checkpoint:''',
        '''    if checkpoint:''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''    state_cursor_msc = 0\n    if state:\n        try:\n            state_cursor_msc = int(state.get("last_deal_time_msc", 0) or 0)\n        except (TypeError, ValueError):\n            incidents.append(ReconcileIncident("LOCAL_STATE_DEAL_CURSOR_INVALID", "CRITICAL", "last_deal_time_msc is invalid"))\n\n    cursor_msc = max(state_cursor_msc, checkpoint_cursor[0])''',
        '''    state_cursor_msc = 0\n    state_cursor_ticket = 0\n    if state:\n        try:\n            state_cursor_msc = int(state.get("last_deal_time_msc", 0) or 0)\n            state_cursor_ticket = int(state.get("last_deal_ticket", 0) or 0)\n        except (TypeError, ValueError):\n            incidents.append(ReconcileIncident("LOCAL_STATE_DEAL_CURSOR_INVALID", "CRITICAL", "last deal cursor is invalid"))\n\n    cursor_msc = max(state_cursor_msc, checkpoint_cursor[0])''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''    deals = list(broker.history_deals(start, current, symbol=symbol, magic=magic))\n    deals = sorted(deals, key=lambda item: (int(item.time_msc), int(item.ticket)))\n    lifecycles = build_deal_lifecycles(deals)\n    local_entries = read_local_entries(events_path, cfg.event_backups)\n\n    current_by_id: dict[int, BrokerPosition] = {_position_id(position): position for position in positions}\n''',
        '''    deals = list(broker.history_deals(start, current, symbol=symbol, magic=magic))\n    deals = sorted(deals, key=lambda item: (int(item.time_msc), int(item.ticket)))\n\n    pending_resolution = resolve_pending_order_intent(\n        state.get("pending_order_intent") if state else None,\n        deals,\n        positions,\n        volume_tolerance=cfg.volume_tolerance,\n    )\n    if state and state.get("pending_order_intent") is not None:\n        if pending_resolution.resolved:\n            if persist:\n                append_recovered_event(events_path, state["pending_order_intent"], pending_resolution)\n                state["pending_order_intent"] = None\n                current_cursor = (\n                    int(state.get("last_deal_time_msc", 0) or 0),\n                    int(state.get("last_deal_ticket", 0) or 0),\n                )\n                resolved_cursor = (pending_resolution.cursor_time_msc, pending_resolution.cursor_ticket)\n                if resolved_cursor > current_cursor:\n                    state["last_deal_time_msc"] = resolved_cursor[0]\n                    state["last_deal_ticket"] = resolved_cursor[1]\n                if bool(state.get("halted", False)) and execution_halt_reason_resolvable(state.get("halt_reason")):\n                    state["halted"] = False\n                    state["halt_reason"] = None\n                    state["last_incident_fingerprint"] = None\n                atomic_write_json(state_path, state)\n            else:\n                incidents.append(\n                    ReconcileIncident(\n                        "PENDING_INTENT_RESOLVABLE_READ_ONLY",\n                        "CRITICAL",\n                        "broker evidence proves the pending intent, but health mode is read-only; run verify mode to persist recovery",\n                        strategy=pending_resolution.strategy,\n                        position_id=pending_resolution.position_id,\n                    )\n                )\n        else:\n            incidents.append(\n                ReconcileIncident(\n                    pending_resolution.code,\n                    "CRITICAL",\n                    pending_resolution.detail,\n                    strategy=pending_resolution.strategy,\n                    position_id=pending_resolution.position_id,\n                )\n            )\n\n    if state and bool(state.get("halted", False)):\n        incidents.append(\n            ReconcileIncident(\n                "LOCAL_LIVE_STATE_HALTED",\n                "CRITICAL",\n                f"local live state is halted: {state.get('halt_reason') or 'unspecified'}",\n            )\n        )\n\n    lifecycles = build_deal_lifecycles(deals)\n    local_entries = read_local_entries(events_path, cfg.event_backups)\n\n    current_by_id: dict[int, BrokerPosition] = {_position_id(position): position for position in positions}\n''',
    )
    replace_once(
        "src/restart_reconcile.py",
        '''        "broker_deals": len(deals),\n        "deal_cursor": {"time_msc": latest_cursor[0], "ticket": latest_cursor[1]},''',
        '''        "broker_deals": len(deals),\n        "pending_intent_resolution": pending_resolution.to_dict(),\n        "deal_cursor": {"time_msc": latest_cursor[0], "ticket": latest_cursor[1]},''',
    )

    (ROOT / "src/pending_intent.py").write_text(pending_intent_module, encoding="utf-8")
    (ROOT / "tests/test_phase17_pending_intent.py").write_text(phase17_tests, encoding="utf-8")
    (ROOT / "docs/phase17-pending-intent-resolver.md").write_text(phase17_docs, encoding="utf-8")

    append_once(
        "README.md",
        MARKER,
        '''## Phase 17 — Deterministic Pending-Intent Resolver\n\n<!-- PHASE17_DETERMINISTIC_PENDING_INTENT_RESOLVER -->\nPhase 17 turns the Phase 16 pending-order journal into a deterministic restart resolver. New intents persist a unique intent ID, submission timestamp, stable position identifier, and full `(time_msc, ticket)` pre-submit broker-deal cursor. Restart verification may reconstruct a missing local audit event and clear an execution-only halt only when MT5 deals plus current position state prove the exact full entry/exit. Legacy, partial, conflicting, or otherwise ambiguous evidence remains fail-closed with no automatic resend or repair. See `docs/phase17-pending-intent-resolver.md`.''',
    )

    print("Phase 17 applied")


if __name__ == "__main__":
    main()
