from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .market_clock import WeeklyFxSessionPolicy, evaluate_market_clock
from .mt5_broker import BrokerDeal, BrokerPosition, BrokerTick


COMMENT_PREFIX = "fat:"


@dataclass(frozen=True)
class Incident:
    code: str
    severity: str
    detail: str
    strategy: str = ""
    ticket: int = 0


class IncidentLog:
    columns = ["time", "severity", "code", "strategy", "ticket", "detail"]

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, incident: Incident, when: datetime | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        payload = {
            "time": (when or datetime.now(timezone.utc)).isoformat(),
            "severity": incident.severity,
            "code": incident.code,
            "strategy": incident.strategy,
            "ticket": incident.ticket or "",
            "detail": incident.detail,
        }
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns)
            if not exists:
                writer.writeheader()
            writer.writerow(payload)


class HeartbeatStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        temp.replace(self.path)


def _as_utc(ts: pd.Timestamp | datetime) -> datetime:
    value = pd.Timestamp(ts)
    if value.tzinfo is None:
        value = value.tz_localize("UTC")
    else:
        value = value.tz_convert("UTC")
    return value.to_pydatetime()


def tick_age_seconds(tick: BrokerTick, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    if tick.time_msc <= 0:
        return float("inf")
    tick_time = datetime.fromtimestamp(tick.time_msc / 1000.0, tz=timezone.utc)
    return max(0.0, (current - tick_time).total_seconds())


def bar_age_seconds(bar_time: pd.Timestamp | datetime, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - _as_utc(bar_time)).total_seconds())


def market_freshness_incidents(
    bar_time: pd.Timestamp | datetime,
    tick: BrokerTick,
    *,
    max_tick_age_seconds: float,
    max_bar_age_seconds: float,
    suppress_staleness: bool = False,
    now: datetime | None = None,
) -> list[Incident]:
    if suppress_staleness:
        return []
    incidents: list[Incident] = []
    tick_age = tick_age_seconds(tick, now)
    bar_age = bar_age_seconds(bar_time, now)
    if tick_age > max_tick_age_seconds:
        incidents.append(
            Incident(
                code="STALE_TICK",
                severity="WARN",
                detail=f"tick age {tick_age:.1f}s exceeds cap {max_tick_age_seconds:.1f}s",
            )
        )
    if bar_age > max_bar_age_seconds:
        incidents.append(
            Incident(
                code="STALE_BAR",
                severity="WARN",
                detail=f"completed-bar age {bar_age:.1f}s exceeds cap {max_bar_age_seconds:.1f}s",
            )
        )
    return incidents


def position_integrity_incidents(
    positions: Iterable[BrokerPosition],
    allowed_strategies: set[str],
) -> list[Incident]:
    incidents: list[Incident] = []
    by_strategy: dict[str, list[BrokerPosition]] = {}
    for position in positions:
        if not position.comment.startswith(COMMENT_PREFIX):
            incidents.append(
                Incident(
                    code="UNKNOWN_MANAGED_COMMENT",
                    severity="CRITICAL",
                    detail=f"managed-magic position has unexpected comment {position.comment!r}",
                    ticket=position.ticket,
                )
            )
            continue
        strategy = position.comment[len(COMMENT_PREFIX) :]
        if strategy not in allowed_strategies:
            incidents.append(
                Incident(
                    code="UNKNOWN_MANAGED_STRATEGY",
                    severity="CRITICAL",
                    detail=f"managed position references strategy {strategy!r} not present in the live bundle",
                    strategy=strategy,
                    ticket=position.ticket,
                )
            )
        by_strategy.setdefault(strategy, []).append(position)
        if position.stop_loss <= 0 or position.take_profit <= 0:
            incidents.append(
                Incident(
                    code="MISSING_PROTECTIVE_EXIT",
                    severity="CRITICAL",
                    detail="managed position is missing stop-loss or take-profit",
                    strategy=strategy,
                    ticket=position.ticket,
                )
            )

    for strategy, items in by_strategy.items():
        if len(items) > 1:
            tickets = ",".join(str(item.ticket) for item in items)
            incidents.append(
                Incident(
                    code="DUPLICATE_STRATEGY_POSITION",
                    severity="CRITICAL",
                    detail=f"strategy has {len(items)} simultaneous managed positions: {tickets}",
                    strategy=strategy,
                )
            )
    return incidents


def operational_report(
    broker: Any,
    symbol: str,
    magic: int,
    allowed_strategies: set[str],
    latest_completed_bar: pd.Timestamp | datetime,
    *,
    max_tick_age_seconds: float,
    max_bar_age_seconds: float,
    market_session_enabled: bool = True,
    market_sunday_open_utc: str = "22:00",
    market_friday_close_utc: str = "22:00",
    market_transition_grace_seconds: float = 3600.0,
    max_future_tick_seconds: float = 5.0,
    max_future_bar_seconds: float = 300.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    terminal = broker.terminal_snapshot()
    account = broker.account_snapshot()
    tick = broker.current_tick(symbol)
    spec = broker.symbol_spec(symbol)
    positions = broker.open_positions(symbol=symbol, magic=magic)
    incidents: list[Incident] = []
    current = now or datetime.now(timezone.utc)
    policy = WeeklyFxSessionPolicy(
        enabled=market_session_enabled,
        sunday_open_utc=market_sunday_open_utc,
        friday_close_utc=market_friday_close_utc,
        transition_grace_seconds=market_transition_grace_seconds,
        max_future_tick_seconds=max_future_tick_seconds,
        max_future_bar_seconds=max_future_bar_seconds,
    )
    clock = evaluate_market_clock(
        latest_completed_bar,
        tick.time_msc,
        now=current,
        policy=policy,
    )

    if not terminal.connected:
        incidents.append(Incident("MT5_DISCONNECTED", "CRITICAL", "MT5 terminal reports connected=false"))
    if not terminal.trade_allowed:
        incidents.append(Incident("TERMINAL_TRADING_DISABLED", "CRITICAL", "MT5 terminal reports trade_allowed=false"))
    if clock["tick_future_violation"]:
        incidents.append(
            Incident(
                "BROKER_TICK_IN_FUTURE",
                "CRITICAL",
                f"broker tick is {float(clock['tick_clock_offset_seconds']):.1f}s ahead of host UTC",
            )
        )
    if clock["bar_future_violation"]:
        incidents.append(
            Incident(
                "COMPLETED_BAR_IN_FUTURE",
                "CRITICAL",
                f"completed bar is {float(clock['bar_clock_offset_seconds']):.1f}s ahead of host UTC",
            )
        )

    incidents.extend(
        market_freshness_incidents(
            latest_completed_bar,
            tick,
            max_tick_age_seconds=max_tick_age_seconds,
            max_bar_age_seconds=max_bar_age_seconds,
            suppress_staleness=bool(clock["staleness_suppressed"]),
            now=current,
        )
    )
    incidents.extend(position_integrity_incidents(positions, allowed_strategies))

    spread = float("inf") if spec.pip_size <= 0 else max(0.0, (tick.ask - tick.bid) / spec.pip_size)
    severity = "CRITICAL" if any(item.severity == "CRITICAL" for item in incidents) else "WARN" if incidents else "OK"
    return {
        "status": severity,
        "ok": severity == "OK",
        "terminal": terminal,
        "account": account,
        "tick": tick,
        "symbol_spec": spec,
        "positions": positions,
        "spread_pips": spread,
        "tick_age_seconds": tick_age_seconds(tick, current),
        "bar_age_seconds": bar_age_seconds(latest_completed_bar, current),
        "market_state": clock["state"],
        "market_reason": clock["reason"],
        "next_market_transition_utc": clock["next_transition_utc"],
        "staleness_suppressed": bool(clock["staleness_suppressed"]),
        "tick_clock_offset_seconds": clock["tick_clock_offset_seconds"],
        "bar_clock_offset_seconds": clock["bar_clock_offset_seconds"],
        "incidents": incidents,
    }


def recent_managed_deals(
    broker: Any,
    symbol: str,
    magic: int,
    *,
    after_time_msc: int = 0,
    after_ticket: int = 0,
    lookback_hours: float = 48.0,
    now: datetime | None = None,
) -> list[BrokerDeal]:
    current = now or datetime.now(timezone.utc)
    if after_time_msc > 0:
        start = datetime.fromtimestamp(max(0, after_time_msc - 1) / 1000.0, tz=timezone.utc)
    else:
        start = current - timedelta(hours=lookback_hours)
    deals = broker.history_deals(start, current, symbol=symbol, magic=magic)
    if int(after_ticket) > 0:
        cursor = (int(after_time_msc), int(after_ticket))
        selected = [deal for deal in deals if (int(deal.time_msc), int(deal.ticket)) > cursor]
    else:
        # Backward compatibility: callers that only provide the historical
        # time_msc cursor expect all deals at that exact millisecond to be
        # considered already consumed. Phase 17 uses the full tuple whenever
        # last_deal_ticket is available.
        selected = [deal for deal in deals if int(deal.time_msc) > int(after_time_msc)]
    return sorted(selected, key=lambda item: (int(item.time_msc), int(item.ticket)))


def deal_totals(deals: Iterable[BrokerDeal]) -> dict[str, float | int]:
    items = list(deals)
    return {
        "count": len(items),
        "profit": float(sum(item.profit for item in items)),
        "commission": float(sum(item.commission for item in items)),
        "swap": float(sum(item.swap for item in items)),
    }


def heartbeat_payload(
    *,
    status: str,
    symbol: str,
    last_bar_time: str | None,
    account: Any,
    positions: Iterable[BrokerPosition],
    incidents: Iterable[Incident],
    spread_pips: float | None = None,
    tick_age_seconds_value: float | None = None,
    bar_age_seconds_value: float | None = None,
    last_deal_time_msc: int = 0,
    last_deal_ticket: int = 0,
    market_state: str | None = None,
    market_reason: str | None = None,
    next_market_transition_utc: str | None = None,
    staleness_suppressed: bool = False,
    tick_clock_offset_seconds_value: float | None = None,
    bar_clock_offset_seconds_value: float | None = None,
) -> dict[str, Any]:
    pos = list(positions)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "symbol": symbol,
        "last_bar_time": last_bar_time,
        "last_deal_time_msc": int(last_deal_time_msc),
        "last_deal_ticket": int(last_deal_ticket),
        "account": {
            "login": int(account.login),
            "currency": str(account.currency),
            "balance": float(account.balance),
            "equity": float(account.equity),
            "margin": float(account.margin),
            "margin_free": float(account.margin_free),
            "margin_level": float(account.margin_level),
        },
        "managed_positions": [asdict(item) for item in pos],
        "managed_total_lots": float(sum(item.volume for item in pos)),
        "spread_pips": spread_pips,
        "tick_age_seconds": tick_age_seconds_value,
        "bar_age_seconds": bar_age_seconds_value,
        "market_state": market_state,
        "market_reason": market_reason,
        "next_market_transition_utc": next_market_transition_utc,
        "staleness_suppressed": bool(staleness_suppressed),
        "tick_clock_offset_seconds": tick_clock_offset_seconds_value,
        "bar_clock_offset_seconds": bar_clock_offset_seconds_value,
        "incidents": [asdict(item) for item in incidents],
    }
