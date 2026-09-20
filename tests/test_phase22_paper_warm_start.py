from pathlib import Path

import numpy as np
import pandas as pd

from src.paper import PaperConfig, PaperTradingEngine


def trending_bars(rows: int = 50) -> pd.DataFrame:
    idx = pd.date_range("2026-06-01", periods=rows, freq="h", tz="UTC")
    close = 1.10 + np.arange(rows) * 0.0004
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.0002
    low = np.minimum(open_, close) - 0.0002
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def make_engine(tmp_path: Path) -> tuple[PaperTradingEngine, PaperConfig]:
    cfg = PaperConfig(
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
    strategies = {
        "momentum": {
            "lookback": 2,
            "threshold": 0.0,
            "atr_period": 2,
            "stop_atr": 10.0,
            "take_profit_atr": 50.0,
        }
    }
    return PaperTradingEngine("EURUSD", strategies, {"momentum": 1.0}, cfg), cfg


def test_warm_start_seeds_latest_completed_bar_without_historical_trades(tmp_path):
    bars = trending_bars(40)
    engine, cfg = make_engine(tmp_path)
    snapshot = engine.warm_start(bars)
    assert pd.Timestamp(snapshot["last_bar_time"]) == bars.index[-1]
    assert snapshot["open_positions"] == 0
    assert snapshot["pending_signals"] == 0
    assert snapshot["balance"] == 10_000.0
    events = pd.read_csv(cfg.events_path)
    assert events["event"].tolist() == ["WARM_START"]
    assert not events["event"].isin(["SIGNAL", "ENTRY", "EXIT"]).any()


def test_warm_start_is_idempotent_for_same_history(tmp_path):
    bars = trending_bars(40)
    engine, cfg = make_engine(tmp_path)
    first = engine.warm_start(bars)
    before = Path(cfg.events_path).read_text(encoding="utf-8")
    restarted, _ = make_engine(tmp_path)
    second = restarted.warm_start(bars)
    assert second["last_bar_time"] == first["last_bar_time"]
    assert Path(cfg.events_path).read_text(encoding="utf-8") == before


def test_first_forward_signal_executes_only_on_following_bar(tmp_path, monkeypatch):
    bars = trending_bars(42)
    engine, cfg = make_engine(tmp_path)
    engine.warm_start(bars.iloc[:40])

    def deterministic_signals(frame, _name, _params):
        out = pd.DataFrame(index=frame.index)
        out["signal"] = 0
        out["stop_distance"] = 0.001
        out["take_profit_distance"] = 0.002
        if bars.index[40] in out.index:
            out.loc[bars.index[40], "signal"] = 1
        return out

    monkeypatch.setattr("src.paper.build_signals", deterministic_signals)

    first_forward = engine.process(bars.iloc[:41])
    events = pd.read_csv(cfg.events_path)
    assert pd.Timestamp(first_forward["last_bar_time"]) == bars.index[40]
    assert (events["event"] == "ENTRY").sum() == 0
    signals = events[events["event"] == "SIGNAL"]
    assert len(signals) == 1
    assert pd.Timestamp(signals.iloc[0]["time"]) == bars.index[40]

    second_forward = engine.process(bars.iloc[:42])
    events = pd.read_csv(cfg.events_path)
    entries = events[events["event"] == "ENTRY"]
    assert pd.Timestamp(second_forward["last_bar_time"]) == bars.index[41]
    assert len(entries) == 1
    assert pd.Timestamp(entries.iloc[0]["time"]) == bars.index[41]
    assert abs(float(entries.iloc[0]["fill_price"]) - float(bars.loc[bars.index[41], "open"])) < 1e-12


def test_warm_start_refuses_uninitialized_execution_state(tmp_path):
    bars = trending_bars(10)
    engine, _ = make_engine(tmp_path)
    engine.state.pending_signals["momentum"] = {
        "side": 1,
        "stop_distance": 0.001,
        "take_profit_distance": 0.002,
    }
    try:
        engine.warm_start(bars)
    except RuntimeError as exc:
        assert "execution state" in str(exc)
    else:
        raise AssertionError("warm_start should fail closed when execution state already exists")
