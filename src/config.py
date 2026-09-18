from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "ema_trend"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False
    magic: int = 56001
    deviation_points: int = 20
    risk_per_trade: float = 0.0025
    max_daily_loss_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_lot_per_order: float = 0.02
    max_total_lots: float = 0.05
    max_open_positions: int = 3
    max_spread_pips: float = 2.0
    allowed_account_logins: tuple[int, ...] = ()
    require_account_allowlist: bool = True
    max_margin_fraction_of_equity: float = 0.25
    min_free_margin_fraction_after_order: float = 0.50
    max_tick_age_seconds: float = 30.0
    # 0 = derive automatically from the configured timeframe in the CLI.
    max_bar_age_seconds: float = 0.0
    market_session_enabled: bool = True
    market_sunday_open_utc: str = "22:00"
    market_friday_close_utc: str = "22:00"
    market_transition_grace_seconds: float = 3600.0
    max_future_tick_seconds: float = 5.0
    max_future_bar_seconds: float = 300.0
    deal_reconcile_lookback_hours: float = 72.0
    max_reconnect_attempts: int = 3
    reconnect_backoff_seconds: float = 5.0
    state_path: str = "runtime/live_state.json"
    events_path: str = "runtime/live_events.csv"
    heartbeat_path: str = "runtime/live_heartbeat.json"
    incidents_path: str = "runtime/live_incidents.csv"
    weights_path: str = "results/portfolio/portfolio_weights.csv"
    candidates_path: str = "results/portfolio/portfolio_candidates.csv"
    poll_seconds: int = 30
    history_bars: int = 5000


@dataclass(frozen=True)
class PaperRuntimeConfig:
    state_path: str = "runtime/paper_state.json"
    events_path: str = "runtime/paper_events.csv"
    weights_path: str = "results/portfolio/portfolio_weights.csv"
    candidates_path: str = "results/portfolio/portfolio_candidates.csv"
    poll_seconds: int = 30
    history_bars: int = 5000


@dataclass(frozen=True)
class AppConfig:
    mode: str = "backtest"
    symbol: str = "EURUSD"
    timeframe: str = "H1"
    data_dir: str = "data"
    initial_equity: float = 10_000.0
    risk_per_trade: float = 0.005
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    spread_pips: float = 0.8
    slippage_pips: float = 0.2
    commission_per_lot_round_turn: float = 7.0
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    paper: PaperRuntimeConfig = field(default_factory=PaperRuntimeConfig)
    live: LiveConfig = field(default_factory=LiveConfig)


def _strategy(data: dict[str, Any]) -> StrategyConfig:
    raw = dict(data or {})
    name = str(raw.pop("name", "ema_trend"))
    explicit_params = raw.pop("params", {}) or {}
    if not isinstance(explicit_params, dict):
        raise ValueError("strategy.params must be a mapping")

    # Backward compatible with Phase-1 YAML where fast/slow/etc. lived directly
    # under `strategy:`. Explicit `params:` wins on duplicate keys.
    params = {**raw, **explicit_params}
    return StrategyConfig(name=name, params=params)


def _paper(data: dict[str, Any]) -> PaperRuntimeConfig:
    cfg = PaperRuntimeConfig(**(data or {}))
    if cfg.poll_seconds < 1:
        raise ValueError("paper.poll_seconds must be >= 1")
    if cfg.history_bars < 100:
        raise ValueError("paper.history_bars must be >= 100")
    return cfg


def _valid_hhmm(value: str) -> bool:
    parts = str(value).split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return False
    hour, minute = (int(part) for part in parts)
    return 0 <= hour <= 23 and 0 <= minute <= 59 and len(parts[0]) == 2 and len(parts[1]) == 2


def _live(data: dict[str, Any]) -> LiveConfig:
    raw = dict(data or {})
    if "allowed_account_logins" in raw:
        values = raw.get("allowed_account_logins") or []
        if not isinstance(values, (list, tuple)):
            raise ValueError("live.allowed_account_logins must be a list of MT5 login IDs")
        raw["allowed_account_logins"] = tuple(int(value) for value in values)
    cfg = LiveConfig(**raw)
    if cfg.poll_seconds < 1:
        raise ValueError("live.poll_seconds must be >= 1")
    if cfg.history_bars < 100:
        raise ValueError("live.history_bars must be >= 100")
    if not 0 < cfg.risk_per_trade < 1:
        raise ValueError("live.risk_per_trade must be between 0 and 1")
    if not 0 < cfg.max_daily_loss_pct < 1:
        raise ValueError("live.max_daily_loss_pct must be between 0 and 1")
    if not 0 < cfg.max_drawdown_pct < 1:
        raise ValueError("live.max_drawdown_pct must be between 0 and 1")
    if cfg.max_lot_per_order <= 0 or cfg.max_total_lots <= 0:
        raise ValueError("live lot caps must be positive")
    if cfg.max_lot_per_order > cfg.max_total_lots:
        raise ValueError("live.max_lot_per_order cannot exceed live.max_total_lots")
    if cfg.max_open_positions < 1:
        raise ValueError("live.max_open_positions must be >= 1")
    if cfg.max_spread_pips <= 0:
        raise ValueError("live.max_spread_pips must be positive")
    if any(login <= 0 for login in cfg.allowed_account_logins):
        raise ValueError("live.allowed_account_logins must contain positive login IDs")
    if len(set(cfg.allowed_account_logins)) != len(cfg.allowed_account_logins):
        raise ValueError("live.allowed_account_logins cannot contain duplicates")
    if cfg.enabled and cfg.require_account_allowlist and not cfg.allowed_account_logins:
        raise ValueError("live.enabled requires at least one allowed_account_logins entry")
    if not 0 < cfg.max_margin_fraction_of_equity < 1:
        raise ValueError("live.max_margin_fraction_of_equity must be between 0 and 1")
    if not 0 <= cfg.min_free_margin_fraction_after_order < 1:
        raise ValueError("live.min_free_margin_fraction_after_order must be in [0,1)")
    if cfg.max_tick_age_seconds <= 0:
        raise ValueError("live.max_tick_age_seconds must be positive")
    if cfg.max_bar_age_seconds < 0:
        raise ValueError("live.max_bar_age_seconds cannot be negative")
    if not _valid_hhmm(cfg.market_sunday_open_utc):
        raise ValueError("live.market_sunday_open_utc must be HH:MM UTC")
    if not _valid_hhmm(cfg.market_friday_close_utc):
        raise ValueError("live.market_friday_close_utc must be HH:MM UTC")
    if cfg.market_transition_grace_seconds < 0:
        raise ValueError("live.market_transition_grace_seconds cannot be negative")
    if cfg.max_future_tick_seconds < 0 or cfg.max_future_bar_seconds < 0:
        raise ValueError("live future timestamp tolerances cannot be negative")
    if cfg.deal_reconcile_lookback_hours <= 0:
        raise ValueError("live.deal_reconcile_lookback_hours must be positive")
    if cfg.max_reconnect_attempts < 0:
        raise ValueError("live.max_reconnect_attempts cannot be negative")
    if cfg.reconnect_backoff_seconds < 0:
        raise ValueError("live.reconnect_backoff_seconds cannot be negative")
    return cfg


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("top-level YAML config must be a mapping")
    raw = dict(raw)
    strategy = _strategy(raw.pop("strategy", {}))
    paper = _paper(raw.pop("paper", {}))
    live = _live(raw.pop("live", {}))
    return AppConfig(strategy=strategy, paper=paper, live=live, **raw)
