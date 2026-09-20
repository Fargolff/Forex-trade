from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import request

import yaml

from .audit_ledger import DEFAULT_REPLICA_SUBDIR
from .key_policy import DEFAULT_TRUST_STORE
from .runtime_liveness import verify_liveness_ledger

WATCHER_FORMAT = "forex-auto-trader-independent-remote-watcher-state"
WATCHER_VERSION = 1
WATCHER_WEBHOOK_ENV = "FOREX_WATCHER_ALERT_WEBHOOK_URL"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class RemoteWatcherConfig:
    observer_id: str
    environment_id: str
    machine_id: str
    replica_root: str
    replica_subdir: str = DEFAULT_REPLICA_SUBDIR
    trust_store_path: str = DEFAULT_TRUST_STORE
    state_path: str = "runtime/remote_watcher_state.json"
    outbox_path: str = "runtime/remote_watcher_alerts.jsonl"
    warning_after_seconds: float = 120.0
    critical_after_seconds: float = 300.0
    repeat_warning_seconds: float = 1800.0
    repeat_critical_seconds: float = 600.0
    webhook_env: str = WATCHER_WEBHOOK_ENV
    webhook_timeout_seconds: float = 5.0
    recovery_notifications: bool = True
    max_outbox_bytes: int = 5_000_000
    outbox_backups: int = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str, field: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception as exc:
        raise ValueError(f"{field} must be ISO-8601 with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _identity(value: str, field: str) -> str:
    text = str(value).strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    return text


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_config(cfg: RemoteWatcherConfig, root: Path) -> None:
    observer = _identity(cfg.observer_id, "observer_id")
    machine = _identity(cfg.machine_id, "machine_id")
    _identity(cfg.environment_id, "environment_id")
    if observer == machine:
        raise ValueError("observer_id must differ from the trading machine_id")
    if cfg.warning_after_seconds <= 0:
        raise ValueError("warning_after_seconds must be positive")
    if cfg.critical_after_seconds <= cfg.warning_after_seconds:
        raise ValueError("critical_after_seconds must be greater than warning_after_seconds")
    if cfg.repeat_warning_seconds < 0 or cfg.repeat_critical_seconds < 0:
        raise ValueError("repeat alert intervals cannot be negative")
    if cfg.webhook_timeout_seconds <= 0:
        raise ValueError("webhook_timeout_seconds must be positive")
    if cfg.max_outbox_bytes < 1024 or cfg.outbox_backups < 1:
        raise ValueError("outbox rotation requires max_outbox_bytes >= 1024 and outbox_backups >= 1")
    replica = _resolve(root, cfg.replica_root)
    state = _resolve(root, cfg.state_path)
    outbox = _resolve(root, cfg.outbox_path)
    if _is_within(state, replica) or _is_within(outbox, replica):
        raise ValueError("watcher state/outbox must remain outside the monitored replica root")


def load_watcher_config(path: str | Path = "remote_watcher.yaml", *, root: str | Path = ".") -> RemoteWatcherConfig:
    target = Path(path)
    if not target.exists():
        fallback = Path("remote_watcher.example.yaml")
        if not fallback.exists():
            raise FileNotFoundError(target)
        target = fallback
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("remote watcher config must be a mapping")
    cfg = RemoteWatcherConfig(**raw)
    _validate_config(cfg, Path(root).resolve())
    return cfg


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def _default_state(cfg: RemoteWatcherConfig) -> dict[str, Any]:
    return {
        "version": WATCHER_VERSION,
        "format": WATCHER_FORMAT,
        "observer_id": cfg.observer_id,
        "environment_id": cfg.environment_id,
        "machine_id": cfg.machine_id,
        "ever_seen": False,
        "last_sequence": 0,
        "last_checkpoint_sha256": None,
        "last_boot_id": None,
        "last_observed_at": None,
        "last_check_at": None,
        "active_incident": None,
        "last_result": None,
    }


def _load_state(path: Path, cfg: RemoteWatcherConfig) -> dict[str, Any]:
    if not path.exists():
        return _default_state(cfg)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"WATCHER_STATE_CORRUPT:{type(exc).__name__}:{exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("WATCHER_STATE_CORRUPT:not_object")
    if state.get("version") != WATCHER_VERSION or state.get("format") != WATCHER_FORMAT:
        raise RuntimeError("WATCHER_STATE_FORMAT_INVALID")
    if state.get("observer_id") != cfg.observer_id:
        raise RuntimeError("WATCHER_STATE_OBSERVER_MISMATCH")
    if state.get("environment_id") != cfg.environment_id or state.get("machine_id") != cfg.machine_id:
        raise RuntimeError("WATCHER_STATE_SCOPE_MISMATCH")
    return state


def _rotate(path: Path, max_bytes: int, backups: int) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        destination = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(destination)
    path.replace(path.with_name(f"{path.name}.1"))


class WatcherAlertDispatcher:
    def __init__(self, cfg: RemoteWatcherConfig, root: Path) -> None:
        self.cfg = cfg
        self.outbox = _resolve(root, cfg.outbox_path)

    def emit(
        self,
        *,
        severity: str,
        code: str,
        detail: str,
        context: dict[str, Any],
        observed_at: datetime,
    ) -> dict[str, Any]:
        payload = {
            "time": _iso(observed_at),
            "source": "independent_remote_watcher",
            "observer_id": self.cfg.observer_id,
            "environment_id": self.cfg.environment_id,
            "machine_id": self.cfg.machine_id,
            "severity": severity,
            "code": code,
            "detail": detail,
            "context": context,
        }
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        _rotate(self.outbox, self.cfg.max_outbox_bytes, self.cfg.outbox_backups)
        with self.outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

        webhook_url = os.getenv(self.cfg.webhook_env, "").strip()
        delivered: bool | None = None
        if webhook_url:
            delivered = False
            body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            req = request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with request.urlopen(req, timeout=self.cfg.webhook_timeout_seconds) as response:
                    delivered = 200 <= int(getattr(response, "status", 200)) < 300
            except Exception:
                delivered = False
        return {
            "outbox_recorded": True,
            "webhook_configured": bool(webhook_url),
            "webhook_delivered": delivered,
        }


def _head_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    scope = Path(str(report.get("scope", "")))
    head_path = scope / "head.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    if not isinstance(head, dict):
        raise ValueError("liveness head must be a JSON object")
    sequence = int(head.get("sequence", 0))
    checkpoint_hash = str(head.get("checkpoint_manifest_sha256", ""))
    if sequence < 1 or len(checkpoint_hash) != 64:
        raise ValueError("liveness head sequence/hash is invalid")
    return {
        "sequence": sequence,
        "checkpoint_sha256": checkpoint_hash,
        "boot_id": str(head.get("boot_id", "")),
        "stage": str(head.get("stage", "")),
        "status": str(head.get("status", "")),
        "observed_at": str(head.get("observed_at", "")),
    }


def _incident(severity: str, code: str, detail: str, **context: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, "context": context}


def classify_remote_liveness(
    cfg: RemoteWatcherConfig,
    state: dict[str, Any],
    verification: dict[str, Any],
    head: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    if not verification.get("ok"):
        issues = [str(item) for item in verification.get("issues", [])]
        code = str(verification.get("code", "RUNTIME_LIVENESS_INVALID"))
        joined = ";".join(issues[:8])
        if code == "RUNTIME_LIVENESS_MISSING":
            if state.get("ever_seen"):
                return _incident("CRITICAL", "WATCHER_MACHINE_DISAPPEARED", "previously observed liveness scope no longer has a checkpoint head", issues=issues)
            return _incident("CRITICAL", "WATCHER_LIVENESS_MISSING", "no signed liveness checkpoint has been observed", issues=issues)
        lowered = joined.lower()
        if state.get("ever_seen") and ("filenotfound" in lowered or "no such file" in lowered):
            return _incident("CRITICAL", "WATCHER_REMOTE_SCOPE_UNAVAILABLE", "previously observed remote audit/liveness scope is unavailable", issues=issues)
        return _incident("CRITICAL", "WATCHER_LIVENESS_INTEGRITY", "signed liveness verification failed", issues=issues)

    if head is None:
        return _incident("CRITICAL", "WATCHER_LIVENESS_INTEGRITY", "verified liveness report has no readable head snapshot")
    if verification.get("current_boot_covered") is not True:
        return _incident("CRITICAL", "WATCHER_BOOT_NOT_COVERED", "latest liveness checkpoint does not cover the current remote audit boot")

    sequence = int(head["sequence"])
    checkpoint_hash = str(head["checkpoint_sha256"])
    previous_sequence = int(state.get("last_sequence") or 0)
    previous_hash = str(state.get("last_checkpoint_sha256") or "")
    if previous_sequence > 0 and sequence < previous_sequence:
        return _incident(
            "CRITICAL",
            "WATCHER_LEDGER_REWIND",
            "remote liveness sequence moved backwards from a previously observed head",
            previous_sequence=previous_sequence,
            current_sequence=sequence,
        )
    if previous_sequence > 0 and sequence == previous_sequence and previous_hash and checkpoint_hash != previous_hash:
        return _incident(
            "CRITICAL",
            "WATCHER_LEDGER_MUTATED",
            "remote liveness head changed at an already observed sequence",
            sequence=sequence,
            previous_checkpoint_sha256=previous_hash,
            current_checkpoint_sha256=checkpoint_hash,
        )

    stage = str(verification.get("latest_stage") or head.get("stage") or "").upper()
    status = str(verification.get("latest_status") or head.get("status") or "").upper()
    if stage == "ERROR" or status == "ERROR":
        return _incident("CRITICAL", "WATCHER_RUNTIME_ERROR", "trading runtime published an ERROR checkpoint", stage=stage, status=status)
    if stage == "HALTED" or status in {"CRITICAL", "HALTED"}:
        return _incident("CRITICAL", "WATCHER_RUNTIME_HALTED", "trading runtime published a halted/critical checkpoint", stage=stage, status=status)

    observed_text = str(verification.get("latest_observed_at") or head.get("observed_at") or "")
    try:
        observed = _parse_time(observed_text, "latest_observed_at")
    except Exception as exc:
        return _incident("CRITICAL", "WATCHER_LIVENESS_INTEGRITY", f"latest checkpoint time is invalid: {exc}")
    age = (now - observed).total_seconds()
    if age < -60:
        return _incident("CRITICAL", "WATCHER_CLOCK_ANOMALY", "latest liveness checkpoint is materially in the future", age_seconds=age)
    if age > cfg.critical_after_seconds:
        return _incident("CRITICAL", "WATCHER_HEARTBEAT_STALE", "signed runtime heartbeat exceeded the critical freshness threshold", age_seconds=age)
    if stage == "DEGRADED" or status in {"WARN", "WARNING", "DEGRADED"}:
        return _incident("WARNING", "WATCHER_RUNTIME_DEGRADED", "trading runtime published a degraded/warning checkpoint", stage=stage, status=status, age_seconds=age)
    if age > cfg.warning_after_seconds:
        return _incident("WARNING", "WATCHER_HEARTBEAT_LATE", "signed runtime heartbeat exceeded the warning freshness threshold", age_seconds=age)
    return None


def _rank(severity: str) -> int:
    return {"INFO": 0, "RECOVERY": 0, "WARNING": 1, "CRITICAL": 2}.get(str(severity).upper(), 0)


def _repeat_seconds(cfg: RemoteWatcherConfig, severity: str) -> float:
    return cfg.repeat_critical_seconds if str(severity).upper() == "CRITICAL" else cfg.repeat_warning_seconds


def _update_active_incident(
    cfg: RemoteWatcherConfig,
    active: dict[str, Any] | None,
    incident: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    same_code = isinstance(active, dict) and active.get("code") == incident["code"]
    if not same_code:
        return {
            "severity": incident["severity"],
            "code": incident["code"],
            "detail": incident["detail"],
            "first_seen_at": _iso(now),
            "last_seen_at": _iso(now),
            "last_alert_at": _iso(now),
            "occurrences": 1,
        }, True

    previous_severity = str(active.get("severity", "WARNING"))
    last_alert_text = str(active.get("last_alert_at") or "")
    should_alert = _rank(incident["severity"]) > _rank(previous_severity)
    if not should_alert:
        if not last_alert_text:
            should_alert = True
        else:
            try:
                elapsed = (now - _parse_time(last_alert_text, "last_alert_at")).total_seconds()
                should_alert = elapsed >= _repeat_seconds(cfg, incident["severity"])
            except Exception:
                should_alert = True
    updated = dict(active)
    updated.update(
        {
            "severity": incident["severity"],
            "code": incident["code"],
            "detail": incident["detail"],
            "last_seen_at": _iso(now),
            "occurrences": int(active.get("occurrences", 0)) + 1,
        }
    )
    if should_alert:
        updated["last_alert_at"] = _iso(now)
    return updated, should_alert


def _result_context(verification: dict[str, Any], head: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "verification_code": verification.get("code"),
        "verification_issues": verification.get("issues", []),
        "latest_boot_id": verification.get("latest_boot_id"),
        "current_audit_boot_id": verification.get("current_audit_boot_id"),
        "latest_stage": verification.get("latest_stage"),
        "latest_status": verification.get("latest_status"),
        "latest_observed_at": verification.get("latest_observed_at"),
        "head": head,
    }


def check_once(
    cfg: RemoteWatcherConfig,
    *,
    root: str | Path = ".",
    now: datetime | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    _validate_config(cfg, root_path)
    moment = (now or _now()).astimezone(timezone.utc)
    state_path = _resolve(root_path, cfg.state_path)
    dispatcher = WatcherAlertDispatcher(cfg, root_path)

    try:
        state = _load_state(state_path, cfg)
    except Exception as exc:
        detail = str(exc)
        delivery = dispatcher.emit(
            severity="CRITICAL",
            code="WATCHER_STATE_CORRUPT",
            detail=detail,
            context={},
            observed_at=moment,
        )
        return {
            "ok": False,
            "status": "CRITICAL",
            "code": "WATCHER_STATE_CORRUPT",
            "detail": detail,
            "alert_emitted": True,
            "delivery": delivery,
        }

    verification = verify_liveness_ledger(
        root_path,
        environment_id=cfg.environment_id,
        machine_id=cfg.machine_id,
        replica_root=cfg.replica_root,
        replica_subdir=cfg.replica_subdir,
        trust_store_path=cfg.trust_store_path,
        max_age_seconds=None,
        require_current_boot=True,
        now=moment,
    )

    head: dict[str, Any] | None = None
    if verification.get("ok"):
        try:
            head = _head_snapshot(verification)
        except Exception as exc:
            verification = dict(verification)
            verification["ok"] = False
            verification["code"] = "RUNTIME_LIVENESS_INVALID"
            verification["issues"] = list(verification.get("issues", [])) + [f"watcher_head:{type(exc).__name__}:{exc}"]

    incident = classify_remote_liveness(cfg, state, verification, head, now=moment)
    delivery: dict[str, Any] | None = None
    alert_emitted = False
    active = state.get("active_incident") if isinstance(state.get("active_incident"), dict) else None
    result_status = "OK"
    result_code = "WATCHER_OK"
    result_detail = "remote liveness is healthy and current"

    if incident is not None:
        updated_active, should_alert = _update_active_incident(cfg, active, incident, now=moment)
        state["active_incident"] = updated_active
        result_status = incident["severity"]
        result_code = incident["code"]
        result_detail = incident["detail"]
        if should_alert:
            delivery = dispatcher.emit(
                severity=incident["severity"],
                code=incident["code"],
                detail=incident["detail"],
                context={**incident.get("context", {}), **_result_context(verification, head)},
                observed_at=moment,
            )
            alert_emitted = True
    elif active is not None:
        result_status = "RECOVERED"
        result_code = "WATCHER_RECOVERED"
        result_detail = f"remote liveness recovered from {active.get('code')}"
        if cfg.recovery_notifications:
            delivery = dispatcher.emit(
                severity="RECOVERY",
                code="WATCHER_RECOVERED",
                detail=result_detail,
                context={"previous_incident": active, **_result_context(verification, head)},
                observed_at=moment,
            )
            alert_emitted = True
        state["active_incident"] = None

    suspicious_head = result_code in {"WATCHER_LEDGER_REWIND", "WATCHER_LEDGER_MUTATED"}
    if verification.get("ok") and head is not None and not suspicious_head:
        state["ever_seen"] = True
        state["last_sequence"] = int(head["sequence"])
        state["last_checkpoint_sha256"] = head["checkpoint_sha256"]
        state["last_boot_id"] = verification.get("latest_boot_id")
        state["last_observed_at"] = verification.get("latest_observed_at")

    state["last_check_at"] = _iso(moment)
    state["last_result"] = {"status": result_status, "code": result_code, "detail": result_detail}
    _atomic_json(state_path, state)

    return {
        "ok": result_status in {"OK", "RECOVERED"},
        "status": result_status,
        "code": result_code,
        "detail": result_detail,
        "observer_id": cfg.observer_id,
        "environment_id": cfg.environment_id,
        "machine_id": cfg.machine_id,
        "alert_emitted": alert_emitted,
        "delivery": delivery,
        "verification": verification,
        "head": head,
        "state_path": str(state_path),
        "outbox_path": str(dispatcher.outbox),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 34 independent remote liveness watcher")
    parser.add_argument("--config", default="remote_watcher.yaml")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    cfg = load_watcher_config(args.config, root=args.root)
    result = check_once(cfg, root=args.root)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    status = str(result.get("status", "CRITICAL")).upper()
    if status == "CRITICAL":
        raise SystemExit(2)
    if status == "WARNING":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
