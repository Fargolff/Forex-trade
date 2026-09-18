from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import time

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .config import load_config
from .data import MarketDataStore, normalize_ohlc
from .live import ARM_PHRASE, LiveEngineConfig, LiveEventLog, LiveTradingEngine, preflight_report, require_live_arming
from .mt5_broker import MT5Broker
from .ops import operational_report
from .paper import PaperConfig, PaperTradingEngine, load_portfolio_bundle
from .portfolio import PortfolioConfig, research_portfolio, save_portfolio_report
from .research import run_strategy_batch, save_research_results
from .strategy import available_strategies, build_signals, strategy_spec
from .validation import ValidationConfig, validate_strategy_batch


def _synthetic_data(rows: int = 3000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")

    # Regime-changing toy series: alternating drift and volatility make it harder
    # for a single strategy family to dominate every segment.
    segment = np.arange(rows) // max(rows // 6, 1)
    drift = np.where(segment % 3 == 0, 0.000015, np.where(segment % 3 == 1, -0.000010, 0.0))
    volatility = np.where(segment % 2 == 0, 0.00045, 0.00080)
    noise = rng.normal(0.0, volatility, rows)
    close = 1.10 + np.cumsum(drift + noise)
    open_ = np.r_[close[0], close[:-1]]
    upper_wick = rng.uniform(0.0001, 0.0007, rows)
    lower_wick = rng.uniform(0.0001, 0.0007, rows)
    high = np.maximum(open_, close) + upper_wick
    low = np.minimum(open_, close) - lower_wick
    return normalize_ohlc(pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx))


def _periods_per_year(timeframe: str) -> float:
    mapping = {
        "M1": 252.0 * 24.0 * 60.0,
        "M5": 252.0 * 24.0 * 12.0,
        "M15": 252.0 * 24.0 * 4.0,
        "M30": 252.0 * 24.0 * 2.0,
        "H1": 252.0 * 24.0,
        "H4": 252.0 * 6.0,
        "D1": 252.0,
    }
    return mapping.get(timeframe.upper(), 252.0 * 24.0)


def _timeframe_seconds(timeframe: str) -> float:
    mapping = {
        "M1": 60.0,
        "M5": 5.0 * 60.0,
        "M15": 15.0 * 60.0,
        "M30": 30.0 * 60.0,
        "H1": 60.0 * 60.0,
        "H4": 4.0 * 60.0 * 60.0,
        "D1": 24.0 * 60.0 * 60.0,
    }
    return mapping.get(timeframe.upper(), 60.0 * 60.0)


def _backtest_config(cfg) -> BacktestConfig:
    return BacktestConfig(
        initial_equity=cfg.initial_equity,
        risk_per_trade=cfg.risk_per_trade,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
        spread_pips=cfg.spread_pips,
        slippage_pips=cfg.slippage_pips,
        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,
        periods_per_year=_periods_per_year(cfg.timeframe),
    )


def _paper_config(cfg, args) -> PaperConfig:
    return PaperConfig(
        initial_equity=cfg.initial_equity,
        risk_per_trade=cfg.risk_per_trade,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
        spread_pips=cfg.spread_pips,
        slippage_pips=cfg.slippage_pips,
        commission_per_lot_round_turn=cfg.commission_per_lot_round_turn,
        state_path=args.paper_state or cfg.paper.state_path,
        events_path=args.paper_events or cfg.paper.events_path,
    )


def _live_config(cfg, args) -> LiveEngineConfig:
    max_bar_age = cfg.live.max_bar_age_seconds or (_timeframe_seconds(cfg.timeframe) * 2.5)
    return LiveEngineConfig(
        risk_per_trade=cfg.live.risk_per_trade,
        max_daily_loss_pct=cfg.live.max_daily_loss_pct,
        max_drawdown_pct=cfg.live.max_drawdown_pct,
        max_lot_per_order=cfg.live.max_lot_per_order,
        max_total_lots=cfg.live.max_total_lots,
        max_open_positions=cfg.live.max_open_positions,
        max_spread_pips=cfg.live.max_spread_pips,
        allowed_account_logins=tuple(cfg.live.allowed_account_logins),
        require_account_allowlist=cfg.live.require_account_allowlist,
        max_margin_fraction_of_equity=cfg.live.max_margin_fraction_of_equity,
        min_free_margin_fraction_after_order=cfg.live.min_free_margin_fraction_after_order,
        max_tick_age_seconds=cfg.live.max_tick_age_seconds,
        max_bar_age_seconds=max_bar_age,
        market_session_enabled=cfg.live.market_session_enabled,
        market_sunday_open_utc=cfg.live.market_sunday_open_utc,
        market_friday_close_utc=cfg.live.market_friday_close_utc,
        market_transition_grace_seconds=cfg.live.market_transition_grace_seconds,
        max_future_tick_seconds=cfg.live.max_future_tick_seconds,
        max_future_bar_seconds=cfg.live.max_future_bar_seconds,
        deal_reconcile_lookback_hours=cfg.live.deal_reconcile_lookback_hours,
        magic=cfg.live.magic,
        deviation_points=cfg.live.deviation_points,
        state_path=args.live_state or cfg.live.state_path,
        events_path=args.live_events or cfg.live.events_path,
        heartbeat_path=args.live_heartbeat or cfg.live.heartbeat_path,
        incidents_path=args.live_incidents or cfg.live.incidents_path,
    )


def _mt5_data(cfg, bars: int) -> pd.DataFrame:
    broker = MT5Broker()
    broker.connect()
    try:
        return normalize_ohlc(broker.rates(cfg.symbol, cfg.timeframe, bars=bars))
    finally:
        broker.close()


def _completed_from_broker(broker: MT5Broker, cfg, bars: int) -> pd.DataFrame:
    raw = normalize_ohlc(broker.rates(cfg.symbol, cfg.timeframe, bars=bars))
    if len(raw) < 2:
        raise RuntimeError("MT5 returned too few bars to isolate the last completed bar")
    # copy_rates_from_pos includes the currently-forming bar at position 0.
    return raw.iloc[:-1].copy()


def _mt5_completed_data(cfg, bars: int) -> pd.DataFrame:
    broker = MT5Broker()
    broker.connect()
    try:
        return _completed_from_broker(broker, cfg, bars)
    finally:
        broker.close()


def _selected_strategy_names(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return available_strategies()
    names = [x.strip() for x in value.split(",") if x.strip()]
    unknown = sorted(set(names).difference(available_strategies()))
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}; available={available_strategies()}")
    return names


def _validation_config(args) -> ValidationConfig:
    return ValidationConfig(
        walk_forward_train_bars=args.wf_train_bars,
        walk_forward_test_bars=args.wf_test_bars,
        walk_forward_step_bars=args.wf_step_bars,
        parameter_perturbation=args.param_perturbation,
        monte_carlo_runs=args.mc_runs,
        seed=args.seed,
    )


def _paper_demo_bundle(args) -> tuple[dict[str, dict], dict[str, float]]:
    names = _selected_strategy_names(args.strategies)
    strategies = {name: dict(strategy_spec(name).defaults) for name in names}
    weights = {name: 1.0 / len(names) for name in names}
    return strategies, weights


def _paper_portfolio_bundle(cfg, args) -> tuple[dict[str, dict], dict[str, float]]:
    weights_path = args.paper_weights or cfg.paper.weights_path
    candidates_path = args.paper_candidates or cfg.paper.candidates_path
    return load_portfolio_bundle(weights_path, candidates_path)


def _live_portfolio_bundle(cfg, args) -> tuple[dict[str, dict], dict[str, float]]:
    weights_path = args.live_weights or cfg.live.weights_path
    candidates_path = args.live_candidates or cfg.live.candidates_path
    return load_portfolio_bundle(weights_path, candidates_path)


def _print_paper_snapshot(snapshot: dict) -> None:
    print("\n=== PAPER TRADING SNAPSHOT ===")
    print(f"Symbol          : {snapshot['symbol']}")
    print(f"Balance         : {snapshot['balance']:.2f}")
    print(f"Equity          : {snapshot['equity']:.2f}")
    print(f"Peak equity     : {snapshot['peak_equity']:.2f}")
    print(f"Open positions  : {snapshot['open_positions']}")
    print(f"Pending signals : {snapshot['pending_signals']}")
    print(f"Last bar        : {snapshot['last_bar_time']}")
    print(f"Halted          : {snapshot['halted']}")
    if snapshot["halt_reason"]:
        print(f"Halt reason     : {snapshot['halt_reason']}")


def _print_live_snapshot(snapshot: dict) -> None:
    print("\n=== GUARDED LIVE SNAPSHOT ===")
    print(f"Symbol             : {snapshot['symbol']}")
    print(f"Balance            : {snapshot['balance']:.2f}")
    print(f"Equity             : {snapshot['equity']:.2f}")
    print(f"Free margin        : {snapshot['free_margin']:.2f}")
    print(f"Peak equity        : {snapshot['peak_equity']:.2f}")
    print(f"Managed positions  : {snapshot['managed_positions']}")
    print(f"Managed total lots : {snapshot['managed_total_lots']:.4f}")
    print(f"Last bar           : {snapshot['last_bar_time']}")
    print(f"Last deal msec     : {snapshot['last_deal_time_msc']}")
    print(f"Halted             : {snapshot['halted']}")
    if snapshot["halt_reason"]:
        print(f"Halt reason        : {snapshot['halt_reason']}")
    print(f"Heartbeat          : {snapshot['heartbeat_path']}")
    print(f"Incidents          : {snapshot['incidents_path']}")


def _print_operational_report(report: dict) -> None:
    print("\n=== LIVE OPERATIONAL HEALTH ===")
    print(f"Status                 : {report['status']}")
    print(f"Terminal connected     : {report['terminal'].connected}")
    print(f"Terminal trade allowed : {report['terminal'].trade_allowed}")
    print(f"Spread                 : {report['spread_pips']:.2f} pips")
    print(f"Tick age               : {report['tick_age_seconds']:.1f}s")
    print(f"Completed-bar age      : {report['bar_age_seconds']:.1f}s")
    print(f"Market state           : {report.get('market_state', 'UNKNOWN')}")
    print(f"Market reason          : {report.get('market_reason', '')}")
    print(f"Next transition UTC    : {report.get('next_market_transition_utc')}")
    print(f"Tick clock offset      : {report.get('tick_clock_offset_seconds')}s")
    print(f"Bar clock offset       : {report.get('bar_clock_offset_seconds')}s")
    print(f"Managed positions      : {len(report['positions'])}")
    print(f"Account equity         : {report['account'].equity:.2f} {report['account'].currency}")
    if report["incidents"]:
        print("Incidents:")
        for item in report["incidents"]:
            print(f"  [{item.severity}] {item.code}: {item.detail}")
    else:
        print("Incidents              : none")


def _run_paper_mt5_once(cfg, args, engine: PaperTradingEngine) -> dict:
    history_bars = args.paper_history_bars or cfg.paper.history_bars
    completed = _mt5_completed_data(cfg, history_bars)
    return engine.process(completed)


def _run_live_once(cfg, args, broker: MT5Broker, engine: LiveTradingEngine) -> dict:
    history_bars = args.live_history_bars or cfg.live.history_bars
    completed = _completed_from_broker(broker, cfg, history_bars)
    return engine.process_latest(completed)


def _log_runtime_event(event_log: LiveEventLog, event: str, reason: str) -> None:
    event_log.append(
        time=datetime.now(timezone.utc).isoformat(),
        event=event,
        strategy="PORTFOLIO",
        side="",
        lots="",
        price="",
        stop_loss="",
        take_profit="",
        spread_pips="",
        ticket="",
        reason=reason,
    )


def _reconnect_broker(broker: MT5Broker, cfg, event_log: LiveEventLog) -> bool:
    attempts = cfg.live.max_reconnect_attempts
    for attempt in range(1, attempts + 1):
        if cfg.live.reconnect_backoff_seconds > 0:
            time.sleep(cfg.live.reconnect_backoff_seconds)
        try:
            broker.reconnect()
            terminal = broker.terminal_snapshot()
            if not terminal.connected:
                raise RuntimeError("terminal still reports connected=false after reconnect")
            _log_runtime_event(event_log, "RECONNECT", f"attempt={attempt};status=success")
            return True
        except Exception as exc:
            _log_runtime_event(event_log, "RECONNECT", f"attempt={attempt};status=failed;error={exc}")
    return False


def print_report(result: dict, strategy_name: str) -> None:
    print(f"\n=== BACKTEST SUMMARY: {strategy_name} ===")
    print(f"Initial equity : {result['initial_equity']:.2f}")
    print(f"Final equity   : {result['final_equity']:.2f}")
    print(f"Net profit     : {result['net_profit']:.2f}")
    print(f"Return         : {result['return_pct'] * 100:.2f}%")
    print(f"Max drawdown   : {result['max_drawdown_pct'] * 100:.2f}%")
    print(f"Sharpe approx. : {result['sharpe_approx']:.2f}")
    print(f"Trades         : {result['trades']}")
    print(f"Win rate       : {result['win_rate'] * 100:.2f}%")
    print(f"Profit factor  : {result['profit_factor']:.2f}")


def print_catalog() -> None:
    print("\n=== STRATEGY CATALOG ===")
    for name in available_strategies():
        spec = strategy_spec(name)
        print(f"{name:24s} [{spec.family:15s}] {spec.description}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Forex auto-trading research framework")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--mode",
        choices=[
            "demo-backtest",
            "mt5-backtest",
            "batch-demo",
            "batch-mt5",
            "validate-demo",
            "validate-mt5",
            "portfolio-demo",
            "portfolio-mt5",
            "paper-demo",
            "paper-mt5-once",
            "paper-mt5-daemon",
            "live-preflight",
            "live-health",
            "live-mt5-once",
            "live-mt5-daemon",
            "live-flatten",
            "cache-mt5",
            "list-strategies",
        ],
        default="demo-backtest",
        help="Real orders are available only through explicitly armed guarded-live modes.",
    )
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--strategy", default=None, help="Override config strategy for a single backtest.")
    parser.add_argument("--strategies", default="all", help="Comma-separated names or 'all' for batch/validation/portfolio/demo-paper modes.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--save-data", action="store_true", help="Cache loaded bars under data_dir.")
    parser.add_argument("--output", default="results/strategy_leaderboard.csv")
    parser.add_argument("--validation-output", default="results/robust_validation.csv")
    parser.add_argument("--portfolio-output-dir", default="results/portfolio")
    parser.add_argument("--portfolio-verdicts", default="PASS,WATCH", help="Validation verdicts eligible for allocation.")
    parser.add_argument("--portfolio-min-strategies", type=int, default=2)
    parser.add_argument("--max-strategy-weight", type=float, default=0.35)
    parser.add_argument("--portfolio-block-size", type=int, default=24)
    parser.add_argument("--mc-runs", type=int, default=1000)
    parser.add_argument("--wf-train-bars", type=int, default=2000)
    parser.add_argument("--wf-test-bars", type=int, default=500)
    parser.add_argument("--wf-step-bars", type=int, default=500)
    parser.add_argument("--param-perturbation", type=float, default=0.20)
    parser.add_argument("--paper-state", default=None, help="Override persistent paper-state JSON path.")
    parser.add_argument("--paper-events", default=None, help="Override paper event-log CSV path.")
    parser.add_argument("--paper-weights", default=None, help="Override Phase-5 portfolio weights CSV path.")
    parser.add_argument("--paper-candidates", default=None, help="Override Phase-5 portfolio candidates CSV path.")
    parser.add_argument("--paper-poll-seconds", type=int, default=None, help="MT5 daemon polling interval.")
    parser.add_argument("--paper-history-bars", type=int, default=None, help="Bars fetched each paper polling cycle.")
    parser.add_argument("--paper-max-cycles", type=int, default=0, help="Daemon cycles; 0 means run until Ctrl+C.")
    parser.add_argument("--live-state", default=None, help="Override persistent live-state JSON path.")
    parser.add_argument("--live-events", default=None, help="Override live event-log CSV path.")
    parser.add_argument("--live-heartbeat", default=None, help="Override live heartbeat JSON path.")
    parser.add_argument("--live-incidents", default=None, help="Override live incident-log CSV path.")
    parser.add_argument("--live-weights", default=None, help="Override Phase-5 portfolio weights CSV path for live mode.")
    parser.add_argument("--live-candidates", default=None, help="Override Phase-5 portfolio candidates CSV path for live mode.")
    parser.add_argument("--live-poll-seconds", type=int, default=None, help="Guarded live daemon polling interval.")
    parser.add_argument("--live-history-bars", type=int, default=None, help="Bars fetched each guarded live cycle.")
    parser.add_argument("--live-max-cycles", type=int, default=0, help="Live daemon cycles; 0 means run until Ctrl+C.")
    parser.add_argument("--arm-live", default=None, help=f"Required for real-order modes; exact value: {ARM_PHRASE}")
    args = parser.parse_args()

    if args.mode == "list-strategies":
        print_catalog()
        return

    config_path = Path(args.config)
    if not config_path.exists():
        fallback = Path("config.example.yaml")
        if not fallback.exists():
            raise FileNotFoundError("config.yaml or config.example.yaml is required")
        config_path = fallback

    cfg = load_config(config_path)
    store = MarketDataStore(cfg.data_dir)

    if args.mode in {"live-preflight", "live-health"}:
        strategies, _ = _live_portfolio_bundle(cfg, args)
        broker = MT5Broker()
        broker.connect()
        try:
            live_cfg = _live_config(cfg, args)
            if args.mode == "live-preflight":
                report = preflight_report(broker, cfg.symbol, live_cfg, cfg.live.enabled)
                print("\n=== LIVE PREFLIGHT ===")
                for name, passed in report["checks"].items():
                    print(f"{name:30s}: {'PASS' if passed else 'FAIL'}")
                print(f"Spread                 : {report['spread_pips']:.2f} pips")
                print(f"Managed positions       : {len(report['managed_positions'])}")
                print(f"Managed total lots      : {report['managed_total_lots']:.4f}")
                print(f"Account equity          : {report['account'].equity:.2f} {report['account'].currency}")
                print(f"Hedging account         : {report['account'].hedging}")
                print(f"Terminal connected      : {report['terminal'].connected}")
                print(f"Overall                 : {'PASS' if report['ok'] else 'FAIL'}")
                return

            history_bars = args.live_history_bars or cfg.live.history_bars
            completed = _completed_from_broker(broker, cfg, history_bars)
            report = operational_report(
                broker,
                cfg.symbol,
                live_cfg.magic,
                set(strategies),
                completed.index[-1],
                max_tick_age_seconds=live_cfg.max_tick_age_seconds,
                max_bar_age_seconds=live_cfg.max_bar_age_seconds,
                market_session_enabled=live_cfg.market_session_enabled,
                market_sunday_open_utc=live_cfg.market_sunday_open_utc,
                market_friday_close_utc=live_cfg.market_friday_close_utc,
                market_transition_grace_seconds=live_cfg.market_transition_grace_seconds,
                max_future_tick_seconds=live_cfg.max_future_tick_seconds,
                max_future_bar_seconds=live_cfg.max_future_bar_seconds,
            )
            _print_operational_report(report)
        finally:
            broker.close()
        return

    if args.mode in {"live-mt5-once", "live-mt5-daemon", "live-flatten"}:
        broker = MT5Broker()
        broker.connect()
        try:
            live_cfg = _live_config(cfg, args)
            require_live_arming(cfg.live.enabled, args.arm_live)

            if args.mode == "live-flatten":
                event_log = LiveEventLog(live_cfg.events_path)
                positions = broker.open_positions(symbol=cfg.symbol, magic=live_cfg.magic)
                closed = 0
                for position in positions:
                    broker.close_position(
                        position,
                        deviation_points=live_cfg.deviation_points,
                        live_enabled=cfg.live.enabled,
                    )
                    closed += 1
                    event_log.append(
                        time=datetime.now(timezone.utc).isoformat(),
                        event="FLATTEN",
                        strategy="",
                        side=position.side,
                        lots=position.volume,
                        price="",
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                        spread_pips="",
                        ticket=position.ticket,
                        reason="operator_flatten",
                    )
                print(f"Emergency flatten completed for {closed} managed position(s).")
                return

            strategies, weights = _live_portfolio_bundle(cfg, args)
            engine = LiveTradingEngine(
                broker,
                cfg.symbol,
                strategies,
                weights,
                live_cfg,
                cfg.live.enabled,
                args.arm_live,
            )
            if args.mode == "live-mt5-once":
                snapshot = _run_live_once(cfg, args, broker, engine)
                _print_live_snapshot(snapshot)
                print(f"Live state -> {engine.config.state_path}")
                print(f"Live events -> {engine.config.events_path}")
                return

            poll_seconds = args.live_poll_seconds or cfg.live.poll_seconds
            if poll_seconds < 1:
                raise ValueError("live polling interval must be >= 1 second")
            if args.live_max_cycles < 0:
                raise ValueError("live-max-cycles must be >= 0")
            print("Starting GUARDED MT5 live daemon. Real orders can be sent under configured caps.")
            cycles = 0
            event_log = LiveEventLog(live_cfg.events_path)
            try:
                while args.live_max_cycles == 0 or cycles < args.live_max_cycles:
                    cycles += 1
                    try:
                        snapshot = _run_live_once(cfg, args, broker, engine)
                    except Exception as exc:
                        _log_runtime_event(event_log, "RUNTIME_ERROR", f"cycle={cycles};error={exc}")
                        print(f"Live cycle error: {exc}")
                        if not _reconnect_broker(broker, cfg, event_log):
                            raise RuntimeError("MT5 reconnect attempts exhausted; live daemon stopped") from exc
                        print("MT5 connection recovered; next cycle will re-read broker state before trading.")
                        if args.live_max_cycles and cycles >= args.live_max_cycles:
                            break
                        continue

                    _print_live_snapshot(snapshot)
                    if snapshot["halted"]:
                        print("Live portfolio is halted; daemon stopped for operator review.")
                        break
                    if args.live_max_cycles and cycles >= args.live_max_cycles:
                        break
                    time.sleep(poll_seconds)
            except KeyboardInterrupt:
                print("Live daemon stopped by operator. Existing broker positions are NOT automatically flattened.")
            return
        finally:
            broker.close()

    if args.mode == "paper-demo":
        strategies, weights = _paper_demo_bundle(args)
        engine = PaperTradingEngine(cfg.symbol, strategies, weights, _paper_config(cfg, args))
        snapshot = engine.process(_synthetic_data(args.bars, seed=args.seed))
        _print_paper_snapshot(snapshot)
        print(f"Paper state -> {engine.config.state_path}")
        print(f"Paper events -> {engine.config.events_path}")
        print("Synthetic paper mode never sends broker orders.")
        return

    if args.mode in {"paper-mt5-once", "paper-mt5-daemon"}:
        strategies, weights = _paper_portfolio_bundle(cfg, args)
        engine = PaperTradingEngine(cfg.symbol, strategies, weights, _paper_config(cfg, args))
        if args.mode == "paper-mt5-once":
            snapshot = _run_paper_mt5_once(cfg, args, engine)
            _print_paper_snapshot(snapshot)
            print(f"Paper state -> {engine.config.state_path}")
            print(f"Paper events -> {engine.config.events_path}")
            print("MT5 was used for market data only; no order_send call is made by paper mode.")
            return

        poll_seconds = args.paper_poll_seconds or cfg.paper.poll_seconds
        if poll_seconds < 1:
            raise ValueError("paper polling interval must be >= 1 second")
        if args.paper_max_cycles < 0:
            raise ValueError("paper-max-cycles must be >= 0")
        print("Starting MT5 paper daemon. No real order execution is available in this mode.")
        cycles = 0
        try:
            while args.paper_max_cycles == 0 or cycles < args.paper_max_cycles:
                snapshot = _run_paper_mt5_once(cfg, args, engine)
                cycles += 1
                _print_paper_snapshot(snapshot)
                if snapshot["halted"]:
                    print("Paper portfolio is halted by the risk gate; daemon stopped.")
                    break
                if args.paper_max_cycles and cycles >= args.paper_max_cycles:
                    break
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("Paper daemon stopped by user.")
        return

    demo_modes = {"demo-backtest", "batch-demo", "validate-demo", "portfolio-demo"}
    if args.mode in demo_modes:
        raw = _synthetic_data(args.bars, seed=args.seed)
    else:
        raw = _mt5_data(cfg, args.bars)

    if args.save_data or args.mode == "cache-mt5":
        path = store.upsert(cfg.symbol, cfg.timeframe, raw)
        print(f"Cached {len(raw)} bars -> {path}")
        if args.mode == "cache-mt5":
            print(store.coverage(cfg.symbol, cfg.timeframe))
            return

    bt_cfg = _backtest_config(cfg)

    if args.mode in {"batch-demo", "batch-mt5"}:
        names = _selected_strategy_names(args.strategies)
        results = run_strategy_batch(raw, bt_cfg, names)
        path = save_research_results(results, args.output)
        display = results.copy()
        for col in ("return_pct", "max_drawdown_pct", "win_rate"):
            if col in display:
                display[col] = display[col] * 100.0
        print("\n=== RESEARCH LEADERBOARD ===")
        print(display.to_string(index=False))
        print(f"\nSaved leaderboard -> {path}")
        print("NOTE: in-sample rank is only a screening tool, not evidence of a durable edge.")
        return

    if args.mode in {"validate-demo", "validate-mt5"}:
        names = _selected_strategy_names(args.strategies)
        results = validate_strategy_batch(raw, bt_cfg, names, _validation_config(args))
        target = Path(args.validation_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        results.to_csv(target, index=False)
        display = results.copy()
        for col in (
            "oos_return_pct",
            "oos_max_drawdown_pct",
            "walk_forward_positive_fraction",
            "parameter_stability_fraction",
            "monte_carlo_ruin_probability",
            "monte_carlo_p95_drawdown",
        ):
            if col in display:
                display[col] = display[col] * 100.0
        print("\n=== ROBUST VALIDATION ===")
        print(display.to_string(index=False))
        print(f"\nSaved validation report -> {target}")
        print("PASS is a research gate only. It is not permission to deploy meaningful capital.")
        return

    if args.mode in {"portfolio-demo", "portfolio-mt5"}:
        names = _selected_strategy_names(args.strategies)
        verdicts = tuple(x.strip().upper() for x in args.portfolio_verdicts.split(",") if x.strip())
        invalid_verdicts = sorted(set(verdicts).difference({"PASS", "WATCH", "REJECT"}))
        if invalid_verdicts:
            raise ValueError(f"invalid portfolio verdicts: {invalid_verdicts}")
        portfolio_cfg = PortfolioConfig(
            max_strategy_weight=args.max_strategy_weight,
            min_strategies=args.portfolio_min_strategies,
            allowed_verdicts=verdicts,
            monte_carlo_runs=args.mc_runs,
            monte_carlo_block_size=args.portfolio_block_size,
            seed=args.seed,
        )
        report = research_portfolio(raw, bt_cfg, names, _validation_config(args), portfolio_cfg)
        paths = save_portfolio_report(report, args.portfolio_output_dir)

        summary = pd.DataFrame([report["summary"]])
        display_weights = report["weights"].copy()
        for col in ("weight", "risk_contribution", "oos_return_pct", "oos_max_drawdown_pct"):
            if col in display_weights:
                display_weights[col] = display_weights[col] * 100.0
        print("\n=== PORTFOLIO OOS SUMMARY ===")
        print(summary.to_string(index=False))
        print("\n=== PORTFOLIO WEIGHTS ===")
        print(display_weights.to_string(index=False))
        print("\nSaved portfolio reports:")
        for name, path in paths.items():
            print(f"  {name:12s} -> {path}")
        print("Weights are calibrated without using the untouched OOS segment, which is used only for portfolio evaluation.")
        return

    strategy_name = args.strategy or cfg.strategy.name
    params = cfg.strategy.params if args.strategy is None else {}
    signals = build_signals(raw, strategy_name, params)
    result = run_backtest(signals, bt_cfg)
    print_report(result, strategy_name)


if __name__ == "__main__":
    main()
