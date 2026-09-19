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
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_mode: int = 0
    swap_rollover3days: int = -1

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

    def normalize_price(self, price: float) -> float:
        if price <= 0 or self.tick_size <= 0:
            raise ValueError("price and broker tick_size must be positive")
        ticks = round(float(price) / self.tick_size)
        return round(ticks * self.tick_size, self.digits)

    @property
    def minimum_protective_distance(self) -> float:
        # Freeze level is included conservatively. Some brokers apply it mainly
        # to modifications, but refusing a too-close initial protection level is
        # safer than assuming it will remain modifiable immediately after fill.
        return max(int(self.trade_stops_level), int(self.trade_freeze_level), 0) * self.point


def normalize_and_validate_protective_prices(
    spec: SymbolSpec,
    side: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
) -> tuple[float, float, float]:
    if side not in (-1, 1):
        raise ValueError("side must be -1 or 1")
    entry = spec.normalize_price(entry_price)
    stop = spec.normalize_price(stop_loss)
    take = spec.normalize_price(take_profit)
    if side > 0 and not (stop < entry < take):
        raise ValueError("buy protection must satisfy stop_loss < entry < take_profit")
    if side < 0 and not (take < entry < stop):
        raise ValueError("sell protection must satisfy take_profit < entry < stop_loss")
    minimum = spec.minimum_protective_distance
    if minimum > 0:
        if abs(entry - stop) + 1e-12 < minimum:
            raise ValueError(f"stop-loss distance is below broker minimum {minimum}")
        if abs(take - entry) + 1e-12 < minimum:
            raise ValueError(f"take-profit distance is below broker minimum {minimum}")
    return entry, stop, take


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
    identifier: int = 0


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

@dataclass(frozen=True)
class BrokerWorkingOrder:
    ticket: int
    time_setup_msc: int
    time_done_msc: int
    symbol: str
    side: int
    volume_initial: float
    volume_current: float
    price_open: float
    stop_loss: float
    take_profit: float
    magic: int
    comment: str
    state: str


class BrokerOrderRejected(RuntimeError):
    """Broker explicitly rejected an order before/at submission."""


class BrokerSubmissionAmbiguous(RuntimeError):
    """Submission outcome is unknown; callers must reconcile before retrying."""


@dataclass(frozen=True)
class ExecutionReceipt:
    retcode: int
    status: str
    order: int
    deal: int
    requested_volume: float
    filled_volume: float
    price: float
    comment: str

    @property
    def ticket(self) -> int:
        return self.order or self.deal


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
            trade_stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
            trade_freeze_level=int(getattr(info, "trade_freeze_level", 0) or 0),
            swap_long=float(getattr(info, "swap_long", 0.0) or 0.0),
            swap_short=float(getattr(info, "swap_short", 0.0) or 0.0),
            swap_mode=int(getattr(info, "swap_mode", 0) or 0),
            swap_rollover3days=int(getattr(info, "swap_rollover3days", -1)),
        )

    def current_tick(self, symbol: str) -> BrokerTick:
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"missing tick for {symbol}: {mt5.last_error()}")
        bid = float(tick.bid)
        ask = float(tick.ask)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError(f"invalid broker tick for {symbol}: bid={bid} ask={ask}")
        return BrokerTick(bid=bid, ask=ask, time_msc=int(getattr(tick, "time_msc", 0) or 0))

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
                    identifier=int(getattr(pos, "identifier", 0) or getattr(pos, "ticket", 0) or 0),
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

    def _order_state_name(self, state: int) -> str:
        mapping = {
            int(getattr(mt5, "ORDER_STATE_STARTED", -101)): "STARTED",
            int(getattr(mt5, "ORDER_STATE_PLACED", -102)): "PLACED",
            int(getattr(mt5, "ORDER_STATE_CANCELED", -103)): "CANCELED",
            int(getattr(mt5, "ORDER_STATE_PARTIAL", -104)): "PARTIAL",
            int(getattr(mt5, "ORDER_STATE_FILLED", -105)): "FILLED",
            int(getattr(mt5, "ORDER_STATE_REJECTED", -106)): "REJECTED",
            int(getattr(mt5, "ORDER_STATE_EXPIRED", -107)): "EXPIRED",
            int(getattr(mt5, "ORDER_STATE_REQUEST_ADD", -108)): "REQUEST_ADD",
            int(getattr(mt5, "ORDER_STATE_REQUEST_MODIFY", -109)): "REQUEST_MODIFY",
            int(getattr(mt5, "ORDER_STATE_REQUEST_CANCEL", -110)): "REQUEST_CANCEL",
        }
        return mapping.get(int(state), f"UNKNOWN:{int(state)}")

    def _broker_order_side(self, order_type: int) -> int:
        buy_types = {
            int(getattr(mt5, "ORDER_TYPE_BUY", -201)),
            int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT", -202)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP", -203)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", -204)),
        }
        sell_types = {
            int(getattr(mt5, "ORDER_TYPE_SELL", -211)),
            int(getattr(mt5, "ORDER_TYPE_SELL_LIMIT", -212)),
            int(getattr(mt5, "ORDER_TYPE_SELL_STOP", -213)),
            int(getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", -214)),
        }
        if int(order_type) in buy_types:
            return 1
        if int(order_type) in sell_types:
            return -1
        return 0

    def _map_broker_order(self, order: Any) -> BrokerWorkingOrder:
        setup_msc = int(getattr(order, "time_setup_msc", 0) or 0)
        if setup_msc <= 0:
            setup_msc = int(getattr(order, "time_setup", 0) or 0) * 1000
        done_msc = int(getattr(order, "time_done_msc", 0) or 0)
        if done_msc <= 0:
            done_msc = int(getattr(order, "time_done", 0) or 0) * 1000
        return BrokerWorkingOrder(
            ticket=int(getattr(order, "ticket", 0) or 0),
            time_setup_msc=setup_msc,
            time_done_msc=done_msc,
            symbol=str(getattr(order, "symbol", "") or ""),
            side=self._broker_order_side(int(getattr(order, "type", -1))),
            volume_initial=float(getattr(order, "volume_initial", 0.0) or 0.0),
            volume_current=float(getattr(order, "volume_current", 0.0) or 0.0),
            price_open=float(getattr(order, "price_open", 0.0) or 0.0),
            stop_loss=float(getattr(order, "sl", 0.0) or 0.0),
            take_profit=float(getattr(order, "tp", 0.0) or 0.0),
            magic=int(getattr(order, "magic", 0) or 0),
            comment=str(getattr(order, "comment", "") or ""),
            state=self._order_state_name(int(getattr(order, "state", -1))),
        )

    def open_orders(self, symbol: str | None = None, magic: int | None = None) -> list[BrokerWorkingOrder]:
        raw = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if raw is None:
            raise RuntimeError(f"orders_get failed: {mt5.last_error()}")
        out: list[BrokerWorkingOrder] = []
        for order in raw:
            item = self._map_broker_order(order)
            if magic is not None and item.magic != magic:
                continue
            out.append(item)
        return sorted(out, key=lambda item: (item.time_setup_msc, item.ticket))

    def history_orders(
        self,
        start: datetime,
        end: datetime | None = None,
        symbol: str | None = None,
        magic: int | None = None,
    ) -> list[BrokerWorkingOrder]:
        finish = end or datetime.now(timezone.utc)
        raw = mt5.history_orders_get(start, finish)
        if raw is None:
            raise RuntimeError(f"history_orders_get failed: {mt5.last_error()}")
        out: list[BrokerWorkingOrder] = []
        for order in raw:
            item = self._map_broker_order(order)
            if symbol is not None and item.symbol != symbol:
                continue
            if magic is not None and item.magic != magic:
                continue
            out.append(item)
        return sorted(out, key=lambda item: (item.time_done_msc or item.time_setup_msc, item.ticket))

    def _order_type(self, side: int) -> int:
        if side == 1:
            return int(mt5.ORDER_TYPE_BUY)
        if side == -1:
            return int(mt5.ORDER_TYPE_SELL)
        raise ValueError("side must be -1 or 1")

    def order_calc_profit(
        self,
        symbol: str,
        side: int,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float:
        if volume <= 0 or open_price <= 0 or close_price <= 0:
            raise ValueError("volume and prices must be positive")
        result = mt5.order_calc_profit(
            self._order_type(side),
            symbol,
            float(volume),
            float(open_price),
            float(close_price),
        )
        if result is None:
            raise RuntimeError(f"MT5 order_calc_profit failed for {symbol}: {mt5.last_error()}")
        return float(result)

    def loss_per_lot_to_stop(self, symbol: str, side: int, entry_price: float, stop_loss: float) -> float:
        pnl = self.order_calc_profit(symbol, side, 1.0, entry_price, stop_loss)
        loss = abs(float(pnl))
        if loss <= 0:
            raise RuntimeError("broker returned zero/non-positive loss to protective stop")
        return loss

    def order_calc_margin(self, symbol: str, side: int, volume: float, price: float) -> float:
        if volume <= 0 or price <= 0:
            raise ValueError("volume and price must be positive")
        result = mt5.order_calc_margin(self._order_type(side), symbol, float(volume), float(price))
        if result is None:
            raise RuntimeError(f"MT5 order_calc_margin failed for {symbol}: {mt5.last_error()}")
        margin = float(result)
        if margin < 0:
            raise RuntimeError("broker returned negative required margin")
        return margin

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
        raise BrokerOrderRejected(f"MT5 order_check rejected all filling modes: {last_error or mt5.last_error()}")

    def _send_checked(self, request: dict[str, Any], preferred_filling: int) -> ExecutionReceipt:
        checked_request = self._checked_request(request, preferred_filling)
        result = mt5.order_send(checked_request)
        if result is None:
            # None does not prove that the broker never received the request.
            # Never resend automatically after this point.
            raise BrokerSubmissionAmbiguous(f"MT5 order_send returned None: {mt5.last_error()}")

        retcode = int(getattr(result, "retcode", -1))
        done = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        partial = int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))
        placed = int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008))
        if retcode == done:
            status = "FILLED"
        elif retcode == partial:
            status = "PARTIAL"
        elif retcode == placed:
            status = "PLACED"
        else:
            raise BrokerOrderRejected(
                f"MT5 order_send failed: retcode={retcode} comment={getattr(result, 'comment', '')}"
            )

        requested_volume = float(checked_request.get("volume", 0.0) or 0.0)
        filled_volume = float(getattr(result, "volume", 0.0) or 0.0)
        # DONE is broker confirmation of completion. Some gateways omit the
        # echoed volume, so only for DONE may the checked request volume be used.
        if status == "FILLED" and filled_volume <= 0:
            filled_volume = requested_volume
        return ExecutionReceipt(
            retcode=retcode,
            status=status,
            order=int(getattr(result, "order", 0) or 0),
            deal=int(getattr(result, "deal", 0) or 0),
            requested_volume=requested_volume,
            filled_volume=filled_volume,
            price=float(getattr(result, "price", 0.0) or 0.0),
            comment=str(getattr(result, "comment", "") or ""),
        )

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
        market_price = tick.ask if is_buy else tick.bid
        price, stop_loss, take_profit = normalize_and_validate_protective_prices(
            spec, order.side, market_price, order.stop_loss, order.take_profit
        )
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(lots),
            "type": order_type,
            "price": float(price),
            "sl": float(stop_loss),
            "tp": float(take_profit),
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
        price = self.symbol_spec(position.symbol).normalize_price(tick.ask if is_buy else tick.bid)
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
