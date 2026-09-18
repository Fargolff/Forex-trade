from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .risk import RiskLimits, kill_switch_triggered, position_size_lots
from .strategy import build_signals


@dataclass(frozen=True)
class PaperConfig:
    initial_equity: float = 10_000.0
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    spread_pips: float = 0.8
    slippage_pips: float = 0.2
    commission_per_lot_round_turn: float = 7.0
    state_path: str = "runtime/paper_state.json"
    events_path: str = "runtime/paper_events.csv"


@dataclass
class PaperPosition:
    strategy: str
    side: int
    lots: float
    entry_time: str
    entry: float
    stop: float
    take_profit: float


@dataclass
class PaperState:
    version: int = 1
    balance: float = 10_000.0
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    start_of_day_equity: float = 10_000.0
    current_day: str | None = None
    last_bar_time: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    pending_signals: dict[str, dict[str, float | int]] = field(default_factory=dict)


class PaperStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, initial_equity: float) -> PaperState:
        if not self.path.exists():
            return PaperState(
                balance=initial_equity,
                equity=initial_equity,
                peak_equity=initial_equity,
                start_of_day_equity=initial_equity,
            )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        positions = {
            name: PaperPosition(**position)
            for name, position in (raw.pop("positions", {}) or {}).items()
        }
        return PaperState(positions=positions, **raw)

    def save(self, state: PaperState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)


class PaperEventLog:
    columns = [
        "time",
        "event",
        "strategy",
        "side",
        "lots",
        "expected_price",
        "fill_price",
        "slippage_pips",
        "pnl",
        "balance",
        "equity",
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


def load_portfolio_bundle(
    weights_path: str | Path,
    candidates_path: str | Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    weights_file = Path(weights_path)
    candidates_file = Path(candidates_path)
    if not weights_file.exists():
        raise FileNotFoundError(f"portfolio weights not found: {weights_file}")
    if not candidates_file.exists():
        raise FileNotFoundError(f"portfolio candidates not found: {candidates_file}")

    weights_frame = pd.read_csv(weights_file)
    candidates_frame = pd.read_csv(candidates_file)
    if not {"strategy", "weight"}.issubset(weights_frame.columns):
        raise ValueError("portfolio weights file requires strategy and weight columns")
    if "strategy" not in candidates_frame.columns or "selected_params" not in candidates_frame.columns:
        raise ValueError("portfolio candidates file requires strategy and selected_params columns")

    active = weights_frame.loc[weights_frame["weight"] > 0, ["strategy", "weight"]].copy()
    if active.empty:
        raise ValueError("portfolio weights contain no active strategy")

    params_by_strategy: dict[str, dict[str, Any]] = {}
    candidate_rows = candidates_frame.drop_duplicates("strategy", keep="first").set_index("strategy")
    for strategy in active["strategy"]:
        if strategy not in candidate_rows.index:
            raise ValueError(f"missing candidate parameters for {strategy}")
        raw = candidate_rows.loc[strategy, "selected_params"]
        if pd.isna(raw) or not str(raw).strip():
            raise ValueError(f"selected_params is missing for {strategy}")
        parsed = json.loads(str(raw))
        if not isinstance(parsed, dict):
            raise ValueError(f"selected_params for {strategy} must decode to an object")
        params_by_strategy[str(strategy)] = parsed

    weights = {str(row.strategy): float(row.weight) for row in active.itertuples(index=False)}
    return params_by_strategy, weights


class PaperTradingEngine:
    """Stateful paper execution engine.

    Strategy signals are calculated at bar close and stored as pending orders.
    Pending orders execute at the next completed bar's open. The engine never
    calls an MT5 order API; MT5 can be used only as a source of completed bars.
    """

    def __init__(
        self,
        symbol: str,
        strategies: dict[str, dict[str, Any]],
        weights: dict[str, float] | None = None,
        config: PaperConfig | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("at least one paper strategy is required")
        self.symbol = symbol
        self.strategies = strategies
        self.config = config or PaperConfig()
        self.store = PaperStateStore(self.config.state_path)
        self.events = PaperEventLog(self.config.events_path)
        self.state = self.store.load(self.config.initial_equity)

        raw_weights = weights or {name: 1.0 for name in strategies}
        missing = set(strategies).difference(raw_weights)
        if missing:
            raise ValueError(f"missing paper weights for: {sorted(missing)}")
        total = sum(max(0.0, float(raw_weights[name])) for name in strategies)
        if total <= 0:
            raise ValueError("paper strategy weights must sum to a positive value")
        self.weights = {name: max(0.0, float(raw_weights[name])) / total for name in strategies}

        self.limits = RiskLimits(
            risk_per_trade=self.config.risk_per_trade,
            max_daily_loss_pct=self.config.max_daily_loss_pct,
            max_drawdown_pct=self.config.max_drawdown_pct,
        )

    def _entry_fill(self, raw_open: float, side: int) -> tuple[float, float]:
        half_spread = self.config.spread_pips * self.config.pip_size / 2.0
        expected = raw_open + side * half_spread
        fill = expected + side * self.config.slippage_pips * self.config.pip_size
        return expected, fill

    def _exit_fill(self, raw_price: float, side: int) -> tuple[float, float]:
        half_spread = self.config.spread_pips * self.config.pip_size / 2.0
        expected = raw_price - side * half_spread
        fill = expected - side * self.config.slippage_pips * self.config.pip_size
        return expected, fill

    def _position_pnl(self, position: PaperPosition, raw_price: float, include_commission: bool = False) -> float:
        _expected, exit_price = self._exit_fill(raw_price, position.side)
        price_delta = (exit_price - position.entry) * position.side
        pnl_pips = price_delta / self.config.pip_size
        pnl = pnl_pips * self.config.pip_value_per_lot * position.lots
        if include_commission:
            pnl -= self.config.commission_per_lot_round_turn * position.lots
        return float(pnl)

    def _mark_to_market(self, close: float) -> float:
        unrealized = sum(self._position_pnl(position, close) for position in self.state.positions.values())
        self.state.equity = float(self.state.balance + unrealized)
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
        return self.state.equity

    def _close_position(self, strategy: str, ts: pd.Timestamp, raw_price: float, reason: str) -> None:
        position = self.state.positions.pop(strategy)
        expected, fill = self._exit_fill(raw_price, position.side)
        price_delta = (fill - position.entry) * position.side
        pnl_pips = price_delta / self.config.pip_size
        gross = pnl_pips * self.config.pip_value_per_lot * position.lots
        commission = self.config.commission_per_lot_round_turn * position.lots
        pnl = float(gross - commission)
        self.state.balance += pnl
        self.state.equity = self.state.balance
        self.events.append(
            time=ts.isoformat(),
            event="EXIT",
            strategy=strategy,
            side=position.side,
            lots=position.lots,
            expected_price=expected,
            fill_price=fill,
            slippage_pips=abs(fill - expected) / self.config.pip_size,
            pnl=pnl,
            balance=self.state.balance,
            equity=self.state.equity,
            reason=reason,
        )

    def _open_position(
        self,
        strategy: str,
        ts: pd.Timestamp,
        raw_open: float,
        side: int,
        stop_distance: float,
        take_profit_distance: float,
    ) -> None:
        if self.state.halted or side not in (-1, 1) or stop_distance <= 0 or take_profit_distance <= 0:
            return
        weight = self.weights[strategy]
        if weight <= 0:
            return
        strategy_risk = self.config.risk_per_trade * weight
        lots = position_size_lots(
            equity=max(self.state.equity, 0.0),
            risk_per_trade=strategy_risk,
            stop_distance_price=stop_distance,
            pip_size=self.config.pip_size,
            pip_value_per_lot=self.config.pip_value_per_lot,
        )
        if lots <= 0:
            return
        expected, fill = self._entry_fill(raw_open, side)
        self.state.positions[strategy] = PaperPosition(
            strategy=strategy,
            side=side,
            lots=lots,
            entry_time=ts.isoformat(),
            entry=fill,
            stop=fill - side * stop_distance,
            take_profit=fill + side * take_profit_distance,
        )
        self.events.append(
            time=ts.isoformat(),
            event="ENTRY",
            strategy=strategy,
            side=side,
            lots=lots,
            expected_price=expected,
            fill_price=fill,
            slippage_pips=abs(fill - expected) / self.config.pip_size,
            pnl="",
            balance=self.state.balance,
            equity=self.state.equity,
            reason="next_bar_open",
        )

    def _execute_pending_at_open(self, ts: pd.Timestamp, raw_open: float) -> None:
        pending = dict(self.state.pending_signals)
        self.state.pending_signals.clear()
        for strategy, signal in pending.items():
            side = int(signal["side"])
            existing = self.state.positions.get(strategy)
            if existing is not None and existing.side == side:
                continue
            if existing is not None:
                self._close_position(strategy, ts, raw_open, "opposite_signal")
            self._open_position(
                strategy,
                ts,
                raw_open,
                side,
                float(signal["stop_distance"]),
                float(signal["take_profit_distance"]),
            )

    def _process_intrabar_exits(self, ts: pd.Timestamp, row: pd.Series) -> None:
        for strategy in list(self.state.positions):
            position = self.state.positions[strategy]
            raw_exit: float | None = None
            reason: str | None = None

            if position.side > 0:
                if float(row["open"]) <= position.stop:
                    raw_exit, reason = float(row["open"]), "gap_stop"
                elif float(row["open"]) >= position.take_profit:
                    raw_exit, reason = float(row["open"]), "gap_take_profit"
                elif float(row["low"]) <= position.stop:
                    raw_exit, reason = position.stop, "stop"
                elif float(row["high"]) >= position.take_profit:
                    raw_exit, reason = position.take_profit, "take_profit"
            else:
                if float(row["open"]) >= position.stop:
                    raw_exit, reason = float(row["open"]), "gap_stop"
                elif float(row["open"]) <= position.take_profit:
                    raw_exit, reason = float(row["open"]), "gap_take_profit"
                elif float(row["high"]) >= position.stop:
                    raw_exit, reason = position.stop, "stop"
                elif float(row["low"]) <= position.take_profit:
                    raw_exit, reason = position.take_profit, "take_profit"

            if reason is not None and raw_exit is not None:
                self._close_position(strategy, ts, raw_exit, reason)

    def _apply_risk_gate(self, ts: pd.Timestamp, close: float) -> None:
        self._mark_to_market(close)
        killed, reason = kill_switch_triggered(
            self.state.start_of_day_equity,
            self.state.peak_equity,
            self.state.equity,
            self.limits,
        )
        if not killed:
            return
        for strategy in list(self.state.positions):
            self._close_position(strategy, ts, close, f"kill_switch:{reason}")
        self.state.pending_signals.clear()
        self.state.halted = True
        self.state.halt_reason = reason
        self._mark_to_market(close)
        self.events.append(
            time=ts.isoformat(),
            event="HALT",
            strategy="PORTFOLIO",
            side="",
            lots="",
            expected_price=close,
            fill_price=close,
            slippage_pips=0.0,
            pnl="",
            balance=self.state.balance,
            equity=self.state.equity,
            reason=reason,
        )

    def process(self, bars: pd.DataFrame) -> dict[str, Any]:
        required = {"open", "high", "low", "close"}
        missing = required.difference(bars.columns)
        if missing:
            raise ValueError(f"missing OHLC columns: {sorted(missing)}")
        if bars.empty:
            return self.snapshot()
        if not isinstance(bars.index, pd.DatetimeIndex):
            raise ValueError("paper bars require a DatetimeIndex")

        signal_frames = {
            name: build_signals(bars, name, params)
            for name, params in self.strategies.items()
        }
        last_seen = pd.Timestamp(self.state.last_bar_time) if self.state.last_bar_time else None

        for ts, row in bars.sort_index().iterrows():
            if last_seen is not None and ts <= last_seen:
                continue

            day = ts.date().isoformat()
            if day != self.state.current_day:
                self.state.current_day = day
                self.state.start_of_day_equity = self.state.equity

            if not self.state.halted:
                self._execute_pending_at_open(ts, float(row["open"]))
            else:
                self.state.pending_signals.clear()

            self._process_intrabar_exits(ts, row)
            self._apply_risk_gate(ts, float(row["close"]))

            if not self.state.halted:
                for strategy, frame in signal_frames.items():
                    srow = frame.loc[ts]
                    side = int(srow["signal"])
                    if side == 0 or pd.isna(srow["stop_distance"]) or pd.isna(srow["take_profit_distance"]):
                        continue
                    self.state.pending_signals[strategy] = {
                        "side": side,
                        "stop_distance": float(srow["stop_distance"]),
                        "take_profit_distance": float(srow["take_profit_distance"]),
                    }
                    self.events.append(
                        time=ts.isoformat(),
                        event="SIGNAL",
                        strategy=strategy,
                        side=side,
                        lots="",
                        expected_price=float(row["close"]),
                        fill_price="",
                        slippage_pips="",
                        pnl="",
                        balance=self.state.balance,
                        equity=self.state.equity,
                        reason="queued_for_next_bar_open",
                    )

            self._mark_to_market(float(row["close"]))
            self.state.last_bar_time = ts.isoformat()
            self.store.save(self.state)
            last_seen = ts

        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "balance": self.state.balance,
            "equity": self.state.equity,
            "peak_equity": self.state.peak_equity,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "last_bar_time": self.state.last_bar_time,
            "open_positions": len(self.state.positions),
            "pending_signals": len(self.state.pending_signals),
            "positions": {name: asdict(position) for name, position in self.state.positions.items()},
            "weights": dict(self.weights),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
