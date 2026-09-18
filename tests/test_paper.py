from pathlib import Path

import numpy as np
import pandas as pd

from src.paper import PaperConfig, PaperTradingEngine


def trending_bars(rows: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    close = 1.10 + np.arange(rows) * 0.0004
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.0002
    low = np.minimum(open_, close) - 0.0002
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def paper_config(tmp_path: Path) -> PaperConfig:
    return PaperConfig(
        initial_equity=10_000.0,
        risk_per_trade=0.01,
        max_daily_loss_pct=0.10,
        max_drawdown_pct=0.20,
        spread_pips=0.0,
        slippage_pips=0.0,
        commission_per_lot_round_turn=0.0,
        state_path=str(tmp_path / "state.json"),
        events_path=str(tmp_path / "events.csv"),
    )


def test_paper_state_is_persistent_and_idempotent(tmp_path):
    bars = trending_bars()
    strategies = {
        "momentum": {
            "lookback": 2,
            "threshold": 0.0,
            "atr_period": 2,
            "stop_atr": 5.0,
            "take_profit_atr": 20.0,
        }
    }
    cfg = paper_config(tmp_path)

    engine = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    first = engine.process(bars.iloc[:40])
    state_text = Path(cfg.state_path).read_text(encoding="utf-8")
    event_lines = Path(cfg.events_path).read_text(encoding="utf-8").splitlines()

    restarted = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    same = restarted.process(bars.iloc[:40])

    assert same["last_bar_time"] == first["last_bar_time"]
    assert Path(cfg.state_path).read_text(encoding="utf-8") == state_text
    assert Path(cfg.events_path).read_text(encoding="utf-8").splitlines() == event_lines


def test_signal_executes_on_next_bar_open(tmp_path):
    bars = trending_bars(30)
    strategies = {
        "momentum": {
            "lookback": 2,
            "threshold": 0.0,
            "atr_period": 2,
            "stop_atr": 10.0,
            "take_profit_atr": 50.0,
        }
    }
    cfg = paper_config(tmp_path)
    engine = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    engine.process(bars)

    events = pd.read_csv(cfg.events_path)
    signals = events[events["event"] == "SIGNAL"]
    entries = events[events["event"] == "ENTRY"]
    assert not signals.empty
    assert not entries.empty

    first_signal_time = pd.Timestamp(signals.iloc[0]["time"])
    first_entry = entries.iloc[0]
    entry_time = pd.Timestamp(first_entry["time"])
    expected_next = bars.index[bars.index.get_loc(first_signal_time) + 1]

    assert entry_time == expected_next
    assert abs(float(first_entry["fill_price"]) - float(bars.loc[entry_time, "open"])) < 1e-12


def test_restart_processes_only_new_bars(tmp_path):
    bars = trending_bars(50)
    strategies = {
        "momentum": {
            "lookback": 2,
            "threshold": 0.0,
            "atr_period": 2,
            "stop_atr": 5.0,
            "take_profit_atr": 20.0,
        }
    }
    cfg = paper_config(tmp_path)
    first_engine = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    first_engine.process(bars.iloc[:30])
    before = pd.read_csv(cfg.events_path)

    restarted = PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg)
    snapshot = restarted.process(bars)
    after = pd.read_csv(cfg.events_path)

    assert pd.Timestamp(snapshot["last_bar_time"]) == bars.index[-1]
    assert len(after) >= len(before)
    assert (pd.to_datetime(after["time"], utc=True) <= bars.index[-1]).all()
