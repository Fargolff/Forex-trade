from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected text not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def insert_before(path: str, marker: str, payload: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if payload.strip() in text:
        return
    if marker not in text:
        raise RuntimeError(f"marker not found in {path}: {marker!r}")
    target.write_text(text.replace(marker, payload + marker, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# MT5 broker primitives: broker-native P/L + margin calculations and broker
# price/stops/freeze constraints.
# ---------------------------------------------------------------------------
replace_once(
    "src/mt5_broker.py",
    """    trade_allowed: bool\n    filling_mode: int\n\n    @property\n    def pip_size(self) -> float:\n""",
    """    trade_allowed: bool\n    filling_mode: int\n    trade_stops_level: int = 0\n    trade_freeze_level: int = 0\n\n    @property\n    def pip_size(self) -> float:\n""",
)
replace_once(
    "src/mt5_broker.py",
    """    def normalize_volume(self, lots: float) -> float:\n        if lots <= 0 or self.volume_step <= 0:\n            return 0.0\n        capped = min(float(lots), self.volume_max)\n        steps = int((capped + 1e-12) / self.volume_step)\n        normalized = steps * self.volume_step\n        if normalized < self.volume_min - 1e-12:\n            return 0.0\n        decimals = max(0, len(str(self.volume_step).split(\".\")[-1].rstrip(\"0\"))) if \".\" in str(self.volume_step) else 0\n        return round(normalized, decimals + 2)\n\n\n@dataclass(frozen=True)\nclass BrokerTick:\n""",
    """    def normalize_volume(self, lots: float) -> float:\n        if lots <= 0 or self.volume_step <= 0:\n            return 0.0\n        capped = min(float(lots), self.volume_max)\n        steps = int((capped + 1e-12) / self.volume_step)\n        normalized = steps * self.volume_step\n        if normalized < self.volume_min - 1e-12:\n            return 0.0\n        decimals = max(0, len(str(self.volume_step).split(\".\")[-1].rstrip(\"0\"))) if \".\" in str(self.volume_step) else 0\n        return round(normalized, decimals + 2)\n\n    def normalize_price(self, price: float) -> float:\n        if price <= 0 or self.tick_size <= 0:\n            raise ValueError(\"price and broker tick_size must be positive\")\n        ticks = round(float(price) / self.tick_size)\n        return round(ticks * self.tick_size, self.digits)\n\n    @property\n    def minimum_protective_distance(self) -> float:\n        # Freeze level is included conservatively. Some brokers apply it mainly\n        # to modifications, but refusing a too-close initial protection level is\n        # safer than assuming it will remain modifiable immediately after fill.\n        return max(int(self.trade_stops_level), int(self.trade_freeze_level), 0) * self.point\n\n\ndef normalize_and_validate_protective_prices(\n    spec: SymbolSpec,\n    side: int,\n    entry_price: float,\n    stop_loss: float,\n    take_profit: float,\n) -> tuple[float, float, float]:\n    if side not in (-1, 1):\n        raise ValueError(\"side must be -1 or 1\")\n    entry = spec.normalize_price(entry_price)\n    stop = spec.normalize_price(stop_loss)\n    take = spec.normalize_price(take_profit)\n    if side > 0 and not (stop < entry < take):\n        raise ValueError(\"buy protection must satisfy stop_loss < entry < take_profit\")\n    if side < 0 and not (take < entry < stop):\n        raise ValueError(\"sell protection must satisfy take_profit < entry < stop_loss\")\n    minimum = spec.minimum_protective_distance\n    if minimum > 0:\n        if abs(entry - stop) + 1e-12 < minimum:\n            raise ValueError(f\"stop-loss distance is below broker minimum {minimum}\")\n        if abs(take - entry) + 1e-12 < minimum:\n            raise ValueError(f\"take-profit distance is below broker minimum {minimum}\")\n    return entry, stop, take\n\n\n@dataclass(frozen=True)\nclass BrokerTick:\n""",
)
replace_once(
    "src/mt5_broker.py",
    """            trade_allowed=trade_mode != disabled_mode,\n            filling_mode=int(getattr(info, \"filling_mode\", 0)),\n        )\n""",
    """            trade_allowed=trade_mode != disabled_mode,\n            filling_mode=int(getattr(info, \"filling_mode\", 0)),\n            trade_stops_level=int(getattr(info, \"trade_stops_level\", 0) or 0),\n            trade_freeze_level=int(getattr(info, \"trade_freeze_level\", 0) or 0),\n        )\n""",
)
replace_once(
    "src/mt5_broker.py",
    """        if tick is None:\n            raise RuntimeError(f\"missing tick for {symbol}: {mt5.last_error()}\")\n        return BrokerTick(bid=float(tick.bid), ask=float(tick.ask), time_msc=int(getattr(tick, \"time_msc\", 0) or 0))\n\n    def account_snapshot(self) -> AccountSnapshot:\n""",
    """        if tick is None:\n            raise RuntimeError(f\"missing tick for {symbol}: {mt5.last_error()}\")\n        bid = float(tick.bid)\n        ask = float(tick.ask)\n        if bid <= 0 or ask <= 0 or ask < bid:\n            raise RuntimeError(f\"invalid broker tick for {symbol}: bid={bid} ask={ask}\")\n        return BrokerTick(bid=bid, ask=ask, time_msc=int(getattr(tick, \"time_msc\", 0) or 0))\n\n    def account_snapshot(self) -> AccountSnapshot:\n""",
)
insert_before(
    "src/mt5_broker.py",
    "    def _filling_candidates(self, preferred: int) -> list[int]:\n",
    """    def _order_type(self, side: int) -> int:\n        if side == 1:\n            return int(mt5.ORDER_TYPE_BUY)\n        if side == -1:\n            return int(mt5.ORDER_TYPE_SELL)\n        raise ValueError(\"side must be -1 or 1\")\n\n    def order_calc_profit(\n        self,\n        symbol: str,\n        side: int,\n        volume: float,\n        open_price: float,\n        close_price: float,\n    ) -> float:\n        if volume <= 0 or open_price <= 0 or close_price <= 0:\n            raise ValueError(\"volume and prices must be positive\")\n        result = mt5.order_calc_profit(\n            self._order_type(side),\n            symbol,\n            float(volume),\n            float(open_price),\n            float(close_price),\n        )\n        if result is None:\n            raise RuntimeError(f\"MT5 order_calc_profit failed for {symbol}: {mt5.last_error()}\")\n        return float(result)\n\n    def loss_per_lot_to_stop(self, symbol: str, side: int, entry_price: float, stop_loss: float) -> float:\n        pnl = self.order_calc_profit(symbol, side, 1.0, entry_price, stop_loss)\n        loss = abs(float(pnl))\n        if loss <= 0:\n            raise RuntimeError(\"broker returned zero/non-positive loss to protective stop\")\n        return loss\n\n    def order_calc_margin(self, symbol: str, side: int, volume: float, price: float) -> float:\n        if volume <= 0 or price <= 0:\n            raise ValueError(\"volume and price must be positive\")\n        result = mt5.order_calc_margin(self._order_type(side), symbol, float(volume), float(price))\n        if result is None:\n            raise RuntimeError(f\"MT5 order_calc_margin failed for {symbol}: {mt5.last_error()}\")\n        margin = float(result)\n        if margin < 0:\n            raise RuntimeError(\"broker returned negative required margin\")\n        return margin\n\n""",
)
replace_once(
    "src/mt5_broker.py",
    """        is_buy = order.side == 1\n        price = tick.ask if is_buy else tick.bid\n        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL\n        request = {\n""",
    """        is_buy = order.side == 1\n        market_price = tick.ask if is_buy else tick.bid\n        price, stop_loss, take_profit = normalize_and_validate_protective_prices(\n            spec, order.side, market_price, order.stop_loss, order.take_profit\n        )\n        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL\n        request = {\n""",
)
replace_once(
    "src/mt5_broker.py",
    """            \"price\": float(price),\n            \"sl\": float(order.stop_loss),\n            \"tp\": float(order.take_profit),\n""",
    """            \"price\": float(price),\n            \"sl\": float(stop_loss),\n            \"tp\": float(take_profit),\n""",
)
replace_once(
    "src/mt5_broker.py",
    """        price = tick.ask if is_buy else tick.bid\n        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL\n""",
    """        price = self.symbol_spec(position.symbol).normalize_price(tick.ask if is_buy else tick.bid)\n        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL\n""",
)

# ---------------------------------------------------------------------------
# Live engine: account allowlist, broker-native loss sizing and projected margin.
# ---------------------------------------------------------------------------
replace_once(
    "src/live.py",
    "from .mt5_broker import BrokerOrder, BrokerPosition, SymbolSpec\n",
    "from .mt5_broker import BrokerOrder, BrokerPosition, SymbolSpec, normalize_and_validate_protective_prices\n",
)
replace_once(
    "src/live.py",
    """    max_spread_pips: float = 2.0\n    max_tick_age_seconds: float = 30.0\n""",
    """    max_spread_pips: float = 2.0\n    allowed_account_logins: tuple[int, ...] = ()\n    require_account_allowlist: bool = False\n    max_margin_fraction_of_equity: float = 0.25\n    min_free_margin_fraction_after_order: float = 0.50\n    max_tick_age_seconds: float = 30.0\n""",
)
insert_before(
    "src/live.py",
    "\ndef _strategy_name(position: BrokerPosition) -> str | None:\n",
    """\ndef broker_risk_sized_lots(\n    broker: Any,\n    symbol: str,\n    equity: float,\n    risk_fraction: float,\n    strategy_weight: float,\n    entry_price: float,\n    stop_loss: float,\n    spec: SymbolSpec,\n    max_lot_per_order: float,\n) -> float:\n    if equity <= 0 or risk_fraction <= 0 or strategy_weight <= 0:\n        return 0.0\n    risk_cash = float(equity) * float(risk_fraction) * float(strategy_weight)\n    loss_per_lot = float(broker.loss_per_lot_to_stop(symbol, 1 if stop_loss < entry_price else -1, entry_price, stop_loss))\n    if loss_per_lot <= 0:\n        return 0.0\n    raw = min(risk_cash / loss_per_lot, float(max_lot_per_order))\n    return spec.normalize_volume(raw)\n\n\ndef margin_safety_report(\n    broker: Any,\n    account: Any,\n    symbol: str,\n    side: int,\n    lots: float,\n    price: float,\n    cfg: LiveEngineConfig,\n) -> dict[str, Any]:\n    required = float(broker.order_calc_margin(symbol, side, lots, price))\n    equity = float(account.equity)\n    projected_margin = float(account.margin) + required\n    projected_free = float(account.margin_free) - required\n    margin_fraction = float(\"inf\") if equity <= 0 else projected_margin / equity\n    free_fraction = float(\"-inf\") if equity <= 0 else projected_free / equity\n    checks = {\n        \"required_margin_nonnegative\": required >= 0,\n        \"projected_margin_within_cap\": margin_fraction <= cfg.max_margin_fraction_of_equity + 1e-12,\n        \"projected_free_margin_positive\": projected_free > 0,\n        \"projected_free_margin_fraction\": free_fraction + 1e-12 >= cfg.min_free_margin_fraction_after_order,\n    }\n    return {\n        \"ok\": all(checks.values()),\n        \"checks\": checks,\n        \"required_margin\": required,\n        \"projected_margin\": projected_margin,\n        \"projected_free_margin\": projected_free,\n        \"projected_margin_fraction\": margin_fraction,\n        \"projected_free_margin_fraction\": free_fraction,\n    }\n\n""",
)
replace_once(
    "src/live.py",
    """    current_spread = spread_pips(tick, spec)\n    total_lots = sum(float(p.volume) for p in managed)\n    checks = {\n""",
    """    current_spread = spread_pips(tick, spec)\n    total_lots = sum(float(p.volume) for p in managed)\n    account_allowed = (not cfg.require_account_allowlist) or (\n        bool(cfg.allowed_account_logins) and int(account.login) in set(cfg.allowed_account_logins)\n    )\n    checks = {\n""",
)
replace_once(
    "src/live.py",
    """        \"hedging_account\": bool(account.hedging),\n        \"symbol_trade_allowed\": bool(spec.trade_allowed),\n""",
    """        \"hedging_account\": bool(account.hedging),\n        \"account_login_allowed\": account_allowed,\n        \"symbol_trade_allowed\": bool(spec.trade_allowed),\n""",
)
old_sizing = """            stop_distance = float(row[\"stop_distance\"])\n            tp_distance = float(row[\"take_profit_distance\"])\n            lots = risk_sized_lots(\n                account.equity,\n                self.config.risk_per_trade,\n                self.weights[strategy],\n                stop_distance,\n                spec,\n                self.config.max_lot_per_order,\n            )\n            if lots <= 0:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=0.0,\n                    price=\"\",\n                    stop_loss=\"\",\n                    take_profit=\"\",\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=\"position_size_zero\",\n                )\n                continue\n            if total_lots + lots > self.config.max_total_lots + 1e-12:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=lots,\n                    price=\"\",\n                    stop_loss=\"\",\n                    take_profit=\"\",\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=\"total_lot_cap\",\n                )\n                continue\n\n            tick = self.broker.current_tick(self.symbol)\n            market_price = tick.ask if side > 0 else tick.bid\n            stop_loss = round(market_price - side * stop_distance, spec.digits)\n            take_profit = round(market_price + side * tp_distance, spec.digits)\n            order = BrokerOrder(\n                symbol=self.symbol,\n                side=side,\n                lots=lots,\n                stop_loss=stop_loss,\n                take_profit=take_profit,\n                magic=self.config.magic,\n                deviation_points=self.config.deviation_points,\n                comment=f\"{COMMENT_PREFIX}{strategy}\"[:31],\n            )\n"""
new_sizing = """            stop_distance = float(row[\"stop_distance\"])\n            tp_distance = float(row[\"take_profit_distance\"])\n            tick = self.broker.current_tick(self.symbol)\n            market_price = tick.ask if side > 0 else tick.bid\n            raw_stop = market_price - side * stop_distance\n            raw_take = market_price + side * tp_distance\n            try:\n                market_price, stop_loss, take_profit = normalize_and_validate_protective_prices(\n                    spec, side, market_price, raw_stop, raw_take\n                )\n                lots = broker_risk_sized_lots(\n                    self.broker,\n                    self.symbol,\n                    account.equity,\n                    self.config.risk_per_trade,\n                    self.weights[strategy],\n                    market_price,\n                    stop_loss,\n                    spec,\n                    self.config.max_lot_per_order,\n                )\n            except Exception as exc:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=0.0,\n                    price=market_price,\n                    stop_loss=raw_stop,\n                    take_profit=raw_take,\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=f\"broker_risk_or_protection:{exc}\",\n                )\n                continue\n            if lots <= 0:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=0.0,\n                    price=market_price,\n                    stop_loss=stop_loss,\n                    take_profit=take_profit,\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=\"position_size_zero\",\n                )\n                continue\n            if total_lots + lots > self.config.max_total_lots + 1e-12:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=lots,\n                    price=market_price,\n                    stop_loss=stop_loss,\n                    take_profit=take_profit,\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=\"total_lot_cap\",\n                )\n                continue\n            try:\n                margin = margin_safety_report(\n                    self.broker, account, self.symbol, side, lots, market_price, self.config\n                )\n            except Exception as exc:\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=lots,\n                    price=market_price,\n                    stop_loss=stop_loss,\n                    take_profit=take_profit,\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=f\"broker_margin_calc_failed:{exc}\",\n                )\n                continue\n            if not margin[\"ok\"]:\n                failed = \",\".join(name for name, passed in margin[\"checks\"].items() if not passed)\n                self._log(\n                    \"REJECT\",\n                    ts,\n                    strategy=strategy,\n                    side=side,\n                    lots=lots,\n                    price=market_price,\n                    stop_loss=stop_loss,\n                    take_profit=take_profit,\n                    spread_pips=current_spread,\n                    ticket=\"\",\n                    reason=f\"margin_gate:{failed};required={margin['required_margin']:.2f}\",\n                )\n                continue\n\n            order = BrokerOrder(\n                symbol=self.symbol,\n                side=side,\n                lots=lots,\n                stop_loss=stop_loss,\n                take_profit=take_profit,\n                magic=self.config.magic,\n                deviation_points=self.config.deviation_points,\n                comment=f\"{COMMENT_PREFIX}{strategy}\"[:31],\n            )\n"""
replace_once("src/live.py", old_sizing, new_sizing)

# ---------------------------------------------------------------------------
# App config and wiring.
# ---------------------------------------------------------------------------
replace_once(
    "src/config.py",
    """    max_open_positions: int = 3\n    max_spread_pips: float = 2.0\n    max_tick_age_seconds: float = 30.0\n""",
    """    max_open_positions: int = 3\n    max_spread_pips: float = 2.0\n    allowed_account_logins: tuple[int, ...] = ()\n    require_account_allowlist: bool = True\n    max_margin_fraction_of_equity: float = 0.25\n    min_free_margin_fraction_after_order: float = 0.50\n    max_tick_age_seconds: float = 30.0\n""",
)
replace_once(
    "src/config.py",
    """def _live(data: dict[str, Any]) -> LiveConfig:\n    cfg = LiveConfig(**(data or {}))\n""",
    """def _live(data: dict[str, Any]) -> LiveConfig:\n    raw = dict(data or {})\n    if \"allowed_account_logins\" in raw:\n        values = raw.get(\"allowed_account_logins\") or []\n        if not isinstance(values, (list, tuple)):\n            raise ValueError(\"live.allowed_account_logins must be a list of MT5 login IDs\")\n        raw[\"allowed_account_logins\"] = tuple(int(value) for value in values)\n    cfg = LiveConfig(**raw)\n""",
)
replace_once(
    "src/config.py",
    """    if cfg.max_spread_pips <= 0:\n        raise ValueError(\"live.max_spread_pips must be positive\")\n""",
    """    if cfg.max_spread_pips <= 0:\n        raise ValueError(\"live.max_spread_pips must be positive\")\n    if any(login <= 0 for login in cfg.allowed_account_logins):\n        raise ValueError(\"live.allowed_account_logins must contain positive login IDs\")\n    if len(set(cfg.allowed_account_logins)) != len(cfg.allowed_account_logins):\n        raise ValueError(\"live.allowed_account_logins cannot contain duplicates\")\n    if cfg.enabled and cfg.require_account_allowlist and not cfg.allowed_account_logins:\n        raise ValueError(\"live.enabled requires at least one allowed_account_logins entry\")\n    if not 0 < cfg.max_margin_fraction_of_equity < 1:\n        raise ValueError(\"live.max_margin_fraction_of_equity must be between 0 and 1\")\n    if not 0 <= cfg.min_free_margin_fraction_after_order < 1:\n        raise ValueError(\"live.min_free_margin_fraction_after_order must be in [0,1)\")\n""",
)
for path in ("src/main.py", "src/production.py"):
    replace_once(
        path,
        """        max_open_positions=cfg.live.max_open_positions,\n        max_spread_pips=cfg.live.max_spread_pips,\n""" if path == "src/main.py" else """        max_open_positions=app_cfg.live.max_open_positions,\n        max_spread_pips=app_cfg.live.max_spread_pips,\n""",
        """        max_open_positions=cfg.live.max_open_positions,\n        max_spread_pips=cfg.live.max_spread_pips,\n        allowed_account_logins=tuple(cfg.live.allowed_account_logins),\n        require_account_allowlist=cfg.live.require_account_allowlist,\n        max_margin_fraction_of_equity=cfg.live.max_margin_fraction_of_equity,\n        min_free_margin_fraction_after_order=cfg.live.min_free_margin_fraction_after_order,\n""" if path == "src/main.py" else """        max_open_positions=app_cfg.live.max_open_positions,\n        max_spread_pips=app_cfg.live.max_spread_pips,\n        allowed_account_logins=tuple(app_cfg.live.allowed_account_logins),\n        require_account_allowlist=app_cfg.live.require_account_allowlist,\n        max_margin_fraction_of_equity=app_cfg.live.max_margin_fraction_of_equity,\n        min_free_margin_fraction_after_order=app_cfg.live.min_free_margin_fraction_after_order,\n""",
    )
replace_once(
    "src/production.py",
    """    incidents: list[dict[str, str]] = [\n        {\"severity\": item.severity, \"code\": item.code, \"detail\": item.detail} for item in ops[\"incidents\"]\n    ]\n    if future_seconds > prod_cfg.max_future_tick_seconds:\n""",
    """    incidents: list[dict[str, str]] = [\n        {\"severity\": item.severity, \"code\": item.code, \"detail\": item.detail} for item in ops[\"incidents\"]\n    ]\n    if live_cfg.require_account_allowlist and (\n        not live_cfg.allowed_account_logins or int(ops[\"account\"].login) not in set(live_cfg.allowed_account_logins)\n    ):\n        incidents.append(\n            {\n                \"severity\": \"CRITICAL\",\n                \"code\": \"ACCOUNT_LOGIN_NOT_ALLOWED\",\n                \"detail\": f\"MT5 login {ops['account'].login} is not in the configured live account allowlist\",\n            }\n        )\n    if future_seconds > prod_cfg.max_future_tick_seconds:\n""",
)
replace_once(
    "config.example.yaml",
    """  max_open_positions: 3\n  max_spread_pips: 2.0\n\n  # Phase 8 production-health gates.\n""",
    """  max_open_positions: 3\n  max_spread_pips: 2.0\n\n  # Phase 15 broker/account safety. When live.enabled=true, configure the exact\n  # MT5 login IDs that are allowed to receive orders. An empty allowlist fails.\n  allowed_account_logins: []\n  require_account_allowlist: true\n  # Projected margin after each new order must remain below 25% of equity, and\n  # projected free margin must remain at least 50% of equity.\n  max_margin_fraction_of_equity: 0.25\n  min_free_margin_fraction_after_order: 0.50\n\n  # Phase 8 production-health gates.\n""",
)

# README: document Phases 14-15 if not already present.
readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
if "### Phase 15 — Broker-Accurate Position Sizing & Margin Safety ✅" not in text:
    phase_text = """
### Phase 14 — Broker State Disaster Recovery & Restart Reconciliation ✅

- Read-only restart gate compares broker positions/deals with local live state and audit events
- Atomic restart report/checkpoint and account/symbol/magic continuity checks
- Same-millisecond broker deal ordering uses `(time_msc, ticket)`
- Ambiguous restart state fails closed and never auto-repairs broker positions

See `docs/phase14-restart-reconciliation.md` for the restart/recovery runbook.

### Phase 15 — Broker-Accurate Position Sizing & Margin Safety ✅

- Live risk sizing uses MT5 `order_calc_profit` for the actual symbol/account currency path
- No tick-value approximation is used for new live position sizing
- MT5 `order_calc_margin` gates every new order before `order_send`
- Account-login allowlist is mandatory whenever `live.enabled=true` by default
- Projected total-margin and free-margin fractions are capped before entry
- Broker tick-size price normalization is applied to entry/SL/TP prices
- Broker stops/freeze levels are enforced conservatively before order submission
- Invalid/non-positive/crossed bid/ask ticks fail closed
- Production health emits `ACCOUNT_LOGIN_NOT_ALLOWED` as CRITICAL

See `docs/phase15-broker-safety.md` for account pinning, margin policy and broker-specific rollout steps.

"""
    text = text.replace("## Quick start\n", phase_text + "## Quick start\n", 1)
    text = text.replace(
        "- Phase 13 never prunes a local backup unless a byte-identical verified replica is still reachable when pruning is attempted.\n",
        "- Phase 13 never prunes a local backup unless a byte-identical verified replica is still reachable when pruning is attempted.\n- Phase 14 blocks live restarts when broker/local position history cannot be reconciled.\n- Phase 15 requires broker-native live risk/margin calculations and can pin execution to explicit MT5 account logins.\n",
        1,
    )
    text = text.replace("copy backup.example.yaml backup.yaml\n", "copy backup.example.yaml backup.yaml\ncopy reconcile.example.yaml reconcile.yaml\n", 1)
    readme.write_text(text, encoding="utf-8")

print("Phase 15 patch applied")
