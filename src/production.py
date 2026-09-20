from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib import request

import pandas as pd
import yaml

from .config import load_config
from .data import normalize_ohlc
from .live import ARM_PHRASE, LiveEngineConfig, LiveTradingEngine
from .mt5_broker import BrokerPosition, BrokerTick, MT5Broker, SymbolSpec
from .ops import operational_report
from .paper import load_portfolio_bundle

RUNTIME_LIVENESS_REQUIRE_ENV = "FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER"


@dataclass(frozen=True)
class ProductionConfig:
    alert_webhook_env: str = "FOREX_ALERT_WEBHOOK_URL"
    alert_outbox_path: str = "runtime/alerts.jsonl"
    alert_state_path: str = "runtime/alert_state.json"
    alert_timeout_seconds: float = 5.0
    alert_cooldown_seconds: float = 900.0
    metrics_path: str = "runtime/execution_metrics.json"
    production_halt_path: str = "runtime/production_halt.json"
    max_log_bytes: int = 5_000_000
    log_backups: int = 5
    spread_min_samples: int = 30
    spread_z_limit: float = 4.0
    slippage_min_samples: int = 20
    slippage_z_limit: float = 4.0
    max_observed_slippage_pips: float = 2.0
    max_future_tick_seconds: float = 5.0
    min_stressed_margin_level_pct: float = 300.0
    min_stressed_free_margin_pct: float = 0.25
    additional_risk_fraction: float = 0.0025
    reconnect_attempts: int = 3
    reconnect_backoff_seconds: float = 5.0


def load_production_config(path: str | Path = "production.yaml") -> ProductionConfig:
    target = Path(path)
    if not target.exists():
        fallback = Path("production.example.yaml")
        if not fallback.exists():
            return ProductionConfig()
        target = fallback
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("production config must be a mapping")
    cfg = ProductionConfig(**raw)
    if cfg.alert_timeout_seconds <= 0 or cfg.alert_cooldown_seconds < 0:
        raise ValueError("alert timing values are invalid")
    if cfg.max_log_bytes < 1024 or cfg.log_backups < 1:
        raise ValueError("log rotation requires max_log_bytes >= 1024 and log_backups >= 1")
    if cfg.spread_min_samples < 2 or cfg.slippage_min_samples < 2:
        raise ValueError("baseline sample minimums must be >= 2")
    if cfg.spread_z_limit <= 0 or cfg.slippage_z_limit <= 0:
        raise ValueError("baseline z limits must be positive")
    if cfg.max_observed_slippage_pips <= 0 or cfg.max_future_tick_seconds < 0:
        raise ValueError("execution/clock limits are invalid")
    if cfg.min_stressed_margin_level_pct <= 0:
        raise ValueError("minimum stressed margin level must be positive")
    if not 0 <= cfg.min_stressed_free_margin_pct < 1:
        raise ValueError("minimum stressed free-margin fraction must be in [0,1)")
    if not 0 <= cfg.additional_risk_fraction < 1:
        raise ValueError("additional risk fraction must be in [0,1)")
    if cfg.reconnect_attempts < 0 or cfg.reconnect_backoff_seconds < 0:
        raise ValueError("reconnect settings cannot be negative")
    return cfg


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    temp.replace(target)


def rotate_file(path: str | Path, max_bytes: int, backups: int) -> bool:
    target = Path(path)
    if not target.exists() or target.stat().st_size < max_bytes:
        return False
    oldest = target.with_name(f"{target.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        source = target.with_name(f"{target.name}.{index}")
        destination = target.with_name(f"{target.name}.{index + 1}")
        if source.exists():
            source.replace(destination)
    target.replace(target.with_name(f"{target.name}.1"))
    return True


class AlertDispatcher:
    def __init__(self, cfg: ProductionConfig) -> None:
        self.cfg = cfg
        self.outbox = Path(cfg.alert_outbox_path)
        self.state_path = Path(cfg.alert_state_path)

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def emit(self, severity: str, code: str, detail: str, context: dict[str, Any] | None = None) -> bool:
        now = datetime.now(timezone.utc)
        fingerprint = f"{severity}:{code}:{detail}"
        state = self._state()
        previous = state.get("last", {})
        if previous.get("fingerprint") == fingerprint:
            sent_at = previous.get("sent_at")
            if sent_at:
                try:
                    elapsed = (now - datetime.fromisoformat(sent_at)).total_seconds()
                    if elapsed < self.cfg.alert_cooldown_seconds:
                        return False
                except Exception:
                    pass

        payload = {
            "time": now.isoformat(),
            "severity": severity,
            "code": code,
            "detail": detail,
            "context": context or {},
        }
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        rotate_file(self.outbox, self.cfg.max_log_bytes, self.cfg.log_backups)
        with self.outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

        webhook_url = os.getenv(self.cfg.alert_webhook_env, "").strip()
        delivered = False
        if webhook_url:
            body = json.dumps(payload, default=str).encode("utf-8")
            req = request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with request.urlopen(req, timeout=self.cfg.alert_timeout_seconds) as response:
                    delivered = 200 <= int(getattr(response, "status", 200)) < 300
            except Exception:
                delivered = False

        atomic_write_json(
            self.state_path,
            {"last": {"fingerprint": fingerprint, "sent_at": now.isoformat(), "webhook_delivered": delivered}},
        )
        return True


class ProductionHaltStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def halt(self, code: str, detail: str) -> None:
        atomic_write_json(
            self.path,
            {"halted": True, "time": datetime.now(timezone.utc).isoformat(), "code": code, "detail": detail},
        )

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True


class MetricStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {"metrics": {}, "processed_orders": []}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.state.update(raw)
            except Exception:
                pass

    def stats(self, name: str) -> dict[str, float]:
        raw = self.state.setdefault("metrics", {}).get(name, {})
        return {
            "count": float(raw.get("count", 0)),
            "mean": float(raw.get("mean", 0.0)),
            "m2": float(raw.get("m2", 0.0)),
            "max": float(raw.get("max", 0.0)),
        }

    def z_score(self, name: str, value: float, min_samples: int) -> float | None:
        stats = self.stats(name)
        count = int(stats["count"])
        if count < min_samples:
            return None
        variance = stats["m2"] / max(count - 1, 1)
        std = math.sqrt(max(variance, 0.0))
        if std <= 1e-9:
            return 0.0 if abs(value - stats["mean"]) <= 1e-9 else float("inf")
        return (value - stats["mean"]) / std

    def observe(self, name: str, value: float) -> None:
        metrics = self.state.setdefault("metrics", {})
        raw = metrics.setdefault(name, {"count": 0, "mean": 0.0, "m2": 0.0, "max": 0.0})
        count = int(raw.get("count", 0)) + 1
        mean = float(raw.get("mean", 0.0))
        delta = value - mean
        mean += delta / count
        delta2 = value - mean
        raw["count"] = count
        raw["mean"] = mean
        raw["m2"] = float(raw.get("m2", 0.0)) + delta * delta2
        raw["max"] = max(float(raw.get("max", 0.0)), value)

    def order_processed(self, order_id: int) -> bool:
        return str(order_id) in set(str(item) for item in self.state.get("processed_orders", []))

    def mark_order_processed(self, order_id: int) -> None:
        items = [str(item) for item in self.state.get("processed_orders", [])]
        key = str(order_id)
        if key not in items:
            items.append(key)
        self.state["processed_orders"] = items[-2000:]

    def save(self) -> None:
        atomic_write_json(self.path, self.state)


def tick_future_seconds(tick: BrokerTick, now: datetime | None = None) -> float:
    if tick.time_msc <= 0:
        return 0.0
    current = now or datetime.now(timezone.utc)
    tick_time = datetime.fromtimestamp(tick.time_msc / 1000.0, tz=timezone.utc)
    return max(0.0, (tick_time - current).total_seconds())


def stop_loss_cash_risk(position: BrokerPosition, tick: BrokerTick, spec: SymbolSpec) -> float:
    if position.stop_loss <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return float("inf")
    current_exit = tick.bid if position.side > 0 else tick.ask
    distance = max(0.0, position.side * (current_exit - position.stop_loss))
    return (distance / spec.tick_size) * spec.tick_value * position.volume


def margin_stress_report(
    account: Any,
    positions: list[BrokerPosition],
    tick: BrokerTick,
    spec: SymbolSpec,
    *,
    additional_risk_fraction: float,
    min_margin_level_pct: float,
    min_free_margin_pct: float,
) -> dict[str, Any]:
    existing_stop_risk = sum(stop_loss_cash_risk(position, tick, spec) for position in positions)
    if not math.isfinite(existing_stop_risk):
        existing_stop_risk = float("inf")
    extra_risk = max(0.0, float(account.equity) * additional_risk_fraction)
    total_stress = existing_stop_risk + extra_risk
    stressed_equity = float(account.equity) - total_stress
    stressed_free_margin = float(account.margin_free) - total_stress
    stressed_margin_level = float("inf") if float(account.margin) <= 0 else stressed_equity / float(account.margin) * 100.0
    free_margin_fraction = stressed_free_margin / max(float(account.equity), 1e-12)
    checks = {
        "stressed_equity_positive": stressed_equity > 0,
        "stressed_free_margin_positive": stressed_free_margin > 0,
        "stressed_free_margin_fraction": free_margin_fraction >= min_free_margin_pct,
        "stressed_margin_level": stressed_margin_level >= min_margin_level_pct,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "existing_stop_risk": existing_stop_risk,
        "additional_risk": extra_risk,
        "stressed_equity": stressed_equity,
        "stressed_free_margin": stressed_free_margin,
        "stressed_free_margin_fraction": free_margin_fraction,
        "stressed_margin_level_pct": stressed_margin_level,
    }


def _reason_order_id(reason: str) -> int | None:
    match = re.search(r"(?:^|;)order=(\d+)(?:;|$)", reason)
    return int(match.group(1)) if match else None


def reconcile_slippage_samples(events_path: str | Path, pip_size: float, metrics: MetricStore) -> list[dict[str, float | int]]:
    target = Path(events_path)
    if not target.exists() or pip_size <= 0:
        return []
    entries: dict[int, tuple[int, float]] = {}
    reconciled: list[tuple[int, int, float]] = []
    with target.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            event = str(row.get("event", ""))
            if event == "ENTRY":
                try:
                    ticket = int(float(row.get("ticket") or 0))
                    side = int(float(row.get("side") or 0))
                    price = float(row.get("price") or 0.0)
                except ValueError:
                    continue
                if ticket > 0 and side in (-1, 1) and price > 0:
                    entries[ticket] = (side, price)
            elif event == "RECONCILE_DEAL":
                order_id = _reason_order_id(str(row.get("reason", "")))
                if not order_id:
                    continue
                try:
                    side = int(float(row.get("side") or 0))
                    price = float(row.get("price") or 0.0)
                except ValueError:
                    continue
                if side in (-1, 1) and price > 0:
                    reconciled.append((order_id, side, price))

    samples: list[dict[str, float | int]] = []
    for order_id, deal_side, fill_price in reconciled:
        if metrics.order_processed(order_id) or order_id not in entries:
            continue
        entry_side, requested_price = entries[order_id]
        side = deal_side if deal_side in (-1, 1) else entry_side
        adverse = max(0.0, side * (fill_price - requested_price) / pip_size)
        samples.append({"order": order_id, "slippage_pips": adverse})
        metrics.mark_order_processed(order_id)
    return samples


def _completed_from_broker(broker: MT5Broker, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    raw = normalize_ohlc(broker.rates(symbol, timeframe, bars=bars))
    if len(raw) < 2:
        raise RuntimeError("MT5 returned too few bars to isolate the last completed bar")
    return raw.iloc[:-1].copy()


def _live_engine_config(app_cfg: Any) -> LiveEngineConfig:
    timeframe_seconds = {
        "M1": 60.0,
        "M5": 300.0,
        "M15": 900.0,
        "M30": 1800.0,
        "H1": 3600.0,
        "H4": 14400.0,
        "D1": 86400.0,
    }.get(str(app_cfg.timeframe).upper(), 3600.0)
    max_bar_age = app_cfg.live.max_bar_age_seconds or timeframe_seconds * 2.5
    return LiveEngineConfig(
        risk_per_trade=app_cfg.live.risk_per_trade,
        max_daily_loss_pct=app_cfg.live.max_daily_loss_pct,
        max_drawdown_pct=app_cfg.live.max_drawdown_pct,
        max_lot_per_order=app_cfg.live.max_lot_per_order,
        max_total_lots=app_cfg.live.max_total_lots,
        max_open_positions=app_cfg.live.max_open_positions,
        max_spread_pips=app_cfg.live.max_spread_pips,
        allowed_account_logins=tuple(app_cfg.live.allowed_account_logins),
        require_account_allowlist=app_cfg.live.require_account_allowlist,
        max_margin_fraction_of_equity=app_cfg.live.max_margin_fraction_of_equity,
        min_free_margin_fraction_after_order=app_cfg.live.min_free_margin_fraction_after_order,
        max_tick_age_seconds=app_cfg.live.max_tick_age_seconds,
        max_bar_age_seconds=max_bar_age,
        deal_reconcile_lookback_hours=app_cfg.live.deal_reconcile_lookback_hours,
        magic=app_cfg.live.magic,
        deviation_points=app_cfg.live.deviation_points,
        state_path=app_cfg.live.state_path,
        events_path=app_cfg.live.events_path,
        heartbeat_path=app_cfg.live.heartbeat_path,
        incidents_path=app_cfg.live.incidents_path,
    )


def rotate_runtime_logs(app_cfg: Any, prod_cfg: ProductionConfig) -> list[str]:
    rotated: list[str] = []
    for path in (app_cfg.live.events_path, app_cfg.live.incidents_path, prod_cfg.alert_outbox_path):
        if rotate_file(path, prod_cfg.max_log_bytes, prod_cfg.log_backups):
            rotated.append(str(path))
    return rotated


def production_guard_report(
    broker: Any,
    app_cfg: Any,
    prod_cfg: ProductionConfig,
    strategies: dict[str, dict[str, Any]],
    completed_bars: pd.DataFrame,
    metrics: MetricStore,
) -> dict[str, Any]:
    live_cfg = _live_engine_config(app_cfg)
    latest = completed_bars.index[-1]
    ops = operational_report(
        broker,
        app_cfg.symbol,
        live_cfg.magic,
        set(strategies),
        latest,
        max_tick_age_seconds=live_cfg.max_tick_age_seconds,
        max_bar_age_seconds=live_cfg.max_bar_age_seconds,
    )
    tick = ops["tick"]
    spec = ops["symbol_spec"]
    spread = float(ops["spread_pips"])
    spread_z = metrics.z_score("spread_pips", spread, prod_cfg.spread_min_samples)
    metrics.observe("spread_pips", spread)

    future_seconds = tick_future_seconds(tick)
    stress = margin_stress_report(
        ops["account"],
        list(ops["positions"]),
        tick,
        spec,
        additional_risk_fraction=prod_cfg.additional_risk_fraction,
        min_margin_level_pct=prod_cfg.min_stressed_margin_level_pct,
        min_free_margin_pct=prod_cfg.min_stressed_free_margin_pct,
    )

    incidents: list[dict[str, str]] = [
        {"severity": item.severity, "code": item.code, "detail": item.detail} for item in ops["incidents"]
    ]
    if live_cfg.require_account_allowlist and (
        not live_cfg.allowed_account_logins or int(ops["account"].login) not in set(live_cfg.allowed_account_logins)
    ):
        incidents.append(
            {
                "severity": "CRITICAL",
                "code": "ACCOUNT_LOGIN_NOT_ALLOWED",
                "detail": f"MT5 login {ops['account'].login} is not in the configured live account allowlist",
            }
        )
    if future_seconds > prod_cfg.max_future_tick_seconds:
        incidents.append(
            {
                "severity": "CRITICAL",
                "code": "BROKER_CLOCK_AHEAD",
                "detail": f"broker tick is {future_seconds:.2f}s ahead of local UTC clock",
            }
        )
    if spread_z is not None and spread_z > prod_cfg.spread_z_limit:
        incidents.append(
            {
                "severity": "WARN",
                "code": "SPREAD_BASELINE_ANOMALY",
                "detail": f"spread {spread:.2f} pips is z={spread_z:.2f} versus historical baseline",
            }
        )
    if not stress["ok"]:
        failed = ",".join(name for name, passed in stress["checks"].items() if not passed)
        incidents.append(
            {
                "severity": "CRITICAL",
                "code": "MARGIN_STRESS_FAILED",
                "detail": f"failed={failed};stressed_margin_level={stress['stressed_margin_level_pct']:.1f}%",
            }
        )

    status = "CRITICAL" if any(item["severity"] == "CRITICAL" for item in incidents) else "WARN" if incidents else "OK"
    return {
        "status": status,
        "ops": ops,
        "spread_z": spread_z,
        "future_tick_seconds": future_seconds,
        "margin_stress": stress,
        "incidents": incidents,
    }


def _alert_incidents(alerts: AlertDispatcher, report: dict[str, Any], symbol: str) -> None:
    for incident in report["incidents"]:
        alerts.emit(
            incident["severity"],
            incident["code"],
            incident["detail"],
            {"symbol": symbol, "status": report["status"]},
        )


def _reconnect(broker: MT5Broker, prod_cfg: ProductionConfig, alerts: AlertDispatcher) -> bool:
    for attempt in range(1, prod_cfg.reconnect_attempts + 1):
        if prod_cfg.reconnect_backoff_seconds > 0:
            time.sleep(prod_cfg.reconnect_backoff_seconds)
        try:
            broker.reconnect()
            if broker.terminal_snapshot().connected:
                alerts.emit("WARN", "MT5_RECONNECTED", f"connection recovered on attempt {attempt}")
                return True
        except Exception as exc:
            alerts.emit("WARN", "MT5_RECONNECT_FAILED", f"attempt={attempt};error={exc}")
    return False


def _print_guard(report: dict[str, Any]) -> None:
    stress = report["margin_stress"]
    print("\n=== PHASE 9 PRODUCTION HEALTH ===")
    print(f"Status                         : {report['status']}")
    print(f"Spread                         : {report['ops']['spread_pips']:.2f} pips")
    print(f"Spread z-score                 : {report['spread_z']}")
    print(f"Broker clock ahead             : {report['future_tick_seconds']:.2f}s")
    print(f"Stressed margin level          : {stress['stressed_margin_level_pct']:.1f}%")
    print(f"Stressed free-margin fraction  : {stress['stressed_free_margin_fraction'] * 100:.1f}%")
    if report["incidents"]:
        for item in report["incidents"]:
            print(f"[{item['severity']}] {item['code']}: {item['detail']}")
    else:
        print("Incidents                      : none")



class RuntimeLivenessError(RuntimeError):
    pass


def _runtime_liveness_enabled() -> bool:
    raw = os.getenv(RUNTIME_LIVENESS_REQUIRE_ENV, "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeLivenessError(f"{RUNTIME_LIVENESS_REQUIRE_ENV} must be 0/1/true/false")


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _runtime_liveness_health(
    *,
    stage: str,
    symbol: str,
    report: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    production_halt: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = report or {}
    ops = report.get("ops") if isinstance(report.get("ops"), dict) else {}
    account = ops.get("account")
    positions = list(ops.get("positions") or [])
    incident_rows = report.get("incidents") if isinstance(report.get("incidents"), list) else []
    incidents = []
    for item in incident_rows:
        if isinstance(item, dict):
            incidents.append({
                "severity": str(item.get("severity", "")),
                "code": str(item.get("code", "")),
            })
    stage_name = str(stage).upper()
    status = str(report.get("status") or "OK").upper()
    if stage_name == "DEGRADED":
        status = "DEGRADED"
    elif stage_name == "HALTED":
        status = "HALTED"
    elif stage_name == "ERROR":
        status = "ERROR"
    live = snapshot or {}
    payload = {
        "status": status,
        "symbol": symbol,
        "market_state": ops.get("market_state"),
        "market_reason": ops.get("market_reason"),
        "spread_pips": _safe_number(ops.get("spread_pips")),
        "tick_age_seconds": _safe_number(ops.get("tick_age_seconds")),
        "bar_age_seconds": _safe_number(ops.get("bar_age_seconds")),
        "account": {
            "login": int(getattr(account, "login", 0) or 0) if account is not None else 0,
            "balance": _safe_number(getattr(account, "balance", None)) if account is not None else None,
            "equity": _safe_number(getattr(account, "equity", None)) if account is not None else None,
            "margin_free": _safe_number(getattr(account, "margin_free", None)) if account is not None else None,
            "margin_level": _safe_number(getattr(account, "margin_level", None)) if account is not None else None,
        },
        "managed_positions": {
            "count": len(positions),
            "total_lots": _safe_number(sum(float(getattr(item, "volume", 0.0) or 0.0) for item in positions)),
        },
        "incidents": incidents,
        "live": {
            "halted": bool(live.get("halted", False)),
            "halt_reason": live.get("halt_reason"),
            "last_bar_time": live.get("last_bar_time"),
            "last_deal_time_msc": int(live.get("last_deal_time_msc", 0) or 0),
            "last_deal_ticket": int(live.get("last_deal_ticket", 0) or 0),
            "pending_order_intent_id": live.get("pending_order_intent_id"),
            "managed_positions": int(live.get("managed_positions", 0) or 0),
            "managed_total_lots": _safe_number(live.get("managed_total_lots")),
        },
        "production_halt": production_halt,
    }
    if extra:
        payload["extra"] = extra
    return payload


def _publish_runtime_liveness(
    *,
    stage: str,
    cycle: int,
    symbol: str,
    report: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    production_halt: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _runtime_liveness_enabled():
        return None
    health = _runtime_liveness_health(
        stage=stage,
        symbol=symbol,
        report=report,
        snapshot=snapshot,
        production_halt=production_halt,
        extra=extra,
    )
    try:
        # Lazy import avoids the existing recovery -> production dependency
        # from becoming a production -> liveness -> recovery cycle.
        from .runtime_liveness import append_liveness_from_env

        return append_liveness_from_env(
            Path.cwd(),
            stage=stage,
            cycle=cycle,
            health=health,
        )
    except Exception as exc:
        raise RuntimeLivenessError(f"runtime liveness checkpoint failed: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 9 production observability/supervision")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--production-config", default="production.yaml")
    parser.add_argument("--mode", choices=["health", "supervised-live", "rotate-logs", "clear-halt"], default="health")
    parser.add_argument("--arm-live", default=None, help=f"Required only for supervised-live; exact value: {ARM_PHRASE}")
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = run until stopped")
    parser.add_argument("--poll-seconds", type=int, default=None)
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        config_path = Path("config.example.yaml")
    app_cfg = load_config(config_path)
    prod_cfg = load_production_config(args.production_config)
    alerts = AlertDispatcher(prod_cfg)
    halt_store = ProductionHaltStore(prod_cfg.production_halt_path)
    metrics = MetricStore(prod_cfg.metrics_path)

    if args.mode == "clear-halt":
        print("Production halt cleared." if halt_store.clear() else "No production halt was present.")
        return
    if args.mode == "rotate-logs":
        rotated = rotate_runtime_logs(app_cfg, prod_cfg)
        print("Rotated: " + ", ".join(rotated) if rotated else "No runtime log required rotation.")
        return

    existing_halt = halt_store.load()
    if existing_halt and args.mode == "supervised-live":
        raise RuntimeError(f"production halt is active: {existing_halt}; review and run --mode clear-halt explicitly")

    strategies, weights = load_portfolio_bundle(app_cfg.live.weights_path, app_cfg.live.candidates_path)
    broker = MT5Broker()
    broker.connect()
    try:
        completed = _completed_from_broker(broker, app_cfg.symbol, app_cfg.timeframe, app_cfg.live.history_bars)
        report = production_guard_report(broker, app_cfg, prod_cfg, strategies, completed, metrics)
        metrics.save()
        _print_guard(report)
        if args.mode == "health":
            _alert_incidents(alerts, report, app_cfg.symbol)
            return

        if not app_cfg.live.enabled or args.arm_live != ARM_PHRASE:
            raise RuntimeError("supervised-live requires live.enabled=true plus the exact --arm-live phrase")
        if report["status"] == "CRITICAL":
            _alert_incidents(alerts, report, app_cfg.symbol)
            halt_store.halt("PRODUCTION_GUARD", ";".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL"))
            _publish_runtime_liveness(
                stage="HALTED", cycle=0, symbol=app_cfg.symbol, report=report,
                snapshot=None, production_halt=halt_store.load(),
            )
            raise RuntimeError("production guard is CRITICAL; live engine not started")

        engine = LiveTradingEngine(
            broker,
            app_cfg.symbol,
            strategies,
            weights,
            _live_engine_config(app_cfg),
            app_cfg.live.enabled,
            args.arm_live,
        )
        cycles = 0
        poll_seconds = args.poll_seconds or app_cfg.live.poll_seconds
        while args.max_cycles == 0 or cycles < args.max_cycles:
            cycles += 1
            rotate_runtime_logs(app_cfg, prod_cfg)
            report = None
            snapshot = None
            try:
                completed = _completed_from_broker(broker, app_cfg.symbol, app_cfg.timeframe, app_cfg.live.history_bars)
                report = production_guard_report(broker, app_cfg, prod_cfg, strategies, completed, metrics)
                _alert_incidents(alerts, report, app_cfg.symbol)
                if report["status"] == "CRITICAL":
                    codes = ",".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL")
                    halt_store.halt("PRODUCTION_GUARD", codes)
                    alerts.emit("CRITICAL", "PRODUCTION_HALT", codes, {"cycle": cycles})
                    _publish_runtime_liveness(
                        stage="HALTED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=engine.snapshot(), production_halt=halt_store.load(),
                    )
                    break
                if report["status"] == "WARN":
                    _publish_runtime_liveness(
                        stage="DEGRADED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=engine.snapshot(), production_halt=halt_store.load(),
                    )
                    metrics.save()
                    if args.max_cycles and cycles >= args.max_cycles:
                        break
                    time.sleep(poll_seconds)
                    continue

                # Phase 33 fail-closed gate: prove the remote liveness ledger is
                # writable and chained before this cycle can evaluate/send orders.
                _publish_runtime_liveness(
                    stage="READY", cycle=cycles, symbol=app_cfg.symbol, report=report,
                    snapshot=engine.snapshot(), production_halt=halt_store.load(),
                )
                snapshot = engine.process_latest(completed)
                slippage_samples = reconcile_slippage_samples(app_cfg.live.events_path, report["ops"]["symbol_spec"].pip_size, metrics)
                for sample in slippage_samples:
                    value = float(sample["slippage_pips"])
                    z_score = metrics.z_score("slippage_pips", value, prod_cfg.slippage_min_samples)
                    metrics.observe("slippage_pips", value)
                    if value > prod_cfg.max_observed_slippage_pips or (z_score is not None and z_score > prod_cfg.slippage_z_limit):
                        detail = f"order={sample['order']};slippage={value:.2f}p;z={z_score}"
                        halt_store.halt("SLIPPAGE_ANOMALY", detail)
                        alerts.emit("CRITICAL", "SLIPPAGE_ANOMALY", detail)
                        snapshot["halted"] = True
                metrics.save()
                if snapshot.get("halted"):
                    _publish_runtime_liveness(
                        stage="HALTED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=snapshot, production_halt=halt_store.load(),
                    )
                    alerts.emit("CRITICAL", "LIVE_ENGINE_HALTED", str(snapshot.get("halt_reason") or "production halt"))
                    break
            except RuntimeLivenessError as exc:
                detail = str(exc)
                halt_store.halt("RUNTIME_LIVENESS_FAILED", detail)
                alerts.emit("CRITICAL", "RUNTIME_LIVENESS_FAILED", detail, {"cycle": cycles})
                break
            except Exception as exc:
                try:
                    _publish_runtime_liveness(
                        stage="ERROR", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=snapshot, production_halt=halt_store.load(),
                        extra={"exception": str(exc)},
                    )
                except RuntimeLivenessError as liveness_exc:
                    detail = str(liveness_exc)
                    halt_store.halt("RUNTIME_LIVENESS_FAILED", detail)
                    alerts.emit("CRITICAL", "RUNTIME_LIVENESS_FAILED", detail, {"cycle": cycles})
                    break
                alerts.emit("CRITICAL", "LIVE_RUNTIME_ERROR", str(exc), {"cycle": cycles})
                if not _reconnect(broker, prod_cfg, alerts):
                    halt_store.halt("RECONNECT_EXHAUSTED", str(exc))
                    break
            if args.max_cycles and cycles >= args.max_cycles:
                break
            time.sleep(poll_seconds)
    finally:
        metrics.save()
        broker.close()


if __name__ == "__main__":
    main()
