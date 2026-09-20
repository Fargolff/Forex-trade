from pathlib import Path

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
        '''{
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
}''',
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
