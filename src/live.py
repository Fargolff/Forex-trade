from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import json
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from .mt5_broker import (
    BrokerOrder,
    BrokerOrderRejected,
    BrokerPosition,
    BrokerSubmissionAmbiguous,
    ExecutionReceipt,
    SymbolSpec,
    normalize_and_validate_protective_prices,
)
from .ops import (
    HeartbeatStore,
    Incident,
    IncidentLog,
    deal_totals,
    heartbeat_payload,
    operational_report,
    recent_managed_deals,
)
from .risk import RiskLimits, kill_switch_triggered
from .strategy import build_signals


ARM_PHRASE = "I_UNDERSTAND_LIVE_TRADING"
COMMENT_PREFIX = "fat:"


@dataclass(frozen=True)
class LiveEngineConfig:
    risk_per_trade: float = 0.0025
    max_daily_loss_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_lot_per_order: float = 0.02
    max_total_lots: float = 0.05
    max_open_positions: int = 3
    max_spread_pips: float = 2.0
    allowed_account_logins: tuple[int, ...] = ()
    require_account_allowlist: bool = False
    max_margin_fraction_of_equity: float = 0.25
    min_free_margin_fraction_after_order: float = 0.50
    max_tick_age_seconds: float = 30.0
    max_bar_age_seconds: float = 7200.0
    deal_reconcile_lookback_hours: float = 72.0
    magic: int = 56001
    deviation_points: int = 20
    state_path: str = "runtime/live_state.json"
    events_path: str = "runtime/live_events.csv"
    heartbeat_path: str = "runtime/live_heartbeat.json"
    incidents_path: str = "runtime/live_incidents.csv"


@dataclass
class LiveState:
    version: int = 4
    peak_equity: float = 0.0
    start_of_day_equity: float = 0.0
    current_day: str | None = None
    last_bar_time: str | None = None
    last_deal_time_msc: int = 0
    last_deal_ticket: int = 0
    last_incident_fingerprint: str | None = None
    pending_order_intent: dict[str, Any] | None = None
    halted: bool = False
    halt_reason: str | None = None


class LiveStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> LiveState:
        if not self.path.exists():
            return LiveState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        # Phase-7 state files remain compatible because new fields have defaults.
        allowed = {field.name for field in LiveState.__dataclass_fields__.values()}
        clean = {key: value for key, value in raw.items() if key in allowed}
        return LiveState(**clean)

    def save(self, state: LiveState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)


class LiveEventLog:
    columns = [
        "time",
        "event",
        "strategy",
        "side",
        "lots",
        "price",
        "stop_loss",
        "take_profit",
        "spread_pips",
        "ticket",
        "reason",
    ]

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, **row: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        payload = {key: row.get(key, "") for key in self.columns}
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns)
            if not exists:
                writer.writeheader()
            writer.writerow(payload)


def require_live_arming(live_enabled: bool, arm_phrase: str | None) -> None:
    if not live_enabled:
        raise RuntimeError("live.enabled must be true in config before real order execution is allowed")
    if arm_phrase != ARM_PHRASE:
        raise RuntimeError(f"live trading requires explicit operator arming: --arm-live {ARM_PHRASE}")


def spread_pips(tick: Any, spec: SymbolSpec) -> float:
    if spec.pip_size <= 0:
        raise ValueError("invalid broker pip size")
    return max(0.0, float(tick.ask - tick.bid) / spec.pip_size)


def risk_sized_lots(
    equity: float,
    risk_fraction: float,
    strategy_weight: float,
    stop_distance: float,
    spec: SymbolSpec,
    max_lot_per_order: float,
) -> float:
    if equity <= 0 or risk_fraction <= 0 or strategy_weight <= 0:
        return 0.0
    if stop_distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return 0.0
    risk_cash = equity * risk_fraction * strategy_weight
    cash_risk_per_lot = (stop_distance / spec.tick_size) * spec.tick_value
    if cash_risk_per_lot <= 0:
        return 0.0
    raw = min(risk_cash / cash_risk_per_lot, max_lot_per_order)
    return spec.normalize_volume(raw)


def broker_risk_sized_lots(
    broker: Any,
    symbol: str,
    equity: float,
    risk_fraction: float,
    strategy_weight: float,
    entry_price: float,
    stop_loss: float,
    spec: SymbolSpec,
    max_lot_per_order: float,
) -> float:
    if equity <= 0 or risk_fraction <= 0 or strategy_weight <= 0:
        return 0.0
    risk_cash = float(equity) * float(risk_fraction) * float(strategy_weight)
    loss_per_lot = float(broker.loss_per_lot_to_stop(symbol, 1 if stop_loss < entry_price else -1, entry_price, stop_loss))
    if loss_per_lot <= 0:
        return 0.0
    raw = min(risk_cash / loss_per_lot, float(max_lot_per_order))
    return spec.normalize_volume(raw)


def margin_safety_report(
    broker: Any,
    account: Any,
    symbol: str,
    side: int,
    lots: float,
    price: float,
    cfg: LiveEngineConfig,
) -> dict[str, Any]:
    required = float(broker.order_calc_margin(symbol, side, lots, price))
    equity = float(account.equity)
    projected_margin = float(account.margin) + required
    projected_free = float(account.margin_free) - required
    margin_fraction = float("inf") if equity <= 0 else projected_margin / equity
    free_fraction = float("-inf") if equity <= 0 else projected_free / equity
    checks = {
        "required_margin_nonnegative": required >= 0,
        "projected_margin_within_cap": margin_fraction <= cfg.max_margin_fraction_of_equity + 1e-12,
        "projected_free_margin_positive": projected_free > 0,
        "projected_free_margin_fraction": free_fraction + 1e-12 >= cfg.min_free_margin_fraction_after_order,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "required_margin": required,
        "projected_margin": projected_margin,
        "projected_free_margin": projected_free,
        "projected_margin_fraction": margin_fraction,
        "projected_free_margin_fraction": free_fraction,
    }


def execution_receipt_issue(
    receipt: ExecutionReceipt,
    requested_volume: float,
    volume_tolerance: float,
) -> str | None:
    if receipt.status == "PARTIAL":
        return "PARTIAL_FILL"
    if receipt.status == "PLACED":
        return "ORDER_ACCEPTED_UNCONFIRMED"
    if receipt.status != "FILLED":
        return "ORDER_STATUS_UNKNOWN"
    if abs(float(receipt.filled_volume) - float(requested_volume)) > max(float(volume_tolerance), 1e-12):
        return "FILL_VOLUME_MISMATCH"
    return None


def _strategy_name(position: BrokerPosition) -> str | None:
    if not position.comment.startswith(COMMENT_PREFIX):
        return None
    return position.comment[len(COMMENT_PREFIX) :]


def preflight_report(broker: Any, symbol: str, cfg: LiveEngineConfig, live_enabled: bool) -> dict[str, Any]:
    terminal = broker.terminal_snapshot()
    account = broker.account_snapshot()
    spec = broker.symbol_spec(symbol)
    tick = broker.current_tick(symbol)
    managed = broker.open_positions(symbol=symbol, magic=cfg.magic)
    current_spread = spread_pips(tick, spec)
    total_lots = sum(float(p.volume) for p in managed)
    account_allowed = (not cfg.require_account_allowlist) or (
        bool(cfg.allowed_account_logins) and int(account.login) in set(cfg.allowed_account_logins)
    )
    checks = {
        "config_live_enabled": bool(live_enabled),
        "terminal_connected": bool(terminal.connected),
        "terminal_trade_allowed": bool(terminal.trade_allowed),
        "hedging_account": bool(account.hedging),
        "account_login_allowed": account_allowed,
        "symbol_trade_allowed": bool(spec.trade_allowed),
        "tick_value_available": spec.tick_size > 0 and spec.tick_value > 0,
        "spread_within_cap": current_spread <= cfg.max_spread_pips,
        "position_count_within_cap": len(managed) <= cfg.max_open_positions,
        "total_lots_within_cap": total_lots <= cfg.max_total_lots + 1e-12,
        "free_margin_positive": account.margin_free > 0,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "terminal": terminal,
        "account": account,
        "symbol_spec": spec,
        "spread_pips": current_spread,
        "managed_positions": managed,
        "managed_total_lots": total_lots,
    }


class LiveTradingEngine:
    """Conservative live engine for a hedging MT5 account.

    Only the newest completed bar is evaluated. Missed historical bars are not
    replayed into live orders. Every real order requires both config enablement
    and an explicit operator arming phrase. Phase 8 adds operational freshness,
    position-integrity, broker-deal reconciliation and heartbeat checks before
    any new live order can be submitted.
    """

    def __init__(
        self,
        broker: Any,
        symbol: str,
        strategies: dict[str, dict[str, Any]],
        weights: dict[str, float],
        config: LiveEngineConfig,
        live_enabled: bool,
        arm_phrase: str | None,
    ) -> None:
        if not strategies:
            raise ValueError("at least one live strategy is required")
        missing = set(strategies).difference(weights)
        if missing:
            raise ValueError(f"missing live weights for: {sorted(missing)}")
        require_live_arming(live_enabled, arm_phrase)
        self.broker = broker
        self.symbol = symbol
        self.strategies = strategies
        total_weight = sum(max(0.0, float(weights[name])) for name in strategies)
        if total_weight <= 0:
            raise ValueError("live strategy weights must sum to a positive value")
        self.weights = {name: max(0.0, float(weights[name])) / total_weight for name in strategies}
        self.config = config
        self.live_enabled = live_enabled
        self.store = LiveStateStore(config.state_path)
        self.events = LiveEventLog(config.events_path)
        self.incidents = IncidentLog(config.incidents_path)
        self.heartbeat = HeartbeatStore(config.heartbeat_path)
        self.state = self.store.load()
        self.limits = RiskLimits(
            risk_per_trade=config.risk_per_trade,
            max_daily_loss_pct=config.max_daily_loss_pct,
            max_drawdown_pct=config.max_drawdown_pct,
        )

    def _persist_order_intent(
        self,
        ts: pd.Timestamp | datetime,
        *,
        action: str,
        strategy: str,
        side: int,
        lots: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        position_ticket: int = 0,
        position_id: int = 0,
    ) -> dict[str, Any]:
        intent = {
            "version": 3,
            "intent_id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "deal_cursor_before_submit": {
                "time_msc": int(self.state.last_deal_time_msc),
                "ticket": int(self.state.last_deal_ticket),
            },
            "action": action,
            "strategy": strategy,
            "symbol": self.symbol,
            "magic": int(self.config.magic),
            "side": int(side),
            "lots": float(lots),
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
            "position_ticket": int(position_ticket),
            "position_id": int(position_id or position_ticket),
            "bar_time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        }
        self.state.pending_order_intent = intent
        self.store.save(self.state)
        return intent


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

    def _clear_order_intent(self) -> None:
        self.state.pending_order_intent = None
        self.store.save(self.state)

    def _execution_fail_closed(
        self,
        ts: pd.Timestamp | datetime,
        code: str,
        detail: str,
        *,
        strategy: str = "",
        ticket: int = 0,
    ) -> None:
        incident = Incident(code=code, severity="CRITICAL", detail=detail, strategy=strategy, ticket=ticket)
        self._record_incidents([incident], ts)
        if not self.state.halted:
            self._halt_for_ops(pd.Timestamp(ts), [incident])
        else:
            self.store.save(self.state)

    def _managed_positions(self) -> list[BrokerPosition]:
        return self.broker.open_positions(symbol=self.symbol, magic=self.config.magic)

    def _log(self, event: str, ts: pd.Timestamp | datetime, strategy: str = "", **kwargs: Any) -> None:
        when = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        self.events.append(time=when, event=event, strategy=strategy, **kwargs)

    def _incident_fingerprint(self, incidents: list[Incident]) -> str:
        return "|".join(sorted(f"{item.severity}:{item.code}:{item.strategy}:{item.ticket}" for item in incidents))

    def _record_incidents(self, incidents: list[Incident], ts: pd.Timestamp | datetime) -> None:
        fingerprint = self._incident_fingerprint(incidents)
        if fingerprint == self.state.last_incident_fingerprint:
            return
        self.state.last_incident_fingerprint = fingerprint or None
        for incident in incidents:
            self.incidents.append(incident)
            self._log(
                "INCIDENT",
                ts,
                strategy=incident.strategy or "PORTFOLIO",
                side="",
                lots="",
                price="",
                stop_loss="",
                take_profit="",
                spread_pips="",
                ticket=incident.ticket or "",
                reason=f"{incident.severity}:{incident.code}:{incident.detail}",
            )

    def _operational_report(self, ts: pd.Timestamp) -> dict[str, Any]:
        return operational_report(
            self.broker,
            self.symbol,
            self.config.magic,
            set(self.strategies),
            ts,
            max_tick_age_seconds=self.config.max_tick_age_seconds,
            max_bar_age_seconds=self.config.max_bar_age_seconds,
        )

    def _write_heartbeat(self, report: dict[str, Any], status: str | None = None) -> None:
        self.heartbeat.write(
            heartbeat_payload(
                status=status or report["status"],
                symbol=self.symbol,
                last_bar_time=self.state.last_bar_time,
                account=report["account"],
                positions=report["positions"],
                incidents=report["incidents"],
                spread_pips=report["spread_pips"],
                tick_age_seconds_value=report["tick_age_seconds"],
                bar_age_seconds_value=report["bar_age_seconds"],
                last_deal_time_msc=self.state.last_deal_time_msc,
                last_deal_ticket=self.state.last_deal_ticket,
            )
        )

    def _reconcile_deals(self, ts: pd.Timestamp | datetime) -> dict[str, float | int]:
        deals = recent_managed_deals(
            self.broker,
            self.symbol,
            self.config.magic,
            after_time_msc=self.state.last_deal_time_msc,
            after_ticket=self.state.last_deal_ticket,
            lookback_hours=self.config.deal_reconcile_lookback_hours,
        )
        for deal in deals:
            strategy = deal.comment[len(COMMENT_PREFIX) :] if deal.comment.startswith(COMMENT_PREFIX) else ""
            self._log(
                "RECONCILE_DEAL",
                ts,
                strategy=strategy,
                side=deal.side,
                lots=deal.volume,
                price=deal.price,
                stop_loss="",
                take_profit="",
                spread_pips="",
                ticket=deal.ticket,
                reason=(
                    f"order={deal.order};position={deal.position_id};entry={deal.entry};"
                    f"profit={deal.profit:.2f};commission={deal.commission:.2f};swap={deal.swap:.2f}"
                ),
            )
        if deals:
            latest = max((int(item.time_msc), int(item.ticket)) for item in deals)
            self.state.last_deal_time_msc = latest[0]
            self.state.last_deal_ticket = latest[1]
        return deal_totals(deals)

    def _risk_gate(self, ts: pd.Timestamp) -> tuple[bool, str | None]:
        account = self.broker.account_snapshot()
        day = ts.date().isoformat()
        if self.state.current_day != day or self.state.start_of_day_equity <= 0:
            self.state.current_day = day
            self.state.start_of_day_equity = account.equity
        if self.state.peak_equity <= 0:
            self.state.peak_equity = account.equity
        self.state.peak_equity = max(self.state.peak_equity, account.equity)
        killed, reason = kill_switch_triggered(
            self.state.start_of_day_equity,
            self.state.peak_equity,
            account.equity,
            self.limits,
        )
        return killed, reason

    def emergency_flatten(self, reason: str = "operator_flatten") -> list[int]:
        closed: list[int] = []
        now = datetime.now(timezone.utc)
        for position in list(self._managed_positions()):
            strategy = _strategy_name(position) or ""
            self._persist_order_intent(
                now, action="FLATTEN", strategy=strategy, side=-position.side,
                lots=position.volume, position_ticket=position.ticket,
                position_id=int(getattr(position, "identifier", 0) or position.ticket),
            )
            try:
                receipt = self.broker.close_position(
                    position,
                    deviation_points=self.config.deviation_points,
                    live_enabled=self.live_enabled,
                )
            except BrokerOrderRejected as exc:
                self._clear_order_intent()
                self._execution_fail_closed(
                    now, "EMERGENCY_CLOSE_REJECTED", str(exc), strategy=strategy, ticket=position.ticket
                )
                raise
            except BrokerSubmissionAmbiguous as exc:
                self._execution_fail_closed(
                    now, "EMERGENCY_CLOSE_AMBIGUOUS", str(exc), strategy=strategy, ticket=position.ticket
                )
                raise
            except Exception as exc:
                self._execution_fail_closed(
                    now, "EMERGENCY_CLOSE_EXCEPTION", str(exc), strategy=strategy, ticket=position.ticket
                )
                raise

            self._persist_execution_receipt(receipt)
            issue = execution_receipt_issue(
                receipt, position.volume, max(self.broker.symbol_spec(position.symbol).volume_step / 2.0, 1e-12)
            )
            if issue:
                self._execution_fail_closed(
                    now, issue,
                    f"emergency close status={receipt.status}; requested={position.volume}; filled={receipt.filled_volume}",
                    strategy=strategy, ticket=receipt.ticket or position.ticket,
                )
                raise RuntimeError(f"emergency flatten not fully confirmed: {issue}")

            closed.append(position.ticket)
            self._log(
                "FLATTEN",
                now,
                strategy=strategy,
                side=position.side,
                lots=receipt.filled_volume,
                price=receipt.price or "",
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                spread_pips="",
                ticket=receipt.ticket or position.ticket,
                reason=f"{reason};deal={receipt.deal}",
            )
            self._clear_order_intent()
        return closed

    def _halt_and_flatten(self, ts: pd.Timestamp, reason: str) -> None:
        self.emergency_flatten(f"kill_switch:{reason}")
        self.state.halted = True
        self.state.halt_reason = reason
        self._log(
            "HALT",
            ts,
            strategy="PORTFOLIO",
            side="",
            lots="",
            price="",
            stop_loss="",
            take_profit="",
            spread_pips="",
            ticket="",
            reason=reason,
        )
        self.store.save(self.state)

    def _halt_for_ops(self, ts: pd.Timestamp, incidents: list[Incident]) -> None:
        codes = sorted({item.code for item in incidents if item.severity == "CRITICAL"})
        reason = "ops:" + ",".join(codes)
        self.state.halted = True
        self.state.halt_reason = reason
        self._log(
            "HALT",
            ts,
            strategy="PORTFOLIO",
            side="",
            lots="",
            price="",
            stop_loss="",
            take_profit="",
            spread_pips="",
            ticket="",
            reason=reason,
        )
        # Operational ambiguity is fail-closed for new orders but does not
        # automatically flatten; the operator must inspect the broker state.
        self.store.save(self.state)

    def process_latest(self, completed_bars: pd.DataFrame) -> dict[str, Any]:
        required = {"open", "high", "low", "close"}
        missing = required.difference(completed_bars.columns)
        if missing:
            raise ValueError(f"missing OHLC columns: {sorted(missing)}")
        if completed_bars.empty or not isinstance(completed_bars.index, pd.DatetimeIndex):
            raise ValueError("completed live bars require a non-empty DatetimeIndex")

        bars = completed_bars.sort_index()
        ts = bars.index[-1]

        report = self._operational_report(ts)
        self._record_incidents(report["incidents"], ts)
        self._reconcile_deals(ts)

        if self.state.pending_order_intent is not None:
            pending = self.state.pending_order_intent
            incident = Incident(
                code="PENDING_ORDER_INTENT",
                severity="CRITICAL",
                detail=(
                    f"unresolved {pending.get('action', 'ORDER')} intent for "
                    f"{pending.get('strategy', '')} lots={pending.get('lots', '')}; "
                    "inspect broker positions/deals before clearing the intent"
                ),
                strategy=str(pending.get("strategy", "")),
                ticket=int(pending.get("position_ticket", 0) or 0),
            )
            self._record_incidents([incident], ts)
            if not self.state.halted:
                self._halt_for_ops(ts, [incident])
            self.store.save(self.state)
            self._write_heartbeat(report, status="HALTED")
            return self.snapshot()

        critical = [item for item in report["incidents"] if item.severity == "CRITICAL"]
        if critical and not self.state.halted:
            self._halt_for_ops(ts, critical)
        if critical:
            self.store.save(self.state)
            self._write_heartbeat(report, status="HALTED")
            return self.snapshot()

        if self.state.halted:
            self.store.save(self.state)
            self._write_heartbeat(report, status="HALTED")
            return self.snapshot()

        warnings = [item for item in report["incidents"] if item.severity == "WARN"]
        if warnings:
            # A stale tick/bar is retryable. Do not advance last_bar_time, so the
            # same completed bar can be reconsidered after market data recovers.
            self.store.save(self.state)
            self._write_heartbeat(report, status="DEGRADED")
            return self.snapshot()

        self.state.last_incident_fingerprint = None
        last_seen = pd.Timestamp(self.state.last_bar_time) if self.state.last_bar_time else None
        if last_seen is not None and ts <= last_seen:
            self.store.save(self.state)
            self._write_heartbeat(report, status="OK")
            return self.snapshot()

        killed, reason = self._risk_gate(ts)
        if killed and reason:
            self._halt_and_flatten(ts, reason)
            refreshed = self._operational_report(ts)
            self._reconcile_deals(ts)
            self._write_heartbeat(refreshed, status="HALTED")
            return self.snapshot()

        preflight = preflight_report(self.broker, self.symbol, self.config, self.live_enabled)
        if not preflight["ok"]:
            failed = [name for name, passed in preflight["checks"].items() if not passed]
            self._log(
                "REJECT",
                ts,
                strategy="PORTFOLIO",
                side="",
                lots="",
                price="",
                stop_loss="",
                take_profit="",
                spread_pips=preflight["spread_pips"],
                ticket="",
                reason="preflight:" + ",".join(failed),
            )
            self.state.last_bar_time = ts.isoformat()
            self.store.save(self.state)
            self._write_heartbeat(report, status="DEGRADED")
            return self.snapshot()

        account = preflight["account"]
        spec = preflight["symbol_spec"]
        tick = self.broker.current_tick(self.symbol)
        current_spread = spread_pips(tick, spec)
        managed = self._managed_positions()
        by_strategy: dict[str, BrokerPosition] = {}
        for position in managed:
            name = _strategy_name(position)
            if name and name not in by_strategy:
                by_strategy[name] = position

        total_lots = sum(float(p.volume) for p in managed)
        open_count = len(managed)

        for strategy, params in self.strategies.items():
            frame = build_signals(bars, strategy, params)
            row = frame.iloc[-1]
            side = int(row["signal"])
            if side == 0 or pd.isna(row["stop_distance"]) or pd.isna(row["take_profit_distance"]):
                continue

            existing = by_strategy.get(strategy)
            if existing is not None and existing.side == side:
                continue
            if existing is not None and existing.side != side:
                self._persist_order_intent(
                    ts,
                    action="CLOSE",
                    strategy=strategy,
                    side=-existing.side,
                    lots=existing.volume,
                    position_ticket=existing.ticket,
                    position_id=int(getattr(existing, "identifier", 0) or existing.ticket),
                )
                try:
                    close_receipt = self.broker.close_position(
                        existing,
                        deviation_points=self.config.deviation_points,
                        live_enabled=self.live_enabled,
                    )
                except BrokerOrderRejected as exc:
                    self._clear_order_intent()
                    self._log(
                        "CLOSE_REJECT", ts, strategy=strategy, side=existing.side, lots=existing.volume,
                        price="", stop_loss=existing.stop_loss, take_profit=existing.take_profit,
                        spread_pips=current_spread, ticket=existing.ticket, reason=str(exc),
                    )
                    continue
                except BrokerSubmissionAmbiguous as exc:
                    self._execution_fail_closed(
                        ts, "CLOSE_SUBMISSION_AMBIGUOUS", str(exc), strategy=strategy, ticket=existing.ticket
                    )
                    self._write_heartbeat(report, status="HALTED")
                    return self.snapshot()
                except Exception as exc:
                    self._execution_fail_closed(
                        ts, "CLOSE_EXECUTION_EXCEPTION", str(exc), strategy=strategy, ticket=existing.ticket
                    )
                    self._write_heartbeat(report, status="HALTED")
                    return self.snapshot()

                self._persist_execution_receipt(close_receipt)
                close_issue = execution_receipt_issue(
                    close_receipt, existing.volume, max(spec.volume_step / 2.0, 1e-12)
                )
                if close_issue:
                    self._execution_fail_closed(
                        ts,
                        close_issue,
                        (
                            f"close status={close_receipt.status}; requested={existing.volume}; "
                            f"filled={close_receipt.filled_volume}; order={close_receipt.order}; deal={close_receipt.deal}"
                        ),
                        strategy=strategy,
                        ticket=close_receipt.ticket or existing.ticket,
                    )
                    self._write_heartbeat(report, status="HALTED")
                    return self.snapshot()

                self._log(
                    "CLOSE",
                    ts,
                    strategy=strategy,
                    side=existing.side,
                    lots=close_receipt.filled_volume,
                    price=close_receipt.price or "",
                    stop_loss=existing.stop_loss,
                    take_profit=existing.take_profit,
                    spread_pips=current_spread,
                    ticket=close_receipt.ticket or existing.ticket,
                    reason=f"opposite_signal;deal={close_receipt.deal}",
                )
                self._clear_order_intent()
                total_lots = max(0.0, total_lots - close_receipt.filled_volume)
                open_count = max(0, open_count - 1)

            if current_spread > self.config.max_spread_pips:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots="",
                    price="",
                    stop_loss="",
                    take_profit="",
                    spread_pips=current_spread,
                    ticket="",
                    reason="spread_cap",
                )
                continue
            if open_count >= self.config.max_open_positions:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots="",
                    price="",
                    stop_loss="",
                    take_profit="",
                    spread_pips=current_spread,
                    ticket="",
                    reason="position_count_cap",
                )
                continue

            stop_distance = float(row["stop_distance"])
            tp_distance = float(row["take_profit_distance"])
            tick = self.broker.current_tick(self.symbol)
            market_price = tick.ask if side > 0 else tick.bid
            raw_stop = market_price - side * stop_distance
            raw_take = market_price + side * tp_distance
            try:
                market_price, stop_loss, take_profit = normalize_and_validate_protective_prices(
                    spec, side, market_price, raw_stop, raw_take
                )
                lots = broker_risk_sized_lots(
                    self.broker,
                    self.symbol,
                    account.equity,
                    self.config.risk_per_trade,
                    self.weights[strategy],
                    market_price,
                    stop_loss,
                    spec,
                    self.config.max_lot_per_order,
                )
            except Exception as exc:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots=0.0,
                    price=market_price,
                    stop_loss=raw_stop,
                    take_profit=raw_take,
                    spread_pips=current_spread,
                    ticket="",
                    reason=f"broker_risk_or_protection:{exc}",
                )
                continue
            if lots <= 0:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots=0.0,
                    price=market_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    spread_pips=current_spread,
                    ticket="",
                    reason="position_size_zero",
                )
                continue
            if total_lots + lots > self.config.max_total_lots + 1e-12:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots=lots,
                    price=market_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    spread_pips=current_spread,
                    ticket="",
                    reason="total_lot_cap",
                )
                continue
            try:
                margin = margin_safety_report(
                    self.broker, account, self.symbol, side, lots, market_price, self.config
                )
            except Exception as exc:
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots=lots,
                    price=market_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    spread_pips=current_spread,
                    ticket="",
                    reason=f"broker_margin_calc_failed:{exc}",
                )
                continue
            if not margin["ok"]:
                failed = ",".join(name for name, passed in margin["checks"].items() if not passed)
                self._log(
                    "REJECT",
                    ts,
                    strategy=strategy,
                    side=side,
                    lots=lots,
                    price=market_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    spread_pips=current_spread,
                    ticket="",
                    reason=f"margin_gate:{failed};required={margin['required_margin']:.2f}",
                )
                continue

            order = BrokerOrder(
                symbol=self.symbol,
                side=side,
                lots=lots,
                stop_loss=stop_loss,
                take_profit=take_profit,
                magic=self.config.magic,
                deviation_points=self.config.deviation_points,
                comment=f"{COMMENT_PREFIX}{strategy}"[:31],
            )
            self._persist_order_intent(
                ts,
                action="ENTRY",
                strategy=strategy,
                side=side,
                lots=lots,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            try:
                receipt = self.broker.market_order(order, live_enabled=self.live_enabled)
            except BrokerOrderRejected as exc:
                self._clear_order_intent()
                self._log(
                    "REJECT", ts, strategy=strategy, side=side, lots=lots, price=market_price,
                    stop_loss=stop_loss, take_profit=take_profit, spread_pips=current_spread,
                    ticket="", reason=f"broker_reject:{exc}",
                )
                continue
            except BrokerSubmissionAmbiguous as exc:
                self._execution_fail_closed(
                    ts, "ORDER_SUBMISSION_AMBIGUOUS", str(exc), strategy=strategy
                )
                self._write_heartbeat(report, status="HALTED")
                return self.snapshot()
            except Exception as exc:
                self._execution_fail_closed(
                    ts, "ORDER_EXECUTION_EXCEPTION", str(exc), strategy=strategy
                )
                self._write_heartbeat(report, status="HALTED")
                return self.snapshot()

            self._persist_execution_receipt(receipt)
            issue = execution_receipt_issue(receipt, lots, max(spec.volume_step / 2.0, 1e-12))
            if issue:
                self._execution_fail_closed(
                    ts,
                    issue,
                    (
                        f"entry status={receipt.status}; requested={lots}; filled={receipt.filled_volume}; "
                        f"order={receipt.order}; deal={receipt.deal}"
                    ),
                    strategy=strategy,
                    ticket=receipt.ticket,
                )
                self._write_heartbeat(report, status="HALTED")
                return self.snapshot()

            self._log(
                "ENTRY",
                ts,
                strategy=strategy,
                side=side,
                lots=receipt.filled_volume,
                price=receipt.price or market_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                spread_pips=current_spread,
                ticket=receipt.ticket,
                reason=f"latest_completed_bar_signal;deal={receipt.deal};retcode={receipt.retcode}",
            )
            # Clear only after the durable ENTRY audit row exists. If the process
            # dies after broker submission but before here, restart sees the
            # pending intent and refuses to resend.
            self._clear_order_intent()
            total_lots += receipt.filled_volume
            open_count += 1

        self.state.last_bar_time = ts.isoformat()
        account_after = self.broker.account_snapshot()
        self.state.peak_equity = max(self.state.peak_equity, account_after.equity)
        self._reconcile_deals(ts)
        self.store.save(self.state)
        refreshed = self._operational_report(ts)
        self._record_incidents(refreshed["incidents"], ts)
        self._write_heartbeat(refreshed, status="OK" if refreshed["ok"] else "DEGRADED")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        account = self.broker.account_snapshot()
        managed = self._managed_positions()
        return {
            "symbol": self.symbol,
            "balance": account.balance,
            "equity": account.equity,
            "free_margin": account.margin_free,
            "peak_equity": self.state.peak_equity,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "last_bar_time": self.state.last_bar_time,
            "last_deal_time_msc": self.state.last_deal_time_msc,
            "last_deal_ticket": self.state.last_deal_ticket,
            "pending_order_intent_id": (self.state.pending_order_intent or {}).get("intent_id"),
            "managed_positions": len(managed),
            "managed_total_lots": sum(float(p.volume) for p in managed),
            "heartbeat_path": self.config.heartbeat_path,
            "incidents_path": self.config.incidents_path,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
