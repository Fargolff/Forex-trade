import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from src.mt5_broker import AccountSnapshot, BrokerDeal, BrokerPosition
from src.restart_reconcile import RestartReconcileConfig, restart_reconciliation_report


class FakeBroker:
    def __init__(self, positions=None, deals=None, login=123456):
        self._positions = list(positions or [])
        self._deals = list(deals or [])
        self._login = login

    def account_snapshot(self):
        return AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            currency="USD",
            login=self._login,
            margin_mode=2,
            hedging=True,
        )

    def open_positions(self, symbol=None, magic=None):
        items = self._positions
        if symbol is not None:
            items = [item for item in items if item.symbol == symbol]
        if magic is not None:
            items = [item for item in items if item.magic == magic]
        return list(items)

    def history_deals(self, start, end=None, symbol=None, magic=None):
        items = self._deals
        if symbol is not None:
            items = [item for item in items if item.symbol == symbol]
        if magic is not None:
            items = [item for item in items if item.magic == magic]
        return list(items)


def _position(volume=0.10):
    return BrokerPosition(
        ticket=501,
        symbol="EURUSD",
        side=1,
        volume=volume,
        price_open=1.1000,
        stop_loss=1.0900,
        take_profit=1.1200,
        magic=56001,
        comment="fat:ema_trend",
    )


def _deal(*, ticket=9001, order=101, position_id=501, time_msc=1_700_000_000_000, entry=0, volume=0.10, side=1, comment="fat:ema_trend"):
    return BrokerDeal(
        ticket=ticket,
        order=order,
        position_id=position_id,
        time_msc=time_msc,
        symbol="EURUSD",
        side=side,
        volume=volume,
        price=1.1000,
        profit=0.0,
        commission=0.0,
        swap=0.0,
        magic=56001,
        comment=comment,
        entry=entry,
    )


def _write_state(path: Path, *, halted=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 2, "halted": halted, "halt_reason": "test" if halted else None, "last_deal_time_msc": 0}),
        encoding="utf-8",
    )


def _write_entry(path: Path, ticket=101, strategy="ema_trend", side=1, lots=0.10):
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["time", "event", "strategy", "side", "lots", "price", "stop_loss", "take_profit", "spread_pips", "ticket", "reason"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow(
            {
                "time": "2026-09-18T00:00:00+00:00",
                "event": "ENTRY",
                "strategy": strategy,
                "side": side,
                "lots": lots,
                "price": 1.1000,
                "stop_loss": 1.0900,
                "take_profit": 1.1200,
                "spread_pips": 0.8,
                "ticket": ticket,
                "reason": "latest_completed_bar_signal",
            }
        )


def _cfg(tmp_path: Path):
    return RestartReconcileConfig(
        lookback_hours=24 * 365,
        report_path=str(tmp_path / "restart_report.json"),
        checkpoint_path=str(tmp_path / "restart_checkpoint.json"),
        event_backups=2,
        volume_tolerance=1e-8,
        require_local_entry=True,
    )


def _run(tmp_path: Path, broker: FakeBroker, *, persist=False):
    state = tmp_path / "live_state.json"
    events = tmp_path / "live_events.csv"
    return restart_reconciliation_report(
        broker,
        symbol="EURUSD",
        magic=56001,
        allowed_strategies={"ema_trend"},
        state_path=state,
        events_path=events,
        cfg=_cfg(tmp_path),
        now=datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc),
        persist=persist,
    )


def test_valid_open_position_reconciles_and_writes_checkpoint(tmp_path):
    _write_state(tmp_path / "live_state.json")
    _write_entry(tmp_path / "live_events.csv")
    broker = FakeBroker(positions=[_position()], deals=[_deal()])

    result = _run(tmp_path, broker, persist=True)

    assert result["ok"] is True
    assert result["deal_cursor"] == {"time_msc": 1_700_000_000_000, "ticket": 9001}
    checkpoint = json.loads((tmp_path / "restart_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["account_login"] == 123456
    assert checkpoint["open_positions"][0]["position_id"] == 501


def test_broker_position_without_local_entry_fails_closed(tmp_path):
    _write_state(tmp_path / "live_state.json")
    broker = FakeBroker(positions=[_position()], deals=[_deal()])

    result = _run(tmp_path, broker)

    assert result["ok"] is False
    assert "BROKER_POSITION_WITHOUT_LOCAL_ENTRY" in {item["code"] for item in result["incidents"]}


def test_volume_mismatch_fails_closed(tmp_path):
    _write_state(tmp_path / "live_state.json")
    _write_entry(tmp_path / "live_events.csv")
    broker = FakeBroker(positions=[_position(volume=0.20)], deals=[_deal(volume=0.10)])

    result = _run(tmp_path, broker)

    assert result["ok"] is False
    assert "POSITION_VOLUME_MISMATCH" in {item["code"] for item in result["incidents"]}


def test_prior_position_disappearance_requires_exit_deal(tmp_path):
    _write_state(tmp_path / "live_state.json")
    cfg = _cfg(tmp_path)
    Path(cfg.checkpoint_path).write_text(
        json.dumps(
            {
                "version": 1,
                "account_login": 123456,
                "symbol": "EURUSD",
                "magic": 56001,
                "deal_cursor": {"time_msc": 1_700_000_000_000, "ticket": 9001},
                "open_positions": [{"position_id": 501, "strategy": "ema_trend"}],
            }
        ),
        encoding="utf-8",
    )
    broker = FakeBroker(positions=[], deals=[_deal(ticket=9001)])

    result = _run(tmp_path, broker)

    assert result["ok"] is False
    codes = {item["code"] for item in result["incidents"]}
    assert "POSITION_DISAPPEARED_WITHOUT_EXIT_DEAL" in codes
    assert "DEAL_HISTORY_OPEN_WITHOUT_POSITION" in codes


def test_prior_position_disappearance_is_explained_by_later_exit(tmp_path):
    _write_state(tmp_path / "live_state.json")
    cfg = _cfg(tmp_path)
    Path(cfg.checkpoint_path).write_text(
        json.dumps(
            {
                "version": 1,
                "account_login": 123456,
                "symbol": "EURUSD",
                "magic": 56001,
                "deal_cursor": {"time_msc": 1_700_000_000_000, "ticket": 9001},
                "open_positions": [{"position_id": 501, "strategy": "ema_trend"}],
            }
        ),
        encoding="utf-8",
    )
    exit_deal = _deal(ticket=9002, order=202, time_msc=1_700_000_000_001, entry=1, side=-1, comment="forex-auto-trader:flatten")
    broker = FakeBroker(positions=[], deals=[_deal(ticket=9001), exit_deal])

    result = _run(tmp_path, broker)

    assert result["ok"] is True
    assert result["deal_cursor"] == {"time_msc": 1_700_000_000_001, "ticket": 9002}


def test_checkpoint_detects_account_switch(tmp_path):
    _write_state(tmp_path / "live_state.json")
    cfg = _cfg(tmp_path)
    Path(cfg.checkpoint_path).write_text(
        json.dumps(
            {
                "version": 1,
                "account_login": 111111,
                "symbol": "EURUSD",
                "magic": 56001,
                "deal_cursor": {"time_msc": 0, "ticket": 0},
                "open_positions": [],
            }
        ),
        encoding="utf-8",
    )

    result = _run(tmp_path, FakeBroker(login=222222))

    assert result["ok"] is False
    assert "ACCOUNT_IDENTITY_CHANGED" in {item["code"] for item in result["incidents"]}


def test_same_millisecond_cursor_uses_ticket_tiebreaker(tmp_path):
    _write_state(tmp_path / "live_state.json")
    in_deal = _deal(ticket=9100, time_msc=1_700_000_000_123, volume=0.10)
    out_deal = _deal(
        ticket=9101,
        order=202,
        time_msc=1_700_000_000_123,
        entry=1,
        volume=0.10,
        side=-1,
        comment="forex-auto-trader:flatten",
    )
    broker = FakeBroker(positions=[], deals=[in_deal, out_deal])

    result = _run(tmp_path, broker)

    assert result["ok"] is True
    assert result["deal_cursor"] == {"time_msc": 1_700_000_000_123, "ticket": 9101}


def test_halted_local_state_blocks_restart(tmp_path):
    _write_state(tmp_path / "live_state.json", halted=True)

    result = _run(tmp_path, FakeBroker())

    assert result["ok"] is False
    assert "LOCAL_LIVE_STATE_HALTED" in {item["code"] for item in result["incidents"]}
