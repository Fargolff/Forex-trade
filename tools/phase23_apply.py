from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"Phase 23 marker missing in {path}: {old[:160]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply() -> None:
    Path("src/financing.py").write_text(
        '''from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FinancingSchedule:
    """Synthetic research/paper financing assumptions in account-currency cash.

    long/short values are cash per lot for a 1x rollover. Positive values are
    credits and negative values are costs. weekday_multipliers use Python's
    Monday=0 .. Sunday=6 convention; the default models a Wednesday triple swap.
    """

    enabled: bool = False
    long_cash_per_lot_rollover: float = 0.0
    short_cash_per_lot_rollover: float = 0.0
    rollover_hour_utc: int = 22
    weekday_multipliers: tuple[float, ...] = (1.0, 1.0, 3.0, 1.0, 1.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        multipliers = tuple(float(value) for value in self.weekday_multipliers)
        object.__setattr__(self, "weekday_multipliers", multipliers)
        if not 0 <= int(self.rollover_hour_utc) <= 23:
            raise ValueError("financing.rollover_hour_utc must be in [0,23]")
        if len(multipliers) != 7:
            raise ValueError("financing.weekday_multipliers must contain exactly 7 values")
        if any((not math.isfinite(value)) or value < 0 for value in multipliers):
            raise ValueError("financing.weekday_multipliers must be finite and non-negative")
        for value in (self.long_cash_per_lot_rollover, self.short_cash_per_lot_rollover):
            if not math.isfinite(float(value)):
                raise ValueError("financing cash assumptions must be finite")


@dataclass(frozen=True)
class FinancingAccrual:
    cash: float = 0.0
    rollovers: int = 0
    weighted_rollovers: float = 0.0


def _as_utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def financing_between(
    start: Any,
    end: Any,
    *,
    side: int,
    lots: float,
    schedule: FinancingSchedule,
) -> FinancingAccrual:
    """Return synthetic financing for rollovers in the half-open holding interval.

    A rollover is charged when ``start < rollover_timestamp <= end``. This avoids
    charging a position immediately when it is opened exactly at the configured
    rollover timestamp while still charging positions held across that boundary.
    """
    if side not in (-1, 1):
        raise ValueError("side must be -1 or 1")
    lots = float(lots)
    if lots < 0 or not math.isfinite(lots):
        raise ValueError("lots must be finite and non-negative")
    if not schedule.enabled or lots == 0:
        return FinancingAccrual()

    start_ts = _as_utc(start)
    end_ts = _as_utc(end)
    if end_ts <= start_ts:
        return FinancingAccrual()

    rate = float(
        schedule.long_cash_per_lot_rollover
        if side > 0
        else schedule.short_cash_per_lot_rollover
    )
    cash = 0.0
    rollovers = 0
    weighted = 0.0
    day = start_ts.date()
    end_day = end_ts.date()
    while day <= end_day:
        rollover = pd.Timestamp(
            year=day.year,
            month=day.month,
            day=day.day,
            hour=int(schedule.rollover_hour_utc),
            tz="UTC",
        )
        if start_ts < rollover <= end_ts:
            multiplier = float(schedule.weekday_multipliers[rollover.weekday()])
            if multiplier > 0:
                rollovers += 1
                weighted += multiplier
                cash += rate * lots * multiplier
        day += timedelta(days=1)

    return FinancingAccrual(
        cash=float(cash),
        rollovers=rollovers,
        weighted_rollovers=float(weighted),
    )
''',
        encoding="utf-8",
    )

    replace_once(
        "src/backtest.py",
        "from dataclasses import dataclass\n",
        "from dataclasses import dataclass, field\n",
    )
    replace_once(
        "src/backtest.py",
        "from .risk import RiskLimits, kill_switch_triggered, position_size_lots\n",
        "from .financing import FinancingSchedule, financing_between\nfrom .risk import RiskLimits, kill_switch_triggered, position_size_lots\n",
    )
    replace_once(
        "src/backtest.py",
        "    commission_per_lot_round_turn: float = 7.0\n    periods_per_year: float = 252.0 * 24.0\n",
        "    commission_per_lot_round_turn: float = 7.0\n    financing: FinancingSchedule = field(default_factory=FinancingSchedule)\n    periods_per_year: float = 252.0 * 24.0\n",
    )
    replace_once(
        "src/backtest.py",
        "    pnl: float\n    reason: str\n",
        "    pnl: float\n    reason: str\n    financing: float = 0.0\n",
    )
    replace_once(
        "src/backtest.py",
        "        pnl=float(pnl),\n        reason=reason,\n    )\n\n\ndef run_backtest",
        "        pnl=float(pnl),\n        reason=reason,\n        financing=float(position.get(\"financing_accrued\", 0.0)),\n    )\n\n\ndef _apply_position_financing(position: dict, ts: pd.Timestamp, cfg: BacktestConfig) -> float:\n    start = position.get(\"last_financing_time\", position[\"entry_time\"])\n    accrual = financing_between(\n        start,\n        ts,\n        side=int(position[\"side\"]),\n        lots=float(position[\"lots\"]),\n        schedule=cfg.financing,\n    )\n    position[\"last_financing_time\"] = ts\n    position[\"financing_accrued\"] = float(position.get(\"financing_accrued\", 0.0)) + accrual.cash\n    return float(accrual.cash)\n\n\ndef run_backtest",
    )
    replace_once(
        "src/backtest.py",
        "    trades: list[Trade] = []\n    equity_curve: list[tuple[pd.Timestamp, float]] = []\n",
        "    trades: list[Trade] = []\n    equity_curve: list[tuple[pd.Timestamp, float]] = []\n    total_financing = 0.0\n",
    )
    replace_once(
        "src/backtest.py",
        "        if day != current_day:\n            current_day = day\n            start_of_day_equity = equity\n\n        killed, kill_reason",
        "        if day != current_day:\n            current_day = day\n            start_of_day_equity = equity\n\n        if position is not None:\n            financing_cash = _apply_position_financing(position, ts, cfg)\n            equity += financing_cash\n            total_financing += financing_cash\n\n        killed, kill_reason",
    )
    replace_once(
        "src/backtest.py",
        "                        \"tp\": entry + pending_side * tp_distance,\n                    }\n",
        "                        \"tp\": entry + pending_side * tp_distance,\n                        \"last_financing_time\": ts,\n                        \"financing_accrued\": 0.0,\n                    }\n",
    )
    replace_once(
        "src/backtest.py",
        "        \"net_profit\": equity - cfg.initial_equity,\n",
        "        \"net_profit\": equity - cfg.initial_equity,\n        \"total_financing\": float(total_financing),\n        \"financing_enabled\": bool(cfg.financing.enabled),\n",
    )

    replace_once(
        "src/paper.py",
        "from dataclasses import asdict, dataclass, field\n",
        "from dataclasses import asdict, dataclass, field\n",
    )
    replace_once(
        "src/paper.py",
        "from .risk import RiskLimits, kill_switch_triggered, position_size_lots\n",
        "from .financing import FinancingSchedule, financing_between\nfrom .risk import RiskLimits, kill_switch_triggered, position_size_lots\n",
    )
    replace_once(
        "src/paper.py",
        "    commission_per_lot_round_turn: float = 7.0\n    state_path: str = \"runtime/paper_state.json\"\n",
        "    commission_per_lot_round_turn: float = 7.0\n    financing: FinancingSchedule = field(default_factory=FinancingSchedule)\n    state_path: str = \"runtime/paper_state.json\"\n",
    )
    replace_once(
        "src/paper.py",
        "    stop: float\n    take_profit: float\n",
        "    stop: float\n    take_profit: float\n    last_financing_time: str | None = None\n    financing_accrued: float = 0.0\n",
    )
    replace_once(
        "src/paper.py",
        "    halt_reason: str | None = None\n    positions: dict[str, PaperPosition] = field(default_factory=dict)\n",
        "    halt_reason: str | None = None\n    total_financing: float = 0.0\n    positions: dict[str, PaperPosition] = field(default_factory=dict)\n",
    )
    replace_once(
        "src/paper.py",
        "        raw = json.loads(self.path.read_text(encoding=\"utf-8\"))\n        positions = {\n            name: PaperPosition(**position)\n            for name, position in (raw.pop(\"positions\", {}) or {}).items()\n        }\n        return PaperState(positions=positions, **raw)\n",
        "        raw = json.loads(self.path.read_text(encoding=\"utf-8\"))\n        raw_positions = raw.pop(\"positions\", {}) or {}\n        legacy_anchor = raw.get(\"last_bar_time\")\n        positions: dict[str, PaperPosition] = {}\n        for name, position in raw_positions.items():\n            payload = dict(position)\n            payload.setdefault(\"last_financing_time\", legacy_anchor or payload.get(\"entry_time\"))\n            payload.setdefault(\"financing_accrued\", 0.0)\n            positions[name] = PaperPosition(**payload)\n        raw.setdefault(\"total_financing\", 0.0)\n        return PaperState(positions=positions, **raw)\n",
    )
    replace_once(
        "src/paper.py",
        "            stop=fill - side * stop_distance,\n            take_profit=fill + side * take_profit_distance,\n        )\n",
        "            stop=fill - side * stop_distance,\n            take_profit=fill + side * take_profit_distance,\n            last_financing_time=ts.isoformat(),\n            financing_accrued=0.0,\n        )\n",
    )
    replace_once(
        "src/paper.py",
        "    def _apply_risk_gate(self, ts: pd.Timestamp, close: float) -> None:\n        self._mark_to_market(close)\n        killed, reason = kill_switch_triggered(\n",
        "    def _apply_financing(self, ts: pd.Timestamp) -> float:\n        total = 0.0\n        for strategy, position in self.state.positions.items():\n            start = position.last_financing_time or position.entry_time\n            accrual = financing_between(\n                start,\n                ts,\n                side=position.side,\n                lots=position.lots,\n                schedule=self.config.financing,\n            )\n            position.last_financing_time = ts.isoformat()\n            if accrual.cash == 0.0:\n                continue\n            position.financing_accrued += accrual.cash\n            self.state.balance += accrual.cash\n            self.state.total_financing += accrual.cash\n            total += accrual.cash\n            self.events.append(\n                time=ts.isoformat(),\n                event=\"FINANCING\",\n                strategy=strategy,\n                side=position.side,\n                lots=position.lots,\n                expected_price=\"\",\n                fill_price=\"\",\n                slippage_pips=\"\",\n                pnl=accrual.cash,\n                balance=self.state.balance,\n                equity=self.state.equity,\n                reason=(\n                    f\"rollovers={accrual.rollovers};\"\n                    f\"weighted_rollovers={accrual.weighted_rollovers:g};\"\n                    \"synthetic_account_cash\"\n                ),\n            )\n        return float(total)\n\n    def _apply_risk_gate(self, ts: pd.Timestamp, close: float) -> None:\n        self._mark_to_market(close)\n        if self.state.halted:\n            return\n        killed, reason = kill_switch_triggered(\n",
    )
    replace_once(
        "src/paper.py",
        "            if not self.state.halted:\n                self._execute_pending_at_open(ts, float(row[\"open\"]))\n",
        "            self._apply_financing(ts)\n            self._apply_risk_gate(ts, float(row[\"open\"]))\n\n            if not self.state.halted:\n                self._execute_pending_at_open(ts, float(row[\"open\"]))\n",
    )
    replace_once(
        "src/paper.py",
        "            \"peak_equity\": self.state.peak_equity,\n            \"halted\": self.state.halted,\n",
        "            \"peak_equity\": self.state.peak_equity,\n            \"total_financing\": self.state.total_financing,\n            \"financing_enabled\": bool(self.config.financing.enabled),\n            \"halted\": self.state.halted,\n",
    )

    replace_once(
        "src/config.py",
        "import yaml\n\n\n@dataclass(frozen=True)\nclass StrategyConfig:",
        "import yaml\n\nfrom .financing import FinancingSchedule\n\n\n@dataclass(frozen=True)\nclass StrategyConfig:",
    )
    replace_once(
        "src/config.py",
        "    commission_per_lot_round_turn: float = 7.0\n    strategy: StrategyConfig = field(default_factory=StrategyConfig)\n",
        "    commission_per_lot_round_turn: float = 7.0\n    financing: FinancingSchedule = field(default_factory=FinancingSchedule)\n    strategy: StrategyConfig = field(default_factory=StrategyConfig)\n",
    )
    replace_once(
        "src/config.py",
        "def _valid_hhmm(value: str) -> bool:\n",
        "def _financing(data: dict[str, Any]) -> FinancingSchedule:\n    raw = dict(data or {})\n    if \"weekday_multipliers\" in raw:\n        values = raw.get(\"weekday_multipliers\")\n        if not isinstance(values, (list, tuple)):\n            raise ValueError(\"financing.weekday_multipliers must be a list of 7 values\")\n        raw[\"weekday_multipliers\"] = tuple(float(value) for value in values)\n    return FinancingSchedule(**raw)\n\n\ndef _valid_hhmm(value: str) -> bool:\n",
    )
    replace_once(
        "src/config.py",
        "    strategy = _strategy(raw.pop(\"strategy\", {}))\n    paper = _paper(raw.pop(\"paper\", {}))\n    live = _live(raw.pop(\"live\", {}))\n    return AppConfig(strategy=strategy, paper=paper, live=live, **raw)\n",
        "    strategy = _strategy(raw.pop(\"strategy\", {}))\n    financing = _financing(raw.pop(\"financing\", {}))\n    paper = _paper(raw.pop(\"paper\", {}))\n    live = _live(raw.pop(\"live\", {}))\n    return AppConfig(strategy=strategy, financing=financing, paper=paper, live=live, **raw)\n",
    )

    replace_once(
        "src/main.py",
        "        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,\n        periods_per_year=_periods_per_year(cfg.timeframe),\n",
        "        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,\n        financing=cfg.financing,\n        periods_per_year=_periods_per_year(cfg.timeframe),\n",
    )
    replace_once(
        "src/main.py",
        "        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,\n        state_path=args.paper_state or cfg.paper.state_path,\n",
        "        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,\n        financing=cfg.financing,\n        state_path=args.paper_state or cfg.paper.state_path,\n",
    )
    replace_once(
        "src/main.py",
        "    print(f\"Account equity         : {report['account'].equity:.2f} {report['account'].currency}\")\n",
        "    print(f\"Account equity         : {report['account'].equity:.2f} {report['account'].currency}\")\n    swap = report.get(\"broker_swap_terms\") or {}\n    if swap:\n        print(\n            \"Broker swap raw        : \"\n            f\"long={swap.get('long')} short={swap.get('short')} \"\n            f\"mode={swap.get('mode')} rollover3days={swap.get('rollover3days')}\"\n        )\n",
    )

    replace_once(
        "src/mt5_broker.py",
        "    trade_freeze_level: int = 0\n",
        "    trade_freeze_level: int = 0\n    swap_long: float = 0.0\n    swap_short: float = 0.0\n    swap_mode: int = 0\n    swap_rollover3days: int = -1\n",
    )
    replace_once(
        "src/mt5_broker.py",
        "            trade_freeze_level=int(getattr(info, \"trade_freeze_level\", 0) or 0),\n        )\n",
        "            trade_freeze_level=int(getattr(info, \"trade_freeze_level\", 0) or 0),\n            swap_long=float(getattr(info, \"swap_long\", 0.0) or 0.0),\n            swap_short=float(getattr(info, \"swap_short\", 0.0) or 0.0),\n            swap_mode=int(getattr(info, \"swap_mode\", 0) or 0),\n            swap_rollover3days=int(getattr(info, \"swap_rollover3days\", -1)),\n        )\n",
    )

    replace_once(
        "src/ops.py",
        "def market_freshness_incidents(\n",
        "def broker_swap_terms(spec: Any) -> dict[str, float | int]:\n    return {\n        \"long\": float(getattr(spec, \"swap_long\", 0.0) or 0.0),\n        \"short\": float(getattr(spec, \"swap_short\", 0.0) or 0.0),\n        \"mode\": int(getattr(spec, \"swap_mode\", 0) or 0),\n        \"rollover3days\": int(getattr(spec, \"swap_rollover3days\", -1)),\n    }\n\n\ndef market_freshness_incidents(\n",
    )
    replace_once(
        "src/ops.py",
        "        \"symbol_spec\": spec,\n        \"positions\": positions,\n",
        "        \"symbol_spec\": spec,\n        \"broker_swap_terms\": broker_swap_terms(spec),\n        \"positions\": positions,\n",
    )

    replace_once(
        "config.example.yaml",
        "commission_per_lot_round_turn: 7.0\n\nstrategy:\n",
        "commission_per_lot_round_turn: 7.0\n\n# Phase 23 synthetic financing assumptions for backtest and paper only.\n# Values are account-currency cash per lot for a 1x rollover. Positive = credit,\n# negative = cost. Do not copy MT5 swap_long/swap_short blindly unless you have\n# confirmed the broker's swap_mode/unit conversion. Default remains disabled.\nfinancing:\n  enabled: false\n  long_cash_per_lot_rollover: 0.0\n  short_cash_per_lot_rollover: 0.0\n  rollover_hour_utc: 22\n  # Monday..Sunday. Default models Wednesday triple swap and no weekend event.\n  weekday_multipliers: [1, 1, 3, 1, 1, 0, 0]\n\nstrategy:\n",
    )

    Path("tests/test_phase23_financing.py").write_text(
        '''from pathlib import Path

import pandas as pd

from src.backtest import BacktestConfig, run_backtest
from src.financing import FinancingSchedule, financing_between
from src.mt5_broker import SymbolSpec
from src.ops import broker_swap_terms
from src.paper import PaperConfig, PaperPosition, PaperTradingEngine


def test_financing_schedule_applies_weekday_and_triple_rollovers():
    schedule = FinancingSchedule(
        enabled=True,
        long_cash_per_lot_rollover=-2.0,
        short_cash_per_lot_rollover=1.0,
        rollover_hour_utc=22,
        weekday_multipliers=(1, 1, 3, 1, 1, 0, 0),
    )
    result = financing_between(
        pd.Timestamp("2026-09-15T21:00:00Z"),
        pd.Timestamp("2026-09-17T23:00:00Z"),
        side=1,
        lots=1.5,
        schedule=schedule,
    )
    assert result.rollovers == 3
    assert result.weighted_rollovers == 5.0
    assert result.cash == -15.0


def test_backtest_financing_is_included_in_equity_and_trade_audit():
    idx = pd.date_range("2026-09-15T20:00:00Z", "2026-09-17T23:00:00Z", freq="h")
    frame = pd.DataFrame(
        {
            "open": 1.10,
            "high": 1.1001,
            "low": 1.0999,
            "close": 1.10,
            "signal": 0,
            "stop_distance": 0.01,
            "take_profit_distance": 0.02,
        },
        index=idx,
    )
    frame.loc[idx[0], "signal"] = 1
    cfg = BacktestConfig(
        initial_equity=10_000.0,
        risk_per_trade=0.01,
        max_daily_loss_pct=0.50,
        max_drawdown_pct=0.50,
        spread_pips=0.0,
        slippage_pips=0.0,
        commission_per_lot_round_turn=0.0,
        financing=FinancingSchedule(
            enabled=True,
            long_cash_per_lot_rollover=-2.0,
            short_cash_per_lot_rollover=1.0,
            rollover_hour_utc=22,
            weekday_multipliers=(1, 1, 3, 1, 1, 0, 0),
        ),
    )
    result = run_backtest(frame, cfg)
    assert abs(result["total_financing"] - (-1.0)) < 1e-9
    assert abs(result["final_equity"] - 9999.0) < 1e-9
    assert len(result["trade_log"]) == 1
    assert abs(result["trade_log"][0].financing - (-1.0)) < 1e-9


def test_paper_financing_is_forward_only_and_idempotent(tmp_path, monkeypatch):
    schedule = FinancingSchedule(
        enabled=True,
        long_cash_per_lot_rollover=-2.0,
        short_cash_per_lot_rollover=1.0,
        rollover_hour_utc=22,
        weekday_multipliers=(1, 1, 3, 1, 1, 0, 0),
    )
    cfg = PaperConfig(
        initial_equity=10_000.0,
        risk_per_trade=0.01,
        max_daily_loss_pct=0.50,
        max_drawdown_pct=0.50,
        spread_pips=0.0,
        slippage_pips=0.0,
        commission_per_lot_round_turn=0.0,
        financing=schedule,
        state_path=str(tmp_path / "state.json"),
        events_path=str(tmp_path / "events.csv"),
    )
    strategies = {"momentum": {"lookback": 2, "threshold": 999.0, "atr_period": 2, "stop_atr": 2.0, "take_profit_atr": 3.0}}
    engine = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    engine.state.positions["momentum"] = PaperPosition(
        strategy="momentum",
        side=1,
        lots=1.0,
        entry_time="2026-09-15T21:00:00+00:00",
        entry=1.10,
        stop=1.0,
        take_profit=1.2,
        last_financing_time="2026-09-15T21:00:00+00:00",
    )
    engine.state.last_bar_time = "2026-09-15T21:00:00+00:00"
    engine.store.save(engine.state)

    def flat_signals(frame, _name, _params):
        out = pd.DataFrame(index=frame.index)
        out["signal"] = 0
        out["stop_distance"] = 0.01
        out["take_profit_distance"] = 0.02
        return out

    monkeypatch.setattr("src.paper.build_signals", flat_signals)
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-16T23:00:00Z")])
    bars = pd.DataFrame({"open": [1.10], "high": [1.1001], "low": [1.0999], "close": [1.10]}, index=idx)
    first = engine.process(bars)
    assert first["total_financing"] == -8.0
    assert first["balance"] == 9992.0
    events = pd.read_csv(cfg.events_path)
    financing_events = events[events["event"] == "FINANCING"]
    assert len(financing_events) == 1
    assert float(financing_events.iloc[0]["pnl"]) == -8.0

    restarted = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    second = restarted.process(bars)
    assert second["total_financing"] == -8.0
    assert second["balance"] == 9992.0
    events = pd.read_csv(cfg.events_path)
    assert (events["event"] == "FINANCING").sum() == 1


def test_legacy_paper_position_anchors_financing_at_last_processed_bar(tmp_path):
    state = tmp_path / "legacy.json"
    state.write_text(
        ''' + "'''" + '''{
  "version": 1,
  "balance": 10000.0,
  "equity": 10000.0,
  "peak_equity": 10000.0,
  "start_of_day_equity": 10000.0,
  "current_day": "2026-09-16",
  "last_bar_time": "2026-09-16T20:00:00+00:00",
  "halted": false,
  "halt_reason": null,
  "positions": {
    "momentum": {
      "strategy": "momentum",
      "side": 1,
      "lots": 1.0,
      "entry_time": "2026-09-10T10:00:00+00:00",
      "entry": 1.1,
      "stop": 1.0,
      "take_profit": 1.2
    }
  },
  "pending_signals": {}
}''' + "'''" + ''',
        encoding="utf-8",
    )
    cfg = PaperConfig(state_path=str(state), events_path=str(tmp_path / "events.csv"))
    strategies = {"momentum": {"lookback": 2, "threshold": 999.0, "atr_period": 2, "stop_atr": 2.0, "take_profit_atr": 3.0}}
    engine = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    assert engine.state.positions["momentum"].last_financing_time == "2026-09-16T20:00:00+00:00"
    assert engine.state.total_financing == 0.0


def test_broker_swap_terms_remain_raw_and_do_not_imply_cash_conversion():
    spec = SymbolSpec(
        symbol="EURUSD",
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
        swap_long=-6.5,
        swap_short=2.1,
        swap_mode=1,
        swap_rollover3days=3,
    )
    assert broker_swap_terms(spec) == {
        "long": -6.5,
        "short": 2.1,
        "mode": 1,
        "rollover3days": 3,
    }
''',
        encoding="utf-8",
    )

    Path("docs/phase23-financing.md").write_text(
        '''# Phase 23 — Broker Swap / Financing Cost Realism

Phase 23 adds explicit overnight-financing realism to research and paper trading while avoiding synthetic double charging in live trading.

## Research / paper model

`financing:` in `config.yaml` defines synthetic **account-currency cash per lot per 1x rollover**. Positive values are credits; negative values are costs. The default is disabled, so existing results do not change unless the operator opts in.

The schedule uses a UTC rollover hour plus seven Monday-through-Sunday multipliers. The default `[1, 1, 3, 1, 1, 0, 0]` models a Wednesday triple-swap convention. A charge occurs only when a position was already open across the boundary: `start < rollover <= end`.

Backtests include financing in account equity/net profit and expose `total_financing`. Each trade also records the financing accrued while it was open. Paper mode books financing to persistent balance, emits a `FINANCING` audit event, persists cumulative financing, and remains idempotent across restarts.

## Upgrade behavior for existing paper state

A pre-Phase-23 paper position has no financing cursor. On load, Phase 23 anchors that position to the state's existing `last_bar_time`, not its historical entry time. This deliberately avoids silently retro-charging many days of swap when upgrading an old paper experiment. Financing starts forward from the last already-processed bar.

## Live behavior: observe, do not synthesize

Live accounts already receive broker-booked swap/financing. Phase 23 therefore does **not** deduct a synthetic charge from live equity. `MT5Broker.symbol_spec()` captures raw `swap_long`, `swap_short`, `swap_mode`, and `swap_rollover3days`, and live-health exposes those raw terms for comparison/audit.

Do not copy `swap_long` or `swap_short` directly into the research cash assumptions unless you have confirmed the broker's `swap_mode`, instrument contract, account currency, and conversion rules. MT5 supports multiple swap calculation modes and the raw number is not universally account-currency cash per lot.

## Limitations

- Financing assumptions are static over the test period; broker rates can change historically.
- Holiday and exceptional rollover calendars are not yet modeled.
- Live reconciliation continues to use broker deal-history `swap` as the realized source of truth.
- This improves cost realism but does not guarantee research/live equivalence or profitability.
''',
        encoding="utf-8",
    )

    readme = Path("README.md")
    r = readme.read_text(encoding="utf-8")
    if "### Phase 23 — Broker Swap / Financing Cost Realism ✅" not in r:
        marker = "### Phase 7 — Guarded Small Live Deployment ✅\n"
        section = '''### Phase 23 — Broker Swap / Financing Cost Realism ✅

- Optional account-currency cash-per-lot rollover assumptions for backtest and paper
- Separate long/short financing rates
- UTC rollover hour plus configurable Monday-Sunday multipliers
- Default Wednesday triple-swap schedule
- Backtest `total_financing` and per-trade financing audit
- Persistent/idempotent Paper `FINANCING` events and cumulative financing
- Legacy paper states start financing forward from the last processed bar; no silent retro-charge
- Live does not synthesize financing or double-charge broker-booked swap
- Live health exposes raw MT5 swap terms (`swap_long`, `swap_short`, `swap_mode`, `swap_rollover3days`)

See `docs/phase23-financing.md` before calibrating the assumptions to a broker.

'''
        if marker not in r:
            raise RuntimeError("README Phase 7 marker missing")
        r = r.replace(marker, section + marker, 1)
    r = r.replace(
        "- Broker-specific swap/financing forecasting and reconciliation policy\n",
        "- Historical broker swap-rate ingestion, holiday exceptions and broker-specific forecast calibration\n",
    )
    readme.write_text(r, encoding="utf-8")


if __name__ == "__main__":
    apply()
