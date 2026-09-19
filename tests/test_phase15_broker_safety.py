import pytest

from src.live import LiveEngineConfig, broker_risk_sized_lots, margin_safety_report, preflight_report
from src.mt5_broker import (
    AccountSnapshot,
    BrokerTick,
    SymbolSpec,
    TerminalSnapshot,
    normalize_and_validate_protective_prices,
)


def _spec(*, stops=0, freeze=0):
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
        trade_stops_level=stops,
        trade_freeze_level=freeze,
    )


class FakeBroker:
    def __init__(self, *, login=123, required_margin=100.0, loss_per_lot=100.0):
        self.login = login
        self.required_margin = required_margin
        self.loss_per_lot = loss_per_lot

    def loss_per_lot_to_stop(self, symbol, side, entry_price, stop_loss):
        assert symbol == "EURUSD"
        assert side in (-1, 1)
        assert entry_price > 0 and stop_loss > 0
        return self.loss_per_lot

    def order_calc_margin(self, symbol, side, volume, price):
        assert symbol == "EURUSD"
        assert side in (-1, 1)
        assert volume > 0 and price > 0
        return self.required_margin

    def terminal_snapshot(self):
        return TerminalSnapshot(connected=True, trade_allowed=True, dlls_allowed=False)

    def account_snapshot(self):
        return AccountSnapshot(
            balance=1000.0,
            equity=1000.0,
            margin=100.0,
            margin_free=900.0,
            margin_level=1000.0,
            currency="USD",
            login=self.login,
            margin_mode=2,
            hedging=True,
        )

    def symbol_spec(self, symbol):
        return _spec()

    def current_tick(self, symbol):
        return BrokerTick(bid=1.10000, ask=1.10008, time_msc=1)

    def open_positions(self, symbol=None, magic=None):
        return []


def test_price_normalization_uses_broker_tick_size():
    spec = _spec()
    assert spec.normalize_price(1.100083) == pytest.approx(1.10008)


def test_protective_levels_enforce_stops_and_freeze_distance():
    spec = _spec(stops=30, freeze=50)
    entry, stop, take = normalize_and_validate_protective_prices(
        spec, 1, 1.10000, 1.09940, 1.10070
    )
    assert entry == pytest.approx(1.10000)
    assert stop == pytest.approx(1.09940)
    assert take == pytest.approx(1.10070)

    with pytest.raises(ValueError, match="below broker minimum"):
        normalize_and_validate_protective_prices(spec, 1, 1.10000, 1.09970, 1.10070)


def test_broker_native_sizing_uses_account_currency_loss_per_lot():
    lots = broker_risk_sized_lots(
        FakeBroker(loss_per_lot=100.0),
        "EURUSD",
        equity=10000.0,
        risk_fraction=0.0025,
        strategy_weight=0.50,
        entry_price=1.10000,
        stop_loss=1.09900,
        spec=_spec(),
        max_lot_per_order=0.50,
    )
    # Risk budget = 12.50 USD. 100 USD loss per 1 lot => 0.125 lot,
    # normalized down to the broker's 0.01 step.
    assert lots == pytest.approx(0.12)


def test_margin_gate_accepts_safe_projection_and_rejects_excess_margin():
    account = FakeBroker().account_snapshot()
    cfg = LiveEngineConfig(
        max_margin_fraction_of_equity=0.25,
        min_free_margin_fraction_after_order=0.50,
    )
    safe = margin_safety_report(FakeBroker(required_margin=100.0), account, "EURUSD", 1, 0.10, 1.10, cfg)
    assert safe["ok"] is True
    assert safe["projected_margin_fraction"] == pytest.approx(0.20)
    assert safe["projected_free_margin_fraction"] == pytest.approx(0.80)

    unsafe = margin_safety_report(FakeBroker(required_margin=300.0), account, "EURUSD", 1, 0.10, 1.10, cfg)
    assert unsafe["ok"] is False
    assert unsafe["checks"]["projected_margin_within_cap"] is False


def test_preflight_rejects_login_outside_allowlist():
    cfg = LiveEngineConfig(
        require_account_allowlist=True,
        allowed_account_logins=(999,),
    )
    report = preflight_report(FakeBroker(login=123), "EURUSD", cfg, live_enabled=True)
    assert report["ok"] is False
    assert report["checks"]["account_login_allowed"] is False


def test_preflight_accepts_login_inside_allowlist():
    cfg = LiveEngineConfig(
        require_account_allowlist=True,
        allowed_account_logins=(123, 999),
    )
    report = preflight_report(FakeBroker(login=123), "EURUSD", cfg, live_enabled=True)
    assert report["checks"]["account_login_allowed"] is True
