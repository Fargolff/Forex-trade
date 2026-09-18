import pytest

from src.live import ARM_PHRASE, LiveEngineConfig, preflight_report, require_live_arming, risk_sized_lots
from src.mt5_broker import AccountSnapshot, BrokerPosition, BrokerTick, SymbolSpec, TerminalSnapshot


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


class FakeBroker:
    def __init__(self, *, hedging=True, bid=1.10000, ask=1.10008, positions=None, connected=True, trade_allowed=True):
        self._account = AccountSnapshot(
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            currency="USD",
            login=123,
            margin_mode=2,
            hedging=hedging,
        )
        self._tick = BrokerTick(bid=bid, ask=ask, time_msc=0)
        self._positions = positions or []
        self._terminal = TerminalSnapshot(connected=connected, trade_allowed=trade_allowed, dlls_allowed=False)

    def terminal_snapshot(self):
        return self._terminal

    def account_snapshot(self):
        return self._account

    def symbol_spec(self, symbol):
        return _spec()

    def current_tick(self, symbol):
        return self._tick

    def open_positions(self, symbol=None, magic=None):
        return [p for p in self._positions if magic is None or p.magic == magic]


def test_live_arming_requires_config_and_exact_phrase():
    with pytest.raises(RuntimeError):
        require_live_arming(False, ARM_PHRASE)
    with pytest.raises(RuntimeError):
        require_live_arming(True, "yes")
    require_live_arming(True, ARM_PHRASE)


def test_risk_sized_lots_uses_tick_value_and_broker_step():
    lots = risk_sized_lots(
        equity=10000.0,
        risk_fraction=0.0025,
        strategy_weight=0.50,
        stop_distance=0.00100,
        spec=_spec(),
        max_lot_per_order=0.50,
    )
    assert lots == pytest.approx(0.12)


def test_risk_sized_lots_honors_hard_order_cap():
    lots = risk_sized_lots(
        equity=100000.0,
        risk_fraction=0.01,
        strategy_weight=1.0,
        stop_distance=0.00100,
        spec=_spec(),
        max_lot_per_order=0.02,
    )
    assert lots == pytest.approx(0.02)


def test_preflight_accepts_safe_hedging_environment():
    report = preflight_report(FakeBroker(), "EURUSD", LiveEngineConfig(), live_enabled=True)
    assert report["ok"] is True
    assert all(report["checks"].values())


def test_preflight_rejects_terminal_disconnect():
    report = preflight_report(
        FakeBroker(connected=False),
        "EURUSD",
        LiveEngineConfig(),
        live_enabled=True,
    )
    assert report["ok"] is False
    assert report["checks"]["terminal_connected"] is False


def test_preflight_rejects_netting_account_and_wide_spread():
    report = preflight_report(
        FakeBroker(hedging=False, bid=1.10000, ask=1.10030),
        "EURUSD",
        LiveEngineConfig(max_spread_pips=2.0),
        live_enabled=True,
    )
    assert report["ok"] is False
    assert report["checks"]["hedging_account"] is False
    assert report["checks"]["spread_within_cap"] is False


def test_preflight_counts_only_managed_magic_positions():
    managed = BrokerPosition(
        ticket=1,
        symbol="EURUSD",
        side=1,
        volume=0.02,
        price_open=1.1,
        stop_loss=1.09,
        take_profit=1.12,
        magic=56001,
        comment="fat:ema_trend",
    )
    foreign = BrokerPosition(
        ticket=2,
        symbol="EURUSD",
        side=1,
        volume=5.0,
        price_open=1.1,
        stop_loss=1.09,
        take_profit=1.12,
        magic=999,
        comment="manual",
    )
    report = preflight_report(
        FakeBroker(positions=[managed, foreign]),
        "EURUSD",
        LiveEngineConfig(max_total_lots=0.05),
        live_enabled=True,
    )
    assert report["managed_total_lots"] == pytest.approx(0.02)
    assert report["checks"]["total_lots_within_cap"] is True
