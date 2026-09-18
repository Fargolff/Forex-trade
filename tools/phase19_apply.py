from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected Phase 19 patch anchor missing in {path}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


MARKET_CLOCK = r'''from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

import pandas as pd


_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm_utc(value: str) -> tuple[int, int]:
    text = str(value).strip()
    if not _HHMM.fullmatch(text):
        raise ValueError(f"UTC session time must be HH:MM, got {value!r}")
    hour, minute = text.split(":", 1)
    return int(hour), int(minute)


def _as_utc(value: pd.Timestamp | datetime) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


@dataclass(frozen=True)
class WeeklyFxSessionPolicy:
    enabled: bool = True
    sunday_open_utc: str = "22:00"
    friday_close_utc: str = "22:00"
    transition_grace_seconds: float = 3600.0
    max_future_tick_seconds: float = 5.0
    max_future_bar_seconds: float = 300.0

    def __post_init__(self) -> None:
        parse_hhmm_utc(self.sunday_open_utc)
        parse_hhmm_utc(self.friday_close_utc)
        if self.transition_grace_seconds < 0:
            raise ValueError("transition_grace_seconds cannot be negative")
        if self.max_future_tick_seconds < 0:
            raise ValueError("max_future_tick_seconds cannot be negative")
        if self.max_future_bar_seconds < 0:
            raise ValueError("max_future_bar_seconds cannot be negative")


def _week_start(current: datetime) -> datetime:
    return (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _boundary(week_start: datetime, weekday: int, hhmm: str, weeks: int = 0) -> datetime:
    hour, minute = parse_hhmm_utc(hhmm)
    return week_start + timedelta(weeks=weeks, days=weekday, hours=hour, minutes=minute)


def _nominal_market_open(current: datetime, policy: WeeklyFxSessionPolicy) -> bool:
    if not policy.enabled:
        return True
    weekday = current.weekday()  # Monday=0 ... Sunday=6
    minute_of_day = current.hour * 60 + current.minute
    friday_hour, friday_minute = parse_hhmm_utc(policy.friday_close_utc)
    sunday_hour, sunday_minute = parse_hhmm_utc(policy.sunday_open_utc)
    friday_close = friday_hour * 60 + friday_minute
    sunday_open = sunday_hour * 60 + sunday_minute

    if weekday <= 3:
        return True
    if weekday == 4:
        return minute_of_day < friday_close
    if weekday == 5:
        return False
    return minute_of_day >= sunday_open


def market_session_state(
    now: datetime | None = None,
    policy: WeeklyFxSessionPolicy | None = None,
) -> dict[str, str | float | bool | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cfg = policy or WeeklyFxSessionPolicy()

    if not cfg.enabled:
        return {
            "state": "OPEN",
            "reason": "session_filter_disabled",
            "next_transition_utc": None,
            "staleness_suppressed": False,
        }

    week = _week_start(current)
    boundaries: list[tuple[datetime, str]] = []
    for shift in (-1, 0, 1):
        boundaries.append((_boundary(week, 4, cfg.friday_close_utc, shift), "weekly_close"))
        boundaries.append((_boundary(week, 6, cfg.sunday_open_utc, shift), "weekly_open"))
    boundaries.sort(key=lambda item: item[0])

    grace = float(cfg.transition_grace_seconds)
    if grace > 0:
        near = min(boundaries, key=lambda item: abs((current - item[0]).total_seconds()))
        distance = abs((current - near[0]).total_seconds())
        if distance <= grace:
            next_boundary = next((stamp for stamp, _ in boundaries if stamp > current), None)
            return {
                "state": "TRANSITION",
                "reason": f"near_{near[1]}",
                "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
                "staleness_suppressed": True,
            }

    opened = _nominal_market_open(current, cfg)
    next_boundary = next((stamp for stamp, _ in boundaries if stamp > current), None)
    return {
        "state": "OPEN" if opened else "CLOSED",
        "reason": "weekly_session_open" if opened else "weekly_session_closed",
        "next_transition_utc": next_boundary.isoformat() if next_boundary else None,
        "staleness_suppressed": not opened,
    }


def evaluate_market_clock(
    bar_time: pd.Timestamp | datetime,
    tick_time_msc: int,
    *,
    now: datetime | None = None,
    policy: WeeklyFxSessionPolicy | None = None,
) -> dict[str, str | float | bool | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cfg = policy or WeeklyFxSessionPolicy()
    session = market_session_state(current, cfg)

    tick_offset: float | None = None
    if int(tick_time_msc) > 0:
        tick_time = datetime.fromtimestamp(int(tick_time_msc) / 1000.0, tz=timezone.utc)
        tick_offset = (tick_time - current).total_seconds()

    bar_utc = _as_utc(bar_time)
    bar_offset = (bar_utc - current).total_seconds()
    return {
        **session,
        "tick_clock_offset_seconds": tick_offset,
        "bar_clock_offset_seconds": bar_offset,
        "tick_future_violation": bool(tick_offset is not None and tick_offset > cfg.max_future_tick_seconds),
        "bar_future_violation": bool(bar_offset > cfg.max_future_bar_seconds),
    }
'''

TESTS = r'''from datetime import datetime, timezone

import pandas as pd

from src.market_clock import WeeklyFxSessionPolicy, evaluate_market_clock, market_session_state
from src.mt5_broker import AccountSnapshot, BrokerTick, SymbolSpec, TerminalSnapshot
from src.ops import operational_report


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tick(value: str) -> BrokerTick:
    stamp = int(_utc(value).timestamp() * 1000)
    return BrokerTick(bid=1.1000, ask=1.1001, time_msc=stamp)


def test_weekend_session_is_closed_and_boundaries_are_transition():
    policy = WeeklyFxSessionPolicy(
        sunday_open_utc="22:00",
        friday_close_utc="22:00",
        transition_grace_seconds=3600,
    )
    assert market_session_state(_utc("2026-09-19T12:00:00Z"), policy)["state"] == "CLOSED"
    assert market_session_state(_utc("2026-09-18T21:30:00Z"), policy)["state"] == "TRANSITION"
    assert market_session_state(_utc("2026-09-20T21:30:00Z"), policy)["state"] == "TRANSITION"
    assert market_session_state(_utc("2026-09-21T10:00:00Z"), policy)["state"] == "OPEN"


def test_future_tick_and_future_completed_bar_are_detected():
    policy = WeeklyFxSessionPolicy(max_future_tick_seconds=5, max_future_bar_seconds=60)
    report = evaluate_market_clock(
        pd.Timestamp("2026-09-21T10:02:00Z"),
        int(_utc("2026-09-21T10:00:30Z").timestamp() * 1000),
        now=_utc("2026-09-21T10:00:00Z"),
        policy=policy,
    )
    assert report["tick_future_violation"] is True
    assert report["bar_future_violation"] is True
    assert report["tick_clock_offset_seconds"] == 30.0
    assert report["bar_clock_offset_seconds"] == 120.0


class FakeBroker:
    def __init__(self, tick: BrokerTick):
        self._tick = tick

    def terminal_snapshot(self):
        return TerminalSnapshot(connected=True, trade_allowed=True, dlls_allowed=True)

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

    def current_tick(self, symbol):
        return self._tick

    def symbol_spec(self, symbol):
        return SymbolSpec(
            symbol=symbol,
            digits=5,
            point=0.00001,
            tick_size=0.00001,
            tick_value=1.0,
            contract_size=100000.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_allowed=True,
            filling_mode=0,
        )

    def open_positions(self, symbol=None, magic=None):
        return []


def test_weekend_suppresses_stale_market_data_only():
    broker = FakeBroker(_tick("2026-09-18T21:59:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-18T21:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        market_session_enabled=True,
        market_sunday_open_utc="22:00",
        market_friday_close_utc="22:00",
        market_transition_grace_seconds=3600,
        now=_utc("2026-09-19T12:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "CLOSED"
    assert report["staleness_suppressed"] is True
    assert "STALE_TICK" not in codes
    assert "STALE_BAR" not in codes
    assert report["status"] == "OK"


def test_open_market_keeps_stale_data_fail_closed_warning():
    broker = FakeBroker(_tick("2026-09-21T09:50:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-21T08:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=3600,
        now=_utc("2026-09-21T10:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "OPEN"
    assert "STALE_TICK" in codes
    assert "STALE_BAR" in codes
    assert report["status"] == "WARN"


def test_clock_violation_remains_critical_even_when_market_closed():
    broker = FakeBroker(_tick("2026-09-19T12:01:00Z"))
    report = operational_report(
        broker,
        "EURUSD",
        56001,
        {"ema_trend"},
        pd.Timestamp("2026-09-18T21:00:00Z"),
        max_tick_age_seconds=30,
        max_bar_age_seconds=7200,
        max_future_tick_seconds=5,
        now=_utc("2026-09-19T12:00:00Z"),
    )
    codes = {item.code for item in report["incidents"]}
    assert report["market_state"] == "CLOSED"
    assert "BROKER_TICK_IN_FUTURE" in codes
    assert report["status"] == "CRITICAL"
'''

DOC = r'''# Phase 19 — Market Session-Aware Liveness & Clock Safety

Phase 19 prevents normal FX weekend closure from being misclassified as a broken market-data feed while preserving fail-closed behavior when the market is expected to be open.

## Weekly session policy

The guarded-live runtime now has a configurable UTC weekly session:

- Sunday open, default `22:00` UTC
- Friday close, default `22:00` UTC
- transition grace, default 3600 seconds

The transition grace is symmetric around the configured weekly open/close boundary. This intentionally blocks new entries near the boundary, which both absorbs common one-hour DST/broker-session shifts and reduces weekend-gap execution risk.

Possible market states are:

- `OPEN` — freshness checks are enforced and new entries may be evaluated.
- `CLOSED` — stale tick/bar incidents are suppressed and no new order is submitted.
- `TRANSITION` — stale tick/bar incidents are suppressed and no new order is submitted.

Position-integrity, terminal-health and pending-intent checks continue to run in every state.

## Clock/timestamp safety

MT5 Python does not expose a reliable independent server-clock primitive for this runtime. Phase 19 therefore uses broker tick timestamps and the completed-bar timestamp as broker-data clock evidence.

A tick timestamp ahead of host UTC by more than `max_future_tick_seconds` raises `BROKER_TICK_IN_FUTURE` as CRITICAL. A completed-bar timestamp ahead of host UTC by more than `max_future_bar_seconds` raises `COMPLETED_BAR_IN_FUTURE` as CRITICAL. These checks remain active even when the weekly market is closed.

A host clock that is too far ahead of a valid broker feed still manifests as stale market data while the market is OPEN; Phase 19 does not guess whether that condition is local-clock drift or feed latency.

## Configuration

```yaml
live:
  market_session_enabled: true
  market_sunday_open_utc: "22:00"
  market_friday_close_utc: "22:00"
  market_transition_grace_seconds: 3600
  max_future_tick_seconds: 5
  max_future_bar_seconds: 300
```

Broker session calendars differ. Adjust the weekly UTC boundary only after checking the actual symbol session at the broker. The transition window is a safety buffer, not a guarantee that a venue follows the default schedule.

## Operational behavior

`live-health` and the runtime heartbeat now expose market state, reason, next weekly transition, and signed broker-data clock offsets. A positive clock offset means the broker timestamp is ahead of host UTC.

Phase 19 does not add automatic time synchronization, order resend, or position repair. Fix the OS clock or broker/session configuration explicitly if a clock-integrity incident occurs.
'''


def apply() -> None:
    (ROOT / "src/market_clock.py").write_text(MARKET_CLOCK, encoding="utf-8")
    (ROOT / "tests/test_phase19_market_clock.py").write_text(TESTS, encoding="utf-8")
    (ROOT / "docs/phase19-market-clock.md").write_text(DOC, encoding="utf-8")

    # ops.py: integrate market-session state and clock/timestamp integrity.
    replace_once(
        "src/ops.py",
        'from .mt5_broker import BrokerDeal, BrokerPosition, BrokerTick\n',
        'from .market_clock import WeeklyFxSessionPolicy, evaluate_market_clock\nfrom .mt5_broker import BrokerDeal, BrokerPosition, BrokerTick\n',
    )
    ops = ROOT / "src/ops.py"
    text = ops.read_text(encoding="utf-8")
    start = text.index("def market_freshness_incidents(")
    end = text.index("\n\ndef position_integrity_incidents(", start)
    freshness = r'''def market_freshness_incidents(
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
'''
    text = text[:start] + freshness + text[end:]
    start = text.index("def operational_report(")
    end = text.index("\n\ndef recent_managed_deals(", start)
    operational = r'''def operational_report(
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
'''
    text = text[:start] + operational + text[end:]
    start = text.index("def heartbeat_payload(")
    heartbeat = r'''def heartbeat_payload(
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
'''
    text = text[:start] + heartbeat
    ops.write_text(text, encoding="utf-8")

    # config.py
    replace_once(
        "src/config.py",
        '    # 0 = derive automatically from the configured timeframe in the CLI.\n    max_bar_age_seconds: float = 0.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
        '    # 0 = derive automatically from the configured timeframe in the CLI.\n    max_bar_age_seconds: float = 0.0\n    market_session_enabled: bool = True\n    market_sunday_open_utc: str = "22:00"\n    market_friday_close_utc: str = "22:00"\n    market_transition_grace_seconds: float = 3600.0\n    max_future_tick_seconds: float = 5.0\n    max_future_bar_seconds: float = 300.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
    )
    replace_once(
        "src/config.py",
        'def _live(data: dict[str, Any]) -> LiveConfig:\n',
        'def _valid_hhmm(value: str) -> bool:\n    parts = str(value).split(":")\n    if len(parts) != 2 or not all(part.isdigit() for part in parts):\n        return False\n    hour, minute = (int(part) for part in parts)\n    return 0 <= hour <= 23 and 0 <= minute <= 59 and len(parts[0]) == 2 and len(parts[1]) == 2\n\n\ndef _live(data: dict[str, Any]) -> LiveConfig:\n',
    )
    replace_once(
        "src/config.py",
        '    if cfg.max_bar_age_seconds < 0:\n        raise ValueError("live.max_bar_age_seconds cannot be negative")\n    if cfg.deal_reconcile_lookback_hours <= 0:\n',
        '    if cfg.max_bar_age_seconds < 0:\n        raise ValueError("live.max_bar_age_seconds cannot be negative")\n    if not _valid_hhmm(cfg.market_sunday_open_utc):\n        raise ValueError("live.market_sunday_open_utc must be HH:MM UTC")\n    if not _valid_hhmm(cfg.market_friday_close_utc):\n        raise ValueError("live.market_friday_close_utc must be HH:MM UTC")\n    if cfg.market_transition_grace_seconds < 0:\n        raise ValueError("live.market_transition_grace_seconds cannot be negative")\n    if cfg.max_future_tick_seconds < 0 or cfg.max_future_bar_seconds < 0:\n        raise ValueError("live future timestamp tolerances cannot be negative")\n    if cfg.deal_reconcile_lookback_hours <= 0:\n',
    )

    # live.py
    replace_once(
        "src/live.py",
        '    max_tick_age_seconds: float = 30.0\n    max_bar_age_seconds: float = 7200.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
        '    max_tick_age_seconds: float = 30.0\n    max_bar_age_seconds: float = 7200.0\n    market_session_enabled: bool = True\n    market_sunday_open_utc: str = "22:00"\n    market_friday_close_utc: str = "22:00"\n    market_transition_grace_seconds: float = 3600.0\n    max_future_tick_seconds: float = 5.0\n    max_future_bar_seconds: float = 300.0\n    deal_reconcile_lookback_hours: float = 72.0\n',
    )
    replace_once(
        "src/live.py",
        '            max_tick_age_seconds=self.config.max_tick_age_seconds,\n            max_bar_age_seconds=self.config.max_bar_age_seconds,\n        )\n',
        '            max_tick_age_seconds=self.config.max_tick_age_seconds,\n            max_bar_age_seconds=self.config.max_bar_age_seconds,\n            market_session_enabled=self.config.market_session_enabled,\n            market_sunday_open_utc=self.config.market_sunday_open_utc,\n            market_friday_close_utc=self.config.market_friday_close_utc,\n            market_transition_grace_seconds=self.config.market_transition_grace_seconds,\n            max_future_tick_seconds=self.config.max_future_tick_seconds,\n            max_future_bar_seconds=self.config.max_future_bar_seconds,\n        )\n',
    )
    replace_once(
        "src/live.py",
        '                bar_age_seconds_value=report["bar_age_seconds"],\n                last_deal_time_msc=self.state.last_deal_time_msc,\n',
        '                bar_age_seconds_value=report["bar_age_seconds"],\n                market_state=report.get("market_state"),\n                market_reason=report.get("market_reason"),\n                next_market_transition_utc=report.get("next_market_transition_utc"),\n                staleness_suppressed=bool(report.get("staleness_suppressed", False)),\n                tick_clock_offset_seconds_value=report.get("tick_clock_offset_seconds"),\n                bar_clock_offset_seconds_value=report.get("bar_clock_offset_seconds"),\n                last_deal_time_msc=self.state.last_deal_time_msc,\n',
    )
    replace_once(
        "src/live.py",
        '        if self.state.halted:\n            self.store.save(self.state)\n            self._write_heartbeat(report, status="HALTED")\n            return self.snapshot()\n\n        warnings = [item for item in report["incidents"] if item.severity == "WARN"]\n',
        '        if self.state.halted:\n            self.store.save(self.state)\n            self._write_heartbeat(report, status="HALTED")\n            return self.snapshot()\n\n        if report.get("market_state", "OPEN") != "OPEN":\n            # Weekend / configured boundary windows are not feed failures, but\n            # they are also not permitted entry windows. Keep last_bar_time\n            # unchanged so a fresh completed bar is required after reopening.\n            self.store.save(self.state)\n            self._write_heartbeat(report, status="OK")\n            return self.snapshot()\n\n        warnings = [item for item in report["incidents"] if item.severity == "WARN"]\n',
    )

    # main.py
    replace_once(
        "src/main.py",
        '        max_tick_age_seconds=cfg.live.max_tick_age_seconds,\n        max_bar_age_seconds=max_bar_age,\n        deal_reconcile_lookback_hours=cfg.live.deal_reconcile_lookback_hours,\n',
        '        max_tick_age_seconds=cfg.live.max_tick_age_seconds,\n        max_bar_age_seconds=max_bar_age,\n        market_session_enabled=cfg.live.market_session_enabled,\n        market_sunday_open_utc=cfg.live.market_sunday_open_utc,\n        market_friday_close_utc=cfg.live.market_friday_close_utc,\n        market_transition_grace_seconds=cfg.live.market_transition_grace_seconds,\n        max_future_tick_seconds=cfg.live.max_future_tick_seconds,\n        max_future_bar_seconds=cfg.live.max_future_bar_seconds,\n        deal_reconcile_lookback_hours=cfg.live.deal_reconcile_lookback_hours,\n',
    )
    replace_once(
        "src/main.py",
        '    print(f"Completed-bar age      : {report[\'bar_age_seconds\']:.1f}s")\n    print(f"Managed positions      : {len(report[\'positions\'])}")\n',
        '    print(f"Completed-bar age      : {report[\'bar_age_seconds\']:.1f}s")\n    print(f"Market state           : {report.get(\'market_state\', \'UNKNOWN\')}")\n    print(f"Market reason          : {report.get(\'market_reason\', \'\')}")\n    print(f"Next transition UTC    : {report.get(\'next_market_transition_utc\')}")\n    print(f"Tick clock offset      : {report.get(\'tick_clock_offset_seconds\')}s")\n    print(f"Bar clock offset       : {report.get(\'bar_clock_offset_seconds\')}s")\n    print(f"Managed positions      : {len(report[\'positions\'])}")\n',
    )
    replace_once(
        "src/main.py",
        '                max_tick_age_seconds=live_cfg.max_tick_age_seconds,\n                max_bar_age_seconds=live_cfg.max_bar_age_seconds,\n            )\n',
        '                max_tick_age_seconds=live_cfg.max_tick_age_seconds,\n                max_bar_age_seconds=live_cfg.max_bar_age_seconds,\n                market_session_enabled=live_cfg.market_session_enabled,\n                market_sunday_open_utc=live_cfg.market_sunday_open_utc,\n                market_friday_close_utc=live_cfg.market_friday_close_utc,\n                market_transition_grace_seconds=live_cfg.market_transition_grace_seconds,\n                max_future_tick_seconds=live_cfg.max_future_tick_seconds,\n                max_future_bar_seconds=live_cfg.max_future_bar_seconds,\n            )\n',
    )

    # config.example.yaml
    replace_once(
        "config.example.yaml",
        '  # 0 = auto derive as 2.5 x configured timeframe duration.\n  max_bar_age_seconds: 0\n  deal_reconcile_lookback_hours: 72\n',
        '  # 0 = auto derive as 2.5 x configured timeframe duration.\n  max_bar_age_seconds: 0\n\n  # Phase 19 FX weekly-session and timestamp safety. The one-hour transition\n  # window absorbs common DST/broker boundary shifts and blocks new entries.\n  market_session_enabled: true\n  market_sunday_open_utc: "22:00"\n  market_friday_close_utc: "22:00"\n  market_transition_grace_seconds: 3600\n  max_future_tick_seconds: 5\n  max_future_bar_seconds: 300\n\n  deal_reconcile_lookback_hours: 72\n',
    )

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    marker = "## Phase 19 market-session and clock safety"
    if marker not in text:
        text += r'''

## Phase 19 market-session and clock safety

Guarded live now distinguishes `OPEN`, `CLOSED`, and `TRANSITION` FX weekly-session states. Weekend/boundary closure suppresses stale tick/bar alerts but blocks new order creation; position integrity, pending-intent reconciliation, terminal health, and future-timestamp checks continue to run. `live-health` and heartbeat output include the market state, next transition, and broker-data clock offsets.

See `docs/phase19-market-clock.md` before changing the broker-specific UTC session boundaries.
'''
        readme.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    apply()
