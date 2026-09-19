from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .financing import FinancingSchedule, financing_between
from .risk import RiskLimits, kill_switch_triggered, position_size_lots


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    spread_pips: float = 0.8
    slippage_pips: float = 0.2
    commission_per_lot_round_turn: float = 7.0
    financing: FinancingSchedule = field(default_factory=FinancingSchedule)
    periods_per_year: float = 252.0 * 24.0


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: int
    entry: float
    exit: float
    lots: float
    pnl: float
    reason: str
    financing: float = 0.0


def _entry_price(raw_price: float, side: int, cfg: BacktestConfig) -> float:
    half_spread = (cfg.spread_pips * cfg.pip_size) / 2
    slip = cfg.slippage_pips * cfg.pip_size
    return raw_price + side * (half_spread + slip)


def _exit_price(raw_price: float, side: int, cfg: BacktestConfig) -> float:
    half_spread = (cfg.spread_pips * cfg.pip_size) / 2
    slip = cfg.slippage_pips * cfg.pip_size
    return raw_price - side * (half_spread + slip)


def _close_position(
    position: dict,
    raw_exit: float,
    exit_time: pd.Timestamp,
    reason: str,
    cfg: BacktestConfig,
) -> Trade:
    side = int(position["side"])
    exit_price = _exit_price(float(raw_exit), side, cfg)
    price_delta = (exit_price - float(position["entry"])) * side
    pnl_pips = price_delta / cfg.pip_size
    gross = pnl_pips * cfg.pip_value_per_lot * float(position["lots"])
    commission = cfg.commission_per_lot_round_turn * float(position["lots"])
    pnl = gross - commission
    return Trade(
        entry_time=position["entry_time"],
        exit_time=exit_time,
        side=side,
        entry=float(position["entry"]),
        exit=exit_price,
        lots=float(position["lots"]),
        pnl=float(pnl),
        reason=reason,
        financing=float(position.get("financing_accrued", 0.0)),
    )


def _apply_position_financing(position: dict, ts: pd.Timestamp, cfg: BacktestConfig) -> float:
    start = position.get("last_financing_time", position["entry_time"])
    accrual = financing_between(
        start,
        ts,
        side=int(position["side"]),
        lots=float(position["lots"]),
        schedule=cfg.financing,
    )
    position["last_financing_time"] = ts
    position["financing_accrued"] = float(position.get("financing_accrued", 0.0)) + accrual.cash
    return float(accrual.cash)


def run_backtest(df: pd.DataFrame, cfg: BacktestConfig) -> dict:
    required = {"open", "high", "low", "close", "signal", "stop_distance", "take_profit_distance"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    if cfg.periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if cfg.pip_size <= 0 or cfg.pip_value_per_lot <= 0:
        raise ValueError("pip_size and pip_value_per_lot must be positive")

    equity = cfg.initial_equity
    peak_equity = equity
    start_of_day_equity = equity
    current_day = None
    position: dict | None = None
    pending_signal: dict | None = None
    trades: list[Trade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    total_financing = 0.0

    limits = RiskLimits(
        risk_per_trade=cfg.risk_per_trade,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
    )

    for ts, row in df.iterrows():
        day = ts.date() if hasattr(ts, "date") else None
        if day != current_day:
            current_day = day
            start_of_day_equity = equity

        if position is not None:
            financing_cash = _apply_position_financing(position, ts, cfg)
            equity += financing_cash
            total_financing += financing_cash

        killed, kill_reason = kill_switch_triggered(start_of_day_equity, peak_equity, equity, limits)
        raw_open = float(row["open"])

        if killed:
            pending_signal = None
            if position is not None:
                trade = _close_position(position, raw_open, ts, f"kill_switch_{kill_reason}", cfg)
                equity += trade.pnl
                trades.append(trade)
                position = None
                peak_equity = max(peak_equity, equity)
            equity_curve.append((ts, equity))
            continue

        # A signal is only known after its source bar closes. Therefore a signal
        # generated on bar t is executed at bar t+1 open. This prevents same-bar
        # look-ahead optimism and also makes reversals explicit.
        if pending_signal is not None:
            pending_side = int(pending_signal["side"])
            if position is not None and int(position["side"]) == -pending_side:
                trade = _close_position(position, raw_open, ts, "opposite_signal", cfg)
                equity += trade.pnl
                trades.append(trade)
                position = None
                peak_equity = max(peak_equity, equity)

            if position is None:
                stop_distance = float(pending_signal["stop_distance"])
                tp_distance = float(pending_signal["take_profit_distance"])
                lots = position_size_lots(
                    equity=equity,
                    risk_per_trade=cfg.risk_per_trade,
                    stop_distance_price=stop_distance,
                    pip_size=cfg.pip_size,
                    pip_value_per_lot=cfg.pip_value_per_lot,
                )
                if lots > 0 and stop_distance > 0 and tp_distance > 0:
                    entry = _entry_price(raw_open, pending_side, cfg)
                    position = {
                        "entry_time": ts,
                        "side": pending_side,
                        "entry": entry,
                        "lots": lots,
                        "stop": entry - pending_side * stop_distance,
                        "tp": entry + pending_side * tp_distance,
                        "last_financing_time": ts,
                        "financing_accrued": 0.0,
                    }
            pending_signal = None

        # Stop is checked before take-profit when both are touched inside one OHLC
        # bar. Without intrabar data that is the conservative path assumption.
        if position is not None:
            side = int(position["side"])
            stop = float(position["stop"])
            tp = float(position["tp"])
            raw_exit: float | None = None
            exit_reason: str | None = None

            if side > 0:
                if raw_open <= stop:
                    raw_exit, exit_reason = raw_open, "stop_gap"
                elif float(row["low"]) <= stop:
                    raw_exit, exit_reason = stop, "stop"
                elif raw_open >= tp:
                    raw_exit, exit_reason = tp, "take_profit"
                elif float(row["high"]) >= tp:
                    raw_exit, exit_reason = tp, "take_profit"
            else:
                if raw_open >= stop:
                    raw_exit, exit_reason = raw_open, "stop_gap"
                elif float(row["high"]) >= stop:
                    raw_exit, exit_reason = stop, "stop"
                elif raw_open <= tp:
                    raw_exit, exit_reason = tp, "take_profit"
                elif float(row["low"]) <= tp:
                    raw_exit, exit_reason = tp, "take_profit"

            if exit_reason is not None and raw_exit is not None:
                trade = _close_position(position, raw_exit, ts, exit_reason, cfg)
                equity += trade.pnl
                trades.append(trade)
                position = None
                peak_equity = max(peak_equity, equity)

        signal = int(row["signal"])
        if signal != 0 and pd.notna(row["stop_distance"]) and pd.notna(row["take_profit_distance"]):
            stop_distance = float(row["stop_distance"])
            tp_distance = float(row["take_profit_distance"])
            if stop_distance > 0 and tp_distance > 0:
                pending_signal = {
                    "side": signal,
                    "stop_distance": stop_distance,
                    "take_profit_distance": tp_distance,
                    "signal_time": ts,
                }

        equity_curve.append((ts, equity))

    # Realize the final open position so summary metrics do not silently omit a
    # remaining trade at the end of the test sample. A signal on the final bar is
    # not entered because no next-bar execution price exists.
    if position is not None and len(df) > 0:
        final_ts = df.index[-1]
        final_close = float(df.iloc[-1]["close"])
        trade = _close_position(position, final_close, final_ts, "end_of_test", cfg)
        equity += trade.pnl
        trades.append(trade)
        peak_equity = max(peak_equity, equity)
        if equity_curve:
            equity_curve[-1] = (final_ts, equity)

    curve = pd.Series(
        [x[1] for x in equity_curve],
        index=[x[0] for x in equity_curve],
        name="equity",
        dtype=float,
    )
    returns = curve.pct_change().fillna(0.0)
    max_dd = ((curve.cummax() - curve) / curve.cummax()).max() if len(curve) else 0.0
    annualized_sharpe = 0.0
    if returns.std(ddof=0) > 0:
        annualized_sharpe = np.sqrt(cfg.periods_per_year) * returns.mean() / returns.std(ddof=0)

    pnl_values = [t.pnl for t in trades]
    wins = [x for x in pnl_values if x > 0]
    losses = [x for x in pnl_values if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "initial_equity": cfg.initial_equity,
        "final_equity": equity,
        "net_profit": equity - cfg.initial_equity,
        "total_financing": float(total_financing),
        "financing_enabled": bool(cfg.financing.enabled),
        "return_pct": (equity / cfg.initial_equity - 1) if cfg.initial_equity else 0.0,
        "max_drawdown_pct": float(max_dd),
        "sharpe_approx": float(annualized_sharpe),
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0,
        "trade_log": trades,
        "equity_curve": curve,
    }
