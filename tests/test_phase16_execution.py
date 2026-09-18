from types import SimpleNamespace

import pytest

import src.mt5_broker as broker_module
from src.live import LiveState, LiveStateStore, execution_receipt_issue
from src.mt5_broker import (
    BrokerOrderRejected,
    BrokerSubmissionAmbiguous,
    ExecutionReceipt,
    MT5Broker,
)


class FakeMT5:
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self, result):
        self.result = result
        self.sent = []

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request):
        self.sent.append(dict(request))
        return self.result

    def last_error(self):
        return (0, "fake")


def _broker(monkeypatch, result):
    fake = FakeMT5(result)
    monkeypatch.setattr(broker_module, "mt5", fake)
    return MT5Broker(), fake


def _result(retcode, *, order=10, deal=20, volume=0.10, price=1.10001, comment="ok"):
    return SimpleNamespace(
        retcode=retcode,
        order=order,
        deal=deal,
        volume=volume,
        price=price,
        comment=comment,
    )


def test_done_normalizes_to_full_execution_receipt(monkeypatch):
    broker, fake = _broker(monkeypatch, _result(FakeMT5.TRADE_RETCODE_DONE, volume=0.10))
    receipt = broker._send_checked({"volume": 0.10}, preferred_filling=1)

    assert receipt.status == "FILLED"
    assert receipt.order == 10
    assert receipt.deal == 20
    assert receipt.requested_volume == pytest.approx(0.10)
    assert receipt.filled_volume == pytest.approx(0.10)
    assert len(fake.sent) == 1


def test_done_without_echoed_volume_uses_checked_request_volume(monkeypatch):
    broker, _ = _broker(monkeypatch, _result(FakeMT5.TRADE_RETCODE_DONE, volume=0.0))
    receipt = broker._send_checked({"volume": 0.12}, preferred_filling=1)
    assert receipt.status == "FILLED"
    assert receipt.filled_volume == pytest.approx(0.12)


def test_partial_fill_is_preserved_and_flagged(monkeypatch):
    broker, _ = _broker(monkeypatch, _result(FakeMT5.TRADE_RETCODE_DONE_PARTIAL, volume=0.04))
    receipt = broker._send_checked({"volume": 0.10}, preferred_filling=1)

    assert receipt.status == "PARTIAL"
    assert receipt.filled_volume == pytest.approx(0.04)
    assert execution_receipt_issue(receipt, requested_volume=0.10, volume_tolerance=0.005) == "PARTIAL_FILL"


def test_placed_market_order_is_accepted_but_unconfirmed(monkeypatch):
    broker, _ = _broker(monkeypatch, _result(FakeMT5.TRADE_RETCODE_PLACED, volume=0.0))
    receipt = broker._send_checked({"volume": 0.10}, preferred_filling=1)

    assert receipt.status == "PLACED"
    assert execution_receipt_issue(receipt, requested_volume=0.10, volume_tolerance=0.005) == "ORDER_ACCEPTED_UNCONFIRMED"


def test_none_submission_result_is_ambiguous_and_must_not_be_retried(monkeypatch):
    broker, fake = _broker(monkeypatch, None)
    with pytest.raises(BrokerSubmissionAmbiguous, match="order_send returned None"):
        broker._send_checked({"volume": 0.10}, preferred_filling=1)
    assert len(fake.sent) == 1


def test_explicit_order_send_reject_is_distinct_from_ambiguity(monkeypatch):
    broker, _ = _broker(monkeypatch, _result(10030, comment="invalid fills"))
    with pytest.raises(BrokerOrderRejected, match="retcode=10030"):
        broker._send_checked({"volume": 0.10}, preferred_filling=1)


def test_full_receipt_with_wrong_volume_fails_closed():
    receipt = ExecutionReceipt(
        retcode=10009,
        status="FILLED",
        order=1,
        deal=2,
        requested_volume=0.10,
        filled_volume=0.08,
        price=1.10,
        comment="ok",
    )
    assert execution_receipt_issue(receipt, requested_volume=0.10, volume_tolerance=0.005) == "FILL_VOLUME_MISMATCH"


def test_pending_order_intent_round_trips_atomically(tmp_path):
    store = LiveStateStore(tmp_path / "live_state.json")
    state = LiveState(
        pending_order_intent={
            "action": "ENTRY",
            "strategy": "ema_trend",
            "symbol": "EURUSD",
            "side": 1,
            "lots": 0.10,
            "bar_time": "2026-09-18T08:00:00+00:00",
        }
    )
    store.save(state)

    loaded = store.load()
    assert loaded.version == 3
    assert loaded.pending_order_intent is not None
    assert loaded.pending_order_intent["action"] == "ENTRY"
    assert loaded.pending_order_intent["lots"] == pytest.approx(0.10)
    assert not (tmp_path / "live_state.json.tmp").exists()
