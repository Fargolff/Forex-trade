from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.mt5_broker import AccountSnapshot, BrokerPosition, BrokerTick, SymbolSpec
from src.production import (
    AlertDispatcher,
    MetricStore,
    ProductionConfig,
    ProductionHaltStore,
    margin_stress_report,
    reconcile_slippage_samples,
    rotate_file,
    tick_future_seconds,
)


def _spec() -> SymbolSpec:
    return SymbolSpec(
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
        filling_mode=1,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        balance=10000.0,
        equity=10000.0,
        margin=1000.0,
        margin_free=9000.0,
        margin_level=1000.0,
        currency="USD",
        login=123,
        margin_mode=2,
        hedging=True,
    )


def test_tick_future_seconds_detects_future_broker_clock():
    now = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)
    tick = BrokerTick(bid=1.1, ask=1.1001, time_msc=int((now + timedelta(seconds=8)).timestamp() * 1000))
    assert tick_future_seconds(tick, now) == pytest.approx(8.0)


def test_margin_stress_uses_managed_stop_risk_and_extra_budget():
    tick = BrokerTick(bid=1.1000, ask=1.1001, time_msc=1)
    position = BrokerPosition(
        ticket=1,
        symbol="EURUSD",
        side=1,
        volume=0.10,
        price_open=1.1010,
        stop_loss=1.0950,
        take_profit=1.1100,
        magic=56001,
        comment="fat:ema_trend",
    )
    report = margin_stress_report(
        _account(),
        [position],
        tick,
        _spec(),
        additional_risk_fraction=0.0025,
        min_margin_level_pct=300.0,
        min_free_margin_pct=0.25,
    )
    assert report["existing_stop_risk"] == pytest.approx(50.0)
    assert report["additional_risk"] == pytest.approx(25.0)
    assert report["ok"] is True


def test_metric_store_builds_baseline_and_zscore(tmp_path):
    store = MetricStore(tmp_path / "metrics.json")
    for value in (1.0, 1.1, 0.9, 1.0, 1.05):
        store.observe("spread_pips", value)
    assert store.z_score("spread_pips", 2.0, min_samples=5) > 5.0
    store.save()
    reloaded = MetricStore(tmp_path / "metrics.json")
    assert int(reloaded.stats("spread_pips")["count"]) == 5


def test_reconcile_slippage_matches_entry_order_and_deal(tmp_path):
    events = tmp_path / "events.csv"
    events.write_text(
        "time,event,strategy,side,lots,price,stop_loss,take_profit,spread_pips,ticket,reason\n"
        "2026-09-18T00:00:00+00:00,ENTRY,ema_trend,1,0.01,1.1000,,,1.0,123,latest_completed_bar_signal\n"
        "2026-09-18T00:00:01+00:00,RECONCILE_DEAL,ema_trend,1,0.01,1.1002,,,,999,order=123;position=55;entry=0;profit=0;commission=0;swap=0\n",
        encoding="utf-8",
    )
    metrics = MetricStore(tmp_path / "metrics.json")
    samples = reconcile_slippage_samples(events, 0.0001, metrics)
    assert len(samples) == 1
    assert samples[0]["order"] == 123
    assert samples[0]["slippage_pips"] == pytest.approx(2.0)
    assert reconcile_slippage_samples(events, 0.0001, metrics) == []


def test_alert_dispatcher_writes_local_outbox_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.delenv("FOREX_ALERT_WEBHOOK_URL", raising=False)
    cfg = ProductionConfig(
        alert_outbox_path=str(tmp_path / "alerts.jsonl"),
        alert_state_path=str(tmp_path / "alert-state.json"),
        alert_cooldown_seconds=3600,
        max_log_bytes=1024,
        log_backups=2,
    )
    alerts = AlertDispatcher(cfg)
    assert alerts.emit("CRITICAL", "TEST", "boom") is True
    assert alerts.emit("CRITICAL", "TEST", "boom") is False
    assert len((tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_production_halt_requires_explicit_clear(tmp_path):
    halt = ProductionHaltStore(tmp_path / "halt.json")
    assert halt.load() is None
    halt.halt("TEST", "review required")
    assert halt.load()["halted"] is True
    assert halt.clear() is True
    assert halt.load() is None


def test_log_rotation_preserves_multiple_generations(tmp_path):
    target = tmp_path / "live_events.csv"
    target.write_text("first", encoding="utf-8")
    assert rotate_file(target, max_bytes=1, backups=3) is True
    target.write_text("second", encoding="utf-8")
    assert rotate_file(target, max_bytes=1, backups=3) is True
    target.write_text("third", encoding="utf-8")
    assert rotate_file(target, max_bytes=1, backups=3) is True
    assert (tmp_path / "live_events.csv.1").read_text(encoding="utf-8") == "third"
    assert (tmp_path / "live_events.csv.2").read_text(encoding="utf-8") == "second"
    assert (tmp_path / "live_events.csv.3").read_text(encoding="utf-8") == "first"
