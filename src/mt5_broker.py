from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


@dataclass(frozen=True)
class BrokerOrder:
    symbol: str
    side: int
    lots: float
    stop_loss: float
    take_profit: float
    magic: int
    deviation_points: int = 20
    comment: str = "forex-auto-trader"


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    trade_allowed: bool
    filling_mode: int

    @property
    def pip_size(self) -> float:
        return self.point * 10.0 if self.digits in (3, 5) else self.point

    def normalize_volume(self, lots: float) -> float:
        if lots <= 0 or self.volume_step <= 0:
            return 0.0
        capped = min(float(lots), self.volume_max)
        steps = int((capped + 1e-12) / self.volume_step)
        normalized = steps * self.volume_step
        if normalized < self.volume_min - 1e-12:
            return 0.0
        decimals = max(0, len(str(self.volume_step).split(".")[-1].rstrip("0"))) if "." in str(self.volume_step) else 0
        return round(normalized, decimals + 2)


@dataclass(frozen=True)
class BrokerTick:
    bid: float
    ask: float
    time_msc: int


@dataclass(frozen=True)
class TerminalSnapshot:
    connected: bool
    trade_allowed: bool
    dlls_allowed: bool


@dataclass(frozen=True)
class AccountSnapshot:
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    currency: str
    login: int
    margin_mode: int
    hedging: bool


@dataclass(frozen=True)
class BrokerPosition:
    ticket: int
    symbol: str
    side: int
    volume: float
    price_open: float
    stop_loss: float
    take_profit: float
    magic: int
    comment: str


@dataclass(frozen=True)
class BrokerDeal:
    ticket: int
    order: int
    position_id: int
    time_msc: int
    symbol: str
    side: int
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    magic: int
    comment: str
    entry: int


class MT5Broker:
    def __init__(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is not installed")
        self.connected = False

    def connect(self) -> None:
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        self.connected = True

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def close(self) -> None:
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def terminal_snapshot(self) -> TerminalSnapshot:
        info = mt5.terminal_info()
        if info is None:
            raise RuntimeError(f"MT5 terminal_info failed: {mt5.last_error()}")
        return TerminalSnapshot(
            connected=bool(getattr(info, "connected", False)),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            dlls_allowed=bool(getattr(info, "dlls_allowed", False)),
        )

    def rates(self, symbol: str, timeframe: str, bars: int = 2000) -> pd.DataFrame:
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        tf = getattr(mt5, _TIMEFRAMES[timeframe])
        data = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if data is None or len(data) == 0:
            raise RuntimeError(f"no MT5 rates for {symbol}: {mt5.last_error()}")

        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]

    def _ensure_symbol(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol not found: {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"unable to select symbol: {symbol}")
        return mt5.symbol_info(symbol) or info

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        info = self._ensure_symbol(symbol)
        tick_value = float(getattr(info, "trade_tick_value_loss", 0.0) or getattr(info, "trade_tick_value", 0.0) or 0.0)
        trade_mode = int(getattr(info, "trade_mode", 0))
        disabled_mode = int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0))
        return SymbolSpec(
            symbol=symbol,
            digits=int(info.digits),
            point=float(info.point),
            tick_size=float(getattr(info, "trade_tick_size", 0.0) or info.point),
            tick_value=tick_value,
            contract_size=float(getattr(info, "trade_contract_size", 0.0) or 0.0),
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            volume_max=float(info.volume_max),
            trade_allowed=trade_mode != disabled_mode,
            filling_mode=int(getattr(info, "filling_mode", 0)),
        )

    def current_tick(self, symbol: str) -> BrokerTick:
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"missing tick for {symbol}: {mt5.last_error()}")
        return BrokerTick(bid=float(tick.bid), ask=float(tick.ask), time_msc=int(getattr(tick, "time_msc", 0) or 0))

    def account_snapshot(self) -> AccountSnapshot:
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
        margin_mode = int(getattr(info, "margin_mode", -1))
        hedging_mode = int(getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2))
        return AccountSnapshot(
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(info.margin),
            margin_free=float(info.margin_free),
            margin_level=float(getattr(info, "margin_level", 0.0) or 0.0),
            currency=str(getattr(info, "currency", "")),
            login=int(getattr(info, "login", 0) or 0),
            margin_mode=margin_mode,
            hedging=margin_mode == hedging_mode,
        )

    def account_equity(self) -> float:
        return self.account_snapshot().equity

    def open_positions(self, symbol: str | None = None, magic: int | None = None) -> list[BrokerPosition]:
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if raw is None:
            raise RuntimeError(f"positions_get failed: {mt5.last_error()}")
        out: list[BrokerPosition] = []
        buy_type = int(mt5.POSITION_TYPE_BUY)
        for pos in raw:
            pos_magic = int(getattr(pos, "magic", 0) or 0)
            if magic is not None and pos_magic != magic:
                continue
            side = 1 if int(pos.type) == buy_type else -1
            out.append(
                BrokerPosition(
                    ticket=int(pos.ticket),
                    symbol=str(pos.symbol),
                    side=side,
                    volume=float(pos.volume),
                    price_open=float(pos.price_open),
                    stop_loss=float(getattr(pos, "sl", 0.0) or 0.0),
                    take_profit=float(getattr(pos, "tp", 0.0) or 0.0),
                    magic=pos_magic,
                    comment=str(getattr(pos, "comment", "") or ""),
                )
            )
        return out

    def history_deals(
        self,
        start: datetime,
        end: datetime | None = None,
        symbol: str | None = None,
        magic: int | None = None,
    ) -> list[BrokerDeal]:
        finish = end or datetime.now(timezone.utc)
        raw = mt5.history_deals_get(start, finish)
        if raw is None:
            raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
        buy_type = int(getattr(mt5, "DEAL_TYPE_BUY", 0))
        sell_type = int(getattr(mt5, "DEAL_TYPE_SELL", 1))
        out: list[BrokerDeal] = []
        for deal in raw:
            deal_symbol = str(getattr(deal, "symbol", "") or "")
            deal_magic = int(getattr(deal, "magic", 0) or 0)
            if symbol is not None and deal_symbol != symbol:
                continue
            if magic is not None and deal_magic != magic:
                continue
            deal_type = int(getattr(deal, "type", -1))
            side = 1 if deal_type == buy_type else -1 if deal_type == sell_type else 0
            out.append(
                BrokerDeal(
                    ticket=int(getattr(deal, "ticket", 0) or 0),
                    order=int(getattr(deal, "order", 0) or 0),
                    position_id=int(getattr(deal, "position_id", 0) or 0),
                    time_msc=int(getattr(deal, "time_msc", 0) or 0),
                    symbol=deal_symbol,
                    side=side,
                    volume=float(getattr(deal, "volume", 0.0) or 0.0),
                    price=float(getattr(deal, "price", 0.0) or 0.0),
                    profit=float(getattr(deal, "profit", 0.0) or 0.0),
                    commission=float(getattr(deal, "commission", 0.0) or 0.0),
                    swap=float(getattr(deal, "swap", 0.0) or 0.0),
                    magic=deal_magic,
                    comment=str(getattr(deal, "comment", "") or ""),
                    entry=int(getattr(deal, "entry", -1)),
                )
            )
        return sorted(out, key=lambda item: (item.time_msc, item.ticket))

    def _filling_candidates(self, preferred: int) -> list[int]:
        candidates: list[int] = []
        for value in (
            preferred,
            int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
            int(getattr(mt5, "ORDER_FILLING_FOK", 0)),
            int(getattr(mt5, "ORDER_FILLING_RETURN", 2)),
        ):
            if value not in candidates:
                candidates.append(value)
        return candidates

    def _checked_request(self, request: dict[str, Any], preferred_filling: int) -> dict[str, Any]:
        last_error: str | None = None
        for filling in self._filling_candidates(preferred_filling):
            candidate = dict(request)
            candidate["type_filling"] = filling
            checked = mt5.order_check(candidate)
            if checked is not None and int(getattr(checked, "retcode", -1)) == 0:
                return candidate
            if checked is not None:
                last_error = f"retcode={getattr(checked, 'retcode', None)} comment={getattr(checked, 'comment', '')}"
        raise RuntimeError(f"MT5 order_check rejected all filling modes: {last_error or mt5.last_error()}")

    def _send_checked(self, request: dict[str, Any], preferred_filling: int):
        checked_request = self._checked_request(request, preferred_filling)
        result = mt5.order_send(checked_request)
        if result is None:
            raise RuntimeError(f"MT5 order_send returned None: {mt5.last_error()}")
        success_codes = {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        }
        if int(getattr(result, "retcode", -1)) not in success_codes:
            raise RuntimeError(
                f"MT5 order_send failed: retcode={getattr(result, 'retcode', None)} "
                f"comment={getattr(result, 'comment', '')}"
            )
        return result

    def market_order(self, order: BrokerOrder, live_enabled: bool = False):
        if not live_enabled:
            raise RuntimeError("live trading is disabled in config")
        if order.side not in (-1, 1):
            raise ValueError("side must be -1 or 1")

        info = self._ensure_symbol(order.symbol)
        spec = self.symbol_spec(order.symbol)
        lots = spec.normalize_volume(order.lots)
        if lots <= 0:
            raise ValueError("order volume is below broker minimum or invalid")
        tick = self.current_tick(order.symbol)

        is_buy = order.side == 1
        price = tick.ask if is_buy else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(lots),
            "type": order_type,
            "price": float(price),
            "sl": float(order.stop_loss),
            "tp": float(order.take_profit),
            "deviation": int(order.deviation_points),
            "magic": int(order.magic),
            "comment": str(order.comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        return self._send_checked(request, int(getattr(info, "filling_mode", 0)))

    def close_position(self, position: BrokerPosition, deviation_points: int = 20, live_enabled: bool = False):
        if not live_enabled:
            raise RuntimeError("live trading is disabled in config")
        info = self._ensure_symbol(position.symbol)
        tick = self.current_tick(position.symbol)
        close_side = -position.side
        is_buy = close_side == 1
        price = tick.ask if is_buy else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "position": int(position.ticket),
            "volume": float(position.volume),
            "type": order_type,
            "price": float(price),
            "deviation": int(deviation_points),
            "magic": int(position.magic),
            "comment": "forex-auto-trader:flatten",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        return self._send_checked(request, int(getattr(info, "filling_mode", 0)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
