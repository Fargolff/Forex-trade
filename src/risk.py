from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10


def position_size_lots(
    equity: float,
    risk_per_trade: float,
    stop_distance_price: float,
    pip_size: float,
    pip_value_per_lot: float,
    min_lot: float = 0.01,
    lot_step: float = 0.01,
    max_lot: float = 100.0,
) -> float:
    if equity <= 0:
        return 0.0
    if not 0 < risk_per_trade < 1:
        raise ValueError("risk_per_trade must be between 0 and 1")
    if stop_distance_price <= 0 or pip_size <= 0 or pip_value_per_lot <= 0:
        raise ValueError("stop distance, pip size and pip value must be positive")

    risk_cash = equity * risk_per_trade
    stop_pips = stop_distance_price / pip_size
    raw_lots = risk_cash / (stop_pips * pip_value_per_lot)

    stepped = int(raw_lots / lot_step) * lot_step
    if stepped < min_lot:
        return 0.0
    return round(min(stepped, max_lot), 8)


def drawdown_pct(peak_equity: float, equity: float) -> float:
    if peak_equity <= 0:
        return 0.0
    return max(0.0, (peak_equity - equity) / peak_equity)


def kill_switch_triggered(
    start_of_day_equity: float,
    peak_equity: float,
    current_equity: float,
    limits: RiskLimits,
) -> tuple[bool, str | None]:
    if start_of_day_equity > 0:
        daily_loss = max(0.0, (start_of_day_equity - current_equity) / start_of_day_equity)
        if daily_loss >= limits.max_daily_loss_pct:
            return True, "max_daily_loss"

    if drawdown_pct(peak_equity, current_equity) >= limits.max_drawdown_pct:
        return True, "max_drawdown"

    return False, None
