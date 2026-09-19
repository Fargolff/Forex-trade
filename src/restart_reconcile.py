from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import load_config
from .mt5_broker import BrokerDeal, BrokerPosition, MT5Broker
from .paper import load_portfolio_bundle
from .pending_intent import (
    append_recovered_event,
    execution_halt_reason_resolvable,
    resolve_pending_order_intent,
)
from .order_recovery import assess_pending_order_recovery, append_no_fill_recovered_event
from .production import atomic_write_json


COMMENT_PREFIX = "fat:"
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3


@dataclass(frozen=True)
class RestartReconcileConfig:
    lookback_hours: float = 2160.0  # 90 days
    report_path: str = "runtime/restart_reconcile.json"
    checkpoint_path: str = "runtime/restart_checkpoint.json"
    event_backups: int = 5
    volume_tolerance: float = 1e-8
    require_local_entry: bool = True


@dataclass(frozen=True)
class ReconcileIncident:
    code: str
    severity: str
    detail: str
    strategy: str = ""
    position_id: int = 0


@dataclass(frozen=True)
class LocalEntry:
    order_or_deal: int
    strategy: str
    side: int
    lots: float
    time: str


@dataclass
class DealLifecycle:
    position_id: int
    strategy: str = ""
    opening_order: int = 0
    opening_deal: int = 0
    opening_side: int = 0
    net_volume: float = 0.0
    first_time_msc: int = 0
    last_time_msc: int = 0
    last_ticket: int = 0
    unsupported_entries: int = 0


def load_restart_reconcile_config(path: str | Path = "reconcile.yaml") -> RestartReconcileConfig:
    target = Path(path)
    if not target.exists():
        fallback = Path("reconcile.example.yaml")
        if not fallback.exists():
            return RestartReconcileConfig()
        target = fallback
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("restart reconciliation config must be a mapping")
    cfg = RestartReconcileConfig(**raw)
    if cfg.lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    if cfg.event_backups < 0:
        raise ValueError("event_backups cannot be negative")
    if cfg.volume_tolerance <= 0:
        raise ValueError("volume_tolerance must be positive")
    return cfg


def _event_paths(path: str | Path, backups: int) -> list[Path]:
    base = Path(path)
    paths: list[Path] = []
    for index in range(backups, 0, -1):
        rotated = base.with_name(f"{base.name}.{index}")
        if rotated.exists():
            paths.append(rotated)
    if base.exists():
        paths.append(base)
    return paths


def read_local_entries(path: str | Path, backups: int = 5) -> dict[int, LocalEntry]:
    entries: dict[int, LocalEntry] = {}
    for event_path in _event_paths(path, backups):
        try:
            with event_path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("event", "")) != "ENTRY":
                        continue
                    try:
                        ticket = int(float(row.get("ticket") or 0))
                        side = int(float(row.get("side") or 0))
                        lots = float(row.get("lots") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    strategy = str(row.get("strategy", "") or "")
                    if ticket > 0 and side in (-1, 1) and lots > 0 and strategy:
                        entries[ticket] = LocalEntry(
                            order_or_deal=ticket,
                            strategy=strategy,
                            side=side,
                            lots=lots,
                            time=str(row.get("time", "") or ""),
                        )
        except (OSError, csv.Error):
            continue
    return entries


def _strategy_from_comment(comment: str) -> str:
    return comment[len(COMMENT_PREFIX) :] if comment.startswith(COMMENT_PREFIX) else ""


def _position_id(position: BrokerPosition) -> int:
    identifier = int(getattr(position, "identifier", 0) or 0)
    return identifier if identifier > 0 else int(position.ticket)


def _cursor(deals: Iterable[BrokerDeal]) -> tuple[int, int]:
    items = list(deals)
    if not items:
        return (0, 0)
    return max((int(item.time_msc), int(item.ticket)) for item in items)


def _after_cursor(deal: BrokerDeal, cursor: tuple[int, int]) -> bool:
    return (int(deal.time_msc), int(deal.ticket)) > cursor


def build_deal_lifecycles(deals: Iterable[BrokerDeal]) -> dict[int, DealLifecycle]:
    lifecycles: dict[int, DealLifecycle] = {}
    for deal in sorted(deals, key=lambda item: (int(item.time_msc), int(item.ticket))):
        position_id = int(deal.position_id)
        if position_id <= 0:
            continue
        life = lifecycles.setdefault(position_id, DealLifecycle(position_id=position_id))
        if life.first_time_msc <= 0:
            life.first_time_msc = int(deal.time_msc)
        life.last_time_msc = int(deal.time_msc)
        life.last_ticket = int(deal.ticket)

        if int(deal.entry) == DEAL_ENTRY_IN:
            life.net_volume += float(deal.volume)
            if life.opening_deal <= 0:
                life.opening_deal = int(deal.ticket)
                life.opening_order = int(deal.order)
                life.opening_side = int(deal.side)
                life.strategy = _strategy_from_comment(str(deal.comment))
        elif int(deal.entry) in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY):
            life.net_volume -= float(deal.volume)
        else:
            life.unsupported_entries += 1
    return lifecycles


def _load_json_object(path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    target = Path(path)
    if not target.exists():
        return None, None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"
    if not isinstance(payload, dict):
        return None, "not-a-json-object"
    return payload, None


def _checkpoint_positions(checkpoint: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not checkpoint:
        return {}
    raw = checkpoint.get("open_positions", [])
    if not isinstance(raw, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            position_id = int(item.get("position_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if position_id > 0:
            result[position_id] = item
    return result


def _checkpoint_cursor(checkpoint: dict[str, Any] | None) -> tuple[int, int]:
    if not checkpoint:
        return (0, 0)
    raw = checkpoint.get("deal_cursor", {})
    if not isinstance(raw, dict):
        return (0, 0)
    try:
        return (int(raw.get("time_msc", 0) or 0), int(raw.get("ticket", 0) or 0))
    except (TypeError, ValueError):
        return (0, 0)


def _snapshot_order(order: Any) -> dict[str, Any]:
    return {
        "ticket": int(order.ticket),
        "time_setup_msc": int(order.time_setup_msc),
        "time_done_msc": int(order.time_done_msc),
        "symbol": str(order.symbol),
        "side": int(order.side),
        "volume_initial": float(order.volume_initial),
        "volume_current": float(order.volume_current),
        "price_open": float(order.price_open),
        "stop_loss": float(order.stop_loss),
        "take_profit": float(order.take_profit),
        "magic": int(order.magic),
        "comment": str(order.comment),
        "state": str(order.state),
    }


def _snapshot_position(position: BrokerPosition) -> dict[str, Any]:
    return {
        "ticket": int(position.ticket),
        "position_id": _position_id(position),
        "strategy": _strategy_from_comment(str(position.comment)),
        "side": int(position.side),
        "volume": float(position.volume),
        "price_open": float(position.price_open),
        "stop_loss": float(position.stop_loss),
        "take_profit": float(position.take_profit),
        "magic": int(position.magic),
        "comment": str(position.comment),
    }


def restart_reconciliation_report(
    broker: Any,
    *,
    symbol: str,
    magic: int,
    allowed_strategies: set[str],
    state_path: str | Path,
    events_path: str | Path,
    cfg: RestartReconcileConfig,
    now: datetime | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    incidents: list[ReconcileIncident] = []
    account = broker.account_snapshot()
    positions = list(broker.open_positions(symbol=symbol, magic=magic))

    state, state_error = _load_json_object(state_path)
    checkpoint, checkpoint_error = _load_json_object(cfg.checkpoint_path)
    if state_error:
        incidents.append(ReconcileIncident("LOCAL_STATE_CORRUPT", "CRITICAL", state_error))
    if checkpoint_error:
        incidents.append(ReconcileIncident("RECONCILE_CHECKPOINT_CORRUPT", "CRITICAL", checkpoint_error))
    if state is None and positions:
        incidents.append(
            ReconcileIncident(
                "LOCAL_STATE_MISSING_WITH_OPEN_POSITIONS",
                "CRITICAL",
                f"{len(positions)} broker-managed position(s) exist but local live state is missing",
            )
        )
    if checkpoint:
        try:
            prior_login = int(checkpoint.get("account_login", 0) or 0)
        except (TypeError, ValueError):
            prior_login = 0
        if prior_login > 0 and prior_login != int(account.login):
            incidents.append(
                ReconcileIncident(
                    "ACCOUNT_IDENTITY_CHANGED",
                    "CRITICAL",
                    f"checkpoint login={prior_login} but broker login={int(account.login)}",
                )
            )
        prior_symbol = str(checkpoint.get("symbol", "") or "")
        prior_magic = int(checkpoint.get("magic", 0) or 0)
        if prior_symbol and prior_symbol != symbol:
            incidents.append(ReconcileIncident("SYMBOL_CHANGED", "CRITICAL", f"checkpoint={prior_symbol};current={symbol}"))
        if prior_magic and prior_magic != magic:
            incidents.append(ReconcileIncident("MAGIC_CHANGED", "CRITICAL", f"checkpoint={prior_magic};current={magic}"))

    checkpoint_cursor = _checkpoint_cursor(checkpoint)
    state_cursor_msc = 0
    state_cursor_ticket = 0
    if state:
        try:
            state_cursor_msc = int(state.get("last_deal_time_msc", 0) or 0)
            state_cursor_ticket = int(state.get("last_deal_ticket", 0) or 0)
        except (TypeError, ValueError):
            incidents.append(ReconcileIncident("LOCAL_STATE_DEAL_CURSOR_INVALID", "CRITICAL", "last deal cursor is invalid"))

    cursor_msc = max(state_cursor_msc, checkpoint_cursor[0])
    if cursor_msc > 0:
        cursor_start = datetime.fromtimestamp(max(0, cursor_msc - 1) / 1000.0, tz=timezone.utc)
        lookback_start = current - timedelta(hours=cfg.lookback_hours)
        start = min(cursor_start, lookback_start)
    else:
        start = current - timedelta(hours=cfg.lookback_hours)

    deals = list(broker.history_deals(start, current, symbol=symbol, magic=magic))
    deals = sorted(deals, key=lambda item: (int(item.time_msc), int(item.ticket)))

    open_orders_fn = getattr(broker, "open_orders", None)
    history_orders_fn = getattr(broker, "history_orders", None)
    working_orders = list(open_orders_fn(symbol=symbol, magic=magic)) if callable(open_orders_fn) else []
    history_orders = (
        list(history_orders_fn(start, current, symbol=symbol, magic=magic))
        if callable(history_orders_fn)
        else []
    )

    pending_intent = state.get("pending_order_intent") if state else None
    pending_recovery = assess_pending_order_recovery(
        pending_intent,
        deals,
        positions,
        working_orders,
        history_orders,
        volume_tolerance=cfg.volume_tolerance,
    )

    pending_ticket = 0
    if isinstance(pending_intent, dict):
        try:
            pending_ticket = int(pending_intent.get("broker_order_ticket", 0) or 0)
        except (TypeError, ValueError):
            pending_ticket = 0
    if state is None and working_orders:
        incidents.append(
            ReconcileIncident(
                "LOCAL_STATE_MISSING_WITH_WORKING_ORDERS",
                "CRITICAL",
                f"{len(working_orders)} managed working order(s) exist but local live state is missing",
            )
        )
    for order in working_orders:
        if int(order.ticket) != pending_ticket:
            incidents.append(
                ReconcileIncident(
                    "UNTRACKED_WORKING_ORDER",
                    "CRITICAL",
                    f"managed broker order {int(order.ticket)} state={order.state} is not the exact pending intent order",
                )
            )
    if state and state.get("pending_order_intent") is not None:
        if pending_recovery.resolved:
            if persist:
                if pending_recovery.effect == "FILLED":
                    append_recovered_event(
                        events_path,
                        state["pending_order_intent"],
                        pending_recovery.as_phase17_resolution(),
                    )
                elif pending_recovery.effect == "NO_FILL":
                    append_no_fill_recovered_event(
                        events_path,
                        state["pending_order_intent"],
                        pending_recovery,
                    )
                state["pending_order_intent"] = None
                current_cursor = (
                    int(state.get("last_deal_time_msc", 0) or 0),
                    int(state.get("last_deal_ticket", 0) or 0),
                )
                resolved_cursor = (pending_recovery.cursor_time_msc, pending_recovery.cursor_ticket)
                if resolved_cursor > current_cursor:
                    state["last_deal_time_msc"] = resolved_cursor[0]
                    state["last_deal_ticket"] = resolved_cursor[1]
                if bool(state.get("halted", False)) and execution_halt_reason_resolvable(state.get("halt_reason")):
                    state["halted"] = False
                    state["halt_reason"] = None
                    state["last_incident_fingerprint"] = None
                atomic_write_json(state_path, state)
            else:
                incidents.append(
                    ReconcileIncident(
                        "PENDING_INTENT_RESOLVABLE_READ_ONLY",
                        "CRITICAL",
                        "broker evidence proves the pending intent, but health mode is read-only; run verify mode to persist recovery",
                        strategy=pending_recovery.strategy,
                        position_id=pending_recovery.position_id,
                    )
                )
        else:
            incidents.append(
                ReconcileIncident(
                    pending_recovery.code,
                    "CRITICAL",
                    pending_recovery.detail,
                    strategy=pending_recovery.strategy,
                    position_id=pending_recovery.position_id,
                )
            )

    if state and bool(state.get("halted", False)):
        incidents.append(
            ReconcileIncident(
                "LOCAL_LIVE_STATE_HALTED",
                "CRITICAL",
                f"local live state is halted: {state.get('halt_reason') or 'unspecified'}",
            )
        )

    lifecycles = build_deal_lifecycles(deals)
    local_entries = read_local_entries(events_path, cfg.event_backups)

    current_by_id: dict[int, BrokerPosition] = {_position_id(position): position for position in positions}
    for position_id, position in current_by_id.items():
        strategy = _strategy_from_comment(str(position.comment))
        if not strategy:
            incidents.append(
                ReconcileIncident(
                    "POSITION_COMMENT_UNMANAGED",
                    "CRITICAL",
                    f"managed magic position has unexpected comment {position.comment!r}",
                    position_id=position_id,
                )
            )
        elif strategy not in allowed_strategies:
            incidents.append(
                ReconcileIncident(
                    "POSITION_STRATEGY_NOT_IN_BUNDLE",
                    "CRITICAL",
                    f"strategy {strategy!r} is not in current live bundle",
                    strategy=strategy,
                    position_id=position_id,
                )
            )

        life = lifecycles.get(position_id)
        if life is None or life.opening_deal <= 0:
            incidents.append(
                ReconcileIncident(
                    "POSITION_DEAL_HISTORY_MISSING",
                    "CRITICAL",
                    f"cannot find opening broker deal within {cfg.lookback_hours:.0f}h lookback",
                    strategy=strategy,
                    position_id=position_id,
                )
            )
            continue
        if life.unsupported_entries:
            incidents.append(
                ReconcileIncident(
                    "UNSUPPORTED_DEAL_ENTRY_MODE",
                    "CRITICAL",
                    f"position lifecycle contains {life.unsupported_entries} INOUT/unknown deal entry record(s)",
                    strategy=strategy,
                    position_id=position_id,
                )
            )
        if life.strategy != strategy:
            incidents.append(
                ReconcileIncident(
                    "POSITION_STRATEGY_MISMATCH",
                    "CRITICAL",
                    f"broker position strategy={strategy!r}; opening deal strategy={life.strategy!r}",
                    strategy=strategy,
                    position_id=position_id,
                )
            )
        if life.opening_side not in (-1, 1) or int(position.side) != life.opening_side:
            incidents.append(
                ReconcileIncident(
                    "POSITION_SIDE_MISMATCH",
                    "CRITICAL",
                    f"position side={int(position.side)}; opening deal side={life.opening_side}",
                    strategy=strategy,
                    position_id=position_id,
                )
            )
        if abs(float(position.volume) - max(0.0, life.net_volume)) > cfg.volume_tolerance:
            incidents.append(
                ReconcileIncident(
                    "POSITION_VOLUME_MISMATCH",
                    "CRITICAL",
                    f"broker volume={float(position.volume):.10f}; reconstructed={max(0.0, life.net_volume):.10f}",
                    strategy=strategy,
                    position_id=position_id,
                )
            )

        if cfg.require_local_entry:
            candidates = [life.opening_order, life.opening_deal]
            local = next((local_entries[value] for value in candidates if value > 0 and value in local_entries), None)
            if local is None:
                incidents.append(
                    ReconcileIncident(
                        "BROKER_POSITION_WITHOUT_LOCAL_ENTRY",
                        "CRITICAL",
                        f"opening order={life.opening_order};deal={life.opening_deal} has no local ENTRY audit event",
                        strategy=strategy,
                        position_id=position_id,
                    )
                )
            else:
                if local.strategy != strategy or local.side != int(position.side):
                    incidents.append(
                        ReconcileIncident(
                            "LOCAL_ENTRY_MISMATCH",
                            "CRITICAL",
                            f"local strategy/side={local.strategy}/{local.side}; broker={strategy}/{int(position.side)}",
                            strategy=strategy,
                            position_id=position_id,
                        )
                    )

    prior_positions = _checkpoint_positions(checkpoint)
    for position_id, prior in prior_positions.items():
        if position_id in current_by_id:
            continue
        lifecycle = lifecycles.get(position_id)
        explained = False
        if lifecycle:
            for deal in deals:
                if int(deal.position_id) != position_id:
                    continue
                if int(deal.entry) not in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY):
                    continue
                if _after_cursor(deal, checkpoint_cursor):
                    explained = True
                    break
        if not explained:
            incidents.append(
                ReconcileIncident(
                    "POSITION_DISAPPEARED_WITHOUT_EXIT_DEAL",
                    "CRITICAL",
                    "position existed at prior checkpoint but no later broker exit deal explains its disappearance",
                    strategy=str(prior.get("strategy", "") or ""),
                    position_id=position_id,
                )
            )

    for position_id, life in lifecycles.items():
        if life.net_volume > cfg.volume_tolerance and position_id not in current_by_id:
            incidents.append(
                ReconcileIncident(
                    "DEAL_HISTORY_OPEN_WITHOUT_POSITION",
                    "CRITICAL",
                    f"broker deals reconstruct net open volume {life.net_volume:.10f} but no current position exists",
                    strategy=life.strategy,
                    position_id=position_id,
                )
            )

    latest_cursor = _cursor(deals)
    status = "CRITICAL" if incidents else "OK"
    report = {
        "version": 2,
        "generated_at": current.isoformat(),
        "status": status,
        "ok": status == "OK",
        "symbol": symbol,
        "magic": int(magic),
        "account_login": int(account.login),
        "lookback_hours": float(cfg.lookback_hours),
        "state_present": state is not None,
        "events_entries": len(local_entries),
        "broker_deals": len(deals),
        "broker_working_orders": len(working_orders),
        "broker_history_orders": len(history_orders),
        "working_orders": [_snapshot_order(order) for order in working_orders],
        "pending_intent_resolution": pending_recovery.to_dict(),
        "deal_cursor": {"time_msc": latest_cursor[0], "ticket": latest_cursor[1]},
        "managed_positions": [_snapshot_position(position) for position in positions],
        "incidents": [asdict(item) for item in incidents],
    }

    atomic_write_json(cfg.report_path, report)
    if persist and report["ok"]:
        checkpoint_payload = {
            "version": 1,
            "created_at": current.isoformat(),
            "account_login": int(account.login),
            "symbol": symbol,
            "magic": int(magic),
            "deal_cursor": {"time_msc": latest_cursor[0], "ticket": latest_cursor[1]},
            "open_positions": [_snapshot_position(position) for position in positions],
        }
        atomic_write_json(cfg.checkpoint_path, checkpoint_payload)
        report["checkpoint_written"] = True
    else:
        report["checkpoint_written"] = False
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 14 broker/local restart reconciliation gate")
    parser.add_argument("--mode", choices=["verify", "health"], default="verify")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--reconcile-config", default="reconcile.yaml")
    parser.add_argument("--no-checkpoint", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        config_path = Path("config.example.yaml")
    app_cfg = load_config(config_path)
    rec_cfg = load_restart_reconcile_config(args.reconcile_config)
    strategies, _weights = load_portfolio_bundle(app_cfg.live.weights_path, app_cfg.live.candidates_path)

    broker = MT5Broker()
    broker.connect()
    try:
        report = restart_reconciliation_report(
            broker,
            symbol=app_cfg.symbol,
            magic=app_cfg.live.magic,
            allowed_strategies=set(strategies),
            state_path=app_cfg.live.state_path,
            events_path=app_cfg.live.events_path,
            cfg=rec_cfg,
            persist=(args.mode == "verify" and not args.no_checkpoint),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(0 if report["ok"] else 3)
    finally:
        broker.close()


if __name__ == "__main__":
    main()
