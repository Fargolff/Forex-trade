from src.risk import RiskLimits, kill_switch_triggered, position_size_lots


def test_position_size_lots_basic():
    lots = position_size_lots(
        equity=10_000,
        risk_per_trade=0.01,
        stop_distance_price=0.0020,
        pip_size=0.0001,
        pip_value_per_lot=10,
    )
    assert lots == 0.5


def test_daily_loss_kill_switch():
    limits = RiskLimits(risk_per_trade=0.005, max_daily_loss_pct=0.02, max_drawdown_pct=0.10)
    killed, reason = kill_switch_triggered(
        start_of_day_equity=10_000,
        peak_equity=10_000,
        current_equity=9_790,
        limits=limits,
    )
    assert killed is True
    assert reason == "max_daily_loss"


def test_drawdown_kill_switch():
    limits = RiskLimits(risk_per_trade=0.005, max_daily_loss_pct=0.20, max_drawdown_pct=0.10)
    killed, reason = kill_switch_triggered(
        start_of_day_equity=9_500,
        peak_equity=10_000,
        current_equity=8_950,
        limits=limits,
    )
    assert killed is True
    assert reason == "max_drawdown"
