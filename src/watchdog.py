from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import request

import yaml

from .production import atomic_write_json, rotate_file


@dataclass(frozen=True)
class WatchdogConfig:
    heartbeat_path: str = "runtime/live_heartbeat.json"
    production_halt_path: str = "runtime/production_halt.json"
    state_path: str = "runtime/watchdog_state.json"
    alert_outbox_path: str = "runtime/watchdog_alerts.jsonl"
    alert_state_path: str = "runtime/watchdog_alert_state.json"
    heartbeat_url_env: str = "FOREX_HEARTBEAT_URL"
    heartbeat_bearer_env: str = "FOREX_HEARTBEAT_BEARER"
    alert_webhook_env: str = "FOREX_WATCHDOG_WEBHOOK_URL"
    alert_bearer_env: str = "FOREX_WATCHDOG_WEBHOOK_BEARER"
    request_timeout_seconds: float = 5.0
    alert_cooldown_seconds: float = 900.0
    max_heartbeat_age_seconds: float = 120.0
    max_future_seconds: float = 10.0
    poll_seconds: float = 30.0
    max_log_bytes: int = 5_000_000
    log_backups: int = 5


def load_watchdog_config(path: str | Path = "watchdog.yaml") -> WatchdogConfig:
    target = Path(path)
    if not target.exists():
        fallback = Path("watchdog.example.yaml")
        if not fallback.exists():
            return WatchdogConfig()
        target = fallback
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("watchdog config must be a mapping")
    cfg = WatchdogConfig(**raw)
    if cfg.request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")
    if cfg.alert_cooldown_seconds < 0:
        raise ValueError("alert_cooldown_seconds cannot be negative")
    if cfg.max_heartbeat_age_seconds <= 0 or cfg.max_future_seconds < 0:
        raise ValueError("heartbeat timing limits are invalid")
    if cfg.poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if cfg.max_log_bytes < 1024 or cfg.log_backups < 1:
        raise ValueError("log rotation settings are invalid")
    return cfg


def _parse_utc(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_heartbeat(
    heartbeat: dict[str, Any],
    *,
    max_age_seconds: float,
    max_future_seconds: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    updated_at_raw = str(heartbeat.get("updated_at", "") or "").strip()
    if not updated_at_raw:
        return {
            "status": "CRITICAL",
            "code": "HEARTBEAT_TIMESTAMP_MISSING",
            "detail": "heartbeat has no updated_at timestamp",
            "heartbeat_age_seconds": None,
        }
    try:
        updated_at = _parse_utc(updated_at_raw)
    except Exception as exc:
        return {
            "status": "CRITICAL",
            "code": "HEARTBEAT_TIMESTAMP_INVALID",
            "detail": f"cannot parse heartbeat updated_at: {exc}",
            "heartbeat_age_seconds": None,
        }

    age = (current - updated_at).total_seconds()
    if age < -max_future_seconds:
        return {
            "status": "CRITICAL",
            "code": "HEARTBEAT_CLOCK_AHEAD",
            "detail": f"heartbeat timestamp is {-age:.1f}s ahead of watchdog UTC clock",
            "heartbeat_age_seconds": age,
        }
    if age > max_age_seconds:
        return {
            "status": "CRITICAL",
            "code": "HEARTBEAT_TIMEOUT",
            "detail": f"heartbeat age {age:.1f}s exceeds {max_age_seconds:.1f}s",
            "heartbeat_age_seconds": age,
        }

    incidents = heartbeat.get("incidents") or []
    critical_incidents = [item for item in incidents if str(item.get("severity", "")).upper() == "CRITICAL"]
    if critical_incidents:
        codes = ",".join(sorted({str(item.get("code", "UNKNOWN")) for item in critical_incidents}))
        return {
            "status": "CRITICAL",
            "code": "HEARTBEAT_CRITICAL_INCIDENT",
            "detail": f"heartbeat reports critical incident(s): {codes}",
            "heartbeat_age_seconds": max(0.0, age),
        }

    remote_status = str(heartbeat.get("status", "UNKNOWN") or "UNKNOWN").upper()
    if remote_status in {"HALTED", "CRITICAL"}:
        return {
            "status": "CRITICAL",
            "code": "REMOTE_HALTED",
            "detail": f"trading host heartbeat status is {remote_status}",
            "heartbeat_age_seconds": max(0.0, age),
        }
    if remote_status in {"DEGRADED", "WARN", "WARNING"}:
        return {
            "status": "WARN",
            "code": "REMOTE_DEGRADED",
            "detail": f"trading host heartbeat status is {remote_status}",
            "heartbeat_age_seconds": max(0.0, age),
        }
    if remote_status != "OK":
        return {
            "status": "WARN",
            "code": "REMOTE_STATUS_UNKNOWN",
            "detail": f"unrecognized heartbeat status {remote_status!r}",
            "heartbeat_age_seconds": max(0.0, age),
        }
    return {
        "status": "OK",
        "code": "OK",
        "detail": "heartbeat is fresh and reports OK",
        "heartbeat_age_seconds": max(0.0, age),
    }


def _read_json_url(url: str, token: str, timeout_seconds: float) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        if not 200 <= int(getattr(response, "status", 200)) < 300:
            raise RuntimeError(f"heartbeat HTTP status {getattr(response, 'status', None)}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("remote heartbeat must be a JSON object")
    return payload


def read_heartbeat(cfg: WatchdogConfig) -> tuple[dict[str, Any], str]:
    url = os.getenv(cfg.heartbeat_url_env, "").strip()
    if url:
        token = os.getenv(cfg.heartbeat_bearer_env, "").strip()
        return _read_json_url(url, token, cfg.request_timeout_seconds), "remote"
    path = Path(cfg.heartbeat_path)
    if not path.exists():
        raise FileNotFoundError(f"heartbeat file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("heartbeat file must contain a JSON object")
    return payload, "file"


def _production_halt(cfg: WatchdogConfig) -> dict[str, Any] | None:
    path = Path(cfg.production_halt_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"halted": True, "detail": "invalid halt payload"}
    except Exception as exc:
        return {"halted": True, "detail": f"cannot read production halt: {exc}"}


class WatchdogAlertDispatcher:
    def __init__(self, cfg: WatchdogConfig) -> None:
        self.cfg = cfg

    def _alert_state(self) -> dict[str, Any]:
        path = Path(self.cfg.alert_state_path)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def emit(self, result: dict[str, Any]) -> bool:
        if result["status"] == "OK":
            return False
        now = datetime.now(timezone.utc)
        fingerprint = f"{result['status']}:{result['code']}:{result['detail']}"
        previous = self._alert_state().get("last", {})
        if previous.get("fingerprint") == fingerprint and previous.get("sent_at"):
            try:
                elapsed = (now - _parse_utc(str(previous["sent_at"]))).total_seconds()
                if elapsed < self.cfg.alert_cooldown_seconds:
                    return False
            except Exception:
                pass

        payload = {"time": now.isoformat(), **result}
        outbox = Path(self.cfg.alert_outbox_path)
        outbox.parent.mkdir(parents=True, exist_ok=True)
        rotate_file(outbox, self.cfg.max_log_bytes, self.cfg.log_backups)
        with outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

        delivered = False
        webhook = os.getenv(self.cfg.alert_webhook_env, "").strip()
        if webhook:
            headers = {"Content-Type": "application/json"}
            bearer = os.getenv(self.cfg.alert_bearer_env, "").strip()
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            req = request.Request(
                webhook,
                data=json.dumps(payload, default=str).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as response:
                    delivered = 200 <= int(getattr(response, "status", 200)) < 300
            except Exception:
                delivered = False

        atomic_write_json(
            self.cfg.alert_state_path,
            {"last": {"fingerprint": fingerprint, "sent_at": now.isoformat(), "webhook_delivered": delivered}},
        )
        return True


def watchdog_once(cfg: WatchdogConfig, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    source = "unknown"
    try:
        heartbeat, source = read_heartbeat(cfg)
        result = evaluate_heartbeat(
            heartbeat,
            max_age_seconds=cfg.max_heartbeat_age_seconds,
            max_future_seconds=cfg.max_future_seconds,
            now=current,
        )
    except Exception as exc:
        result = {
            "status": "CRITICAL",
            "code": "HEARTBEAT_UNAVAILABLE",
            "detail": str(exc),
            "heartbeat_age_seconds": None,
        }

    halt = _production_halt(cfg)
    if halt and bool(halt.get("halted", True)):
        result = {
            "status": "CRITICAL",
            "code": "PRODUCTION_HALT_PRESENT",
            "detail": str(halt.get("detail") or halt.get("code") or "production halt file is present"),
            "heartbeat_age_seconds": result.get("heartbeat_age_seconds"),
        }

    result = {
        **result,
        "checked_at": current.isoformat(),
        "source": source,
    }
    atomic_write_json(cfg.state_path, result)
    WatchdogAlertDispatcher(cfg).emit(result)
    return result


def _exit_code(status: str) -> int:
    return 0 if status == "OK" else 2 if status == "WARN" else 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent Forex trader heartbeat watchdog")
    parser.add_argument("--config", default="watchdog.yaml")
    parser.add_argument("--mode", choices=["once", "daemon"], default="once")
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run until interrupted")
    args = parser.parse_args()

    cfg = load_watchdog_config(args.config)
    poll = args.poll_seconds or cfg.poll_seconds
    if poll <= 0 or args.max_cycles < 0:
        raise ValueError("invalid watchdog daemon timing")

    cycles = 0
    last_status = "OK"
    try:
        while True:
            result = watchdog_once(cfg)
            last_status = str(result["status"])
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
            cycles += 1
            if args.mode == "once" or (args.max_cycles and cycles >= args.max_cycles):
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("Watchdog stopped by operator.")
    raise SystemExit(_exit_code(last_status))


if __name__ == "__main__":
    main()
