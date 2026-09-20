from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.remote_watcher as watcher


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _cfg(tmp_path: Path, **overrides) -> watcher.RemoteWatcherConfig:
    values = {
        "observer_id": "watcher-pc-01",
        "environment_id": "prod-bkk-01",
        "machine_id": "trader-pc-01",
        "replica_root": str(tmp_path / "remote"),
        "state_path": "runtime/watcher_state.json",
        "outbox_path": "runtime/watcher_alerts.jsonl",
        "warning_after_seconds": 120.0,
        "critical_after_seconds": 300.0,
        "repeat_warning_seconds": 1800.0,
        "repeat_critical_seconds": 600.0,
    }
    values.update(overrides)
    return watcher.RemoteWatcherConfig(**values)


def _scope(tmp_path: Path) -> Path:
    scope = tmp_path / "remote" / "scope" / "liveness"
    scope.mkdir(parents=True, exist_ok=True)
    return scope


def _write_head(
    scope: Path,
    *,
    sequence: int,
    checkpoint_hash: str,
    boot_id: str = "boot001",
    stage: str = "READY",
    status: str = "OK",
    observed_at: datetime = NOW,
) -> None:
    (scope / "head.json").write_text(
        json.dumps(
            {
                "sequence": sequence,
                "checkpoint_manifest_sha256": checkpoint_hash,
                "boot_id": boot_id,
                "stage": stage,
                "status": status,
                "observed_at": observed_at.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _report(
    scope: Path,
    *,
    sequence: int = 1,
    observed_at: datetime = NOW,
    stage: str = "READY",
    status: str = "OK",
    boot_id: str = "boot001",
) -> dict:
    return {
        "ok": True,
        "code": "RUNTIME_LIVENESS_VALID",
        "environment_id": "prod-bkk-01",
        "machine_id": "trader-pc-01",
        "entries": sequence,
        "sequence": sequence,
        "latest_boot_id": boot_id,
        "current_audit_boot_id": boot_id,
        "current_boot_covered": True,
        "latest_stage": stage,
        "latest_status": status,
        "latest_observed_at": observed_at.isoformat(),
        "anchored_history": False,
        "issues": [],
        "scope": str(scope),
    }


def _install_report(monkeypatch: pytest.MonkeyPatch, report: dict) -> None:
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: dict(report))


def _outbox_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "runtime" / "watcher_alerts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_healthy_checkpoint_establishes_local_observation_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64)
    _install_report(monkeypatch, _report(scope))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["ok"] is True
    assert result["status"] == "OK"
    assert result["alert_emitted"] is False
    state = json.loads((tmp_path / "runtime" / "watcher_state.json").read_text(encoding="utf-8"))
    assert state["ever_seen"] is True
    assert state["last_sequence"] == 1
    assert state["last_checkpoint_sha256"] == "a" * 64


def test_late_heartbeat_is_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    observed = NOW - timedelta(seconds=121)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=observed)
    _install_report(monkeypatch, _report(scope, observed_at=observed))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "WARNING"
    assert result["code"] == "WATCHER_HEARTBEAT_LATE"
    assert len(_outbox_lines(tmp_path)) == 1


def test_stale_heartbeat_escalates_to_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    observed = NOW - timedelta(seconds=301)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=observed)
    _install_report(monkeypatch, _report(scope, observed_at=observed))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_HEARTBEAT_STALE"


def test_degraded_checkpoint_is_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    _write_head(scope, sequence=3, checkpoint_hash="a" * 64, stage="DEGRADED", status="WARN")
    _install_report(monkeypatch, _report(scope, sequence=3, stage="DEGRADED", status="WARN"))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "WARNING"
    assert result["code"] == "WATCHER_RUNTIME_DEGRADED"


def test_halted_checkpoint_is_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    _write_head(scope, sequence=3, checkpoint_hash="a" * 64, stage="HALTED", status="CRITICAL")
    _install_report(monkeypatch, _report(scope, sequence=3, stage="HALTED", status="CRITICAL"))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_RUNTIME_HALTED"


def test_duplicate_warning_is_suppressed_until_repeat_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    observed = NOW - timedelta(seconds=150)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=observed)
    _install_report(monkeypatch, _report(scope, observed_at=observed))
    cfg = _cfg(tmp_path)
    first = watcher.check_once(cfg, root=tmp_path, now=NOW)
    second = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=60))
    assert first["alert_emitted"] is True
    assert second["status"] == "WARNING"
    assert second["alert_emitted"] is False
    assert len(_outbox_lines(tmp_path)) == 1


def test_warning_to_critical_escalation_alerts_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    observed = NOW - timedelta(seconds=150)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=observed)
    report = _report(scope, observed_at=observed)
    _install_report(monkeypatch, report)
    cfg = _cfg(tmp_path)
    warning = watcher.check_once(cfg, root=tmp_path, now=NOW)
    critical = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=200))
    assert warning["code"] == "WATCHER_HEARTBEAT_LATE"
    assert critical["code"] == "WATCHER_HEARTBEAT_STALE"
    assert critical["alert_emitted"] is True
    assert len(_outbox_lines(tmp_path)) == 2


def test_recovery_alert_is_emitted_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    late = NOW - timedelta(seconds=150)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=late)
    current = {"report": _report(scope, observed_at=late)}
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: dict(current["report"]))
    cfg = _cfg(tmp_path)
    watcher.check_once(cfg, root=tmp_path, now=NOW)

    _write_head(scope, sequence=2, checkpoint_hash="b" * 64, observed_at=NOW + timedelta(seconds=30))
    current["report"] = _report(scope, sequence=2, observed_at=NOW + timedelta(seconds=30))
    recovered = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=30))
    steady = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=60))
    assert recovered["status"] == "RECOVERED"
    assert recovered["code"] == "WATCHER_RECOVERED"
    assert steady["status"] == "OK"
    assert len(_outbox_lines(tmp_path)) == 2
    assert _outbox_lines(tmp_path)[-1]["severity"] == "RECOVERY"


def test_remote_sequence_rewind_is_critical_and_does_not_replace_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    current = {"report": _report(scope, sequence=5)}
    _write_head(scope, sequence=5, checkpoint_hash="a" * 64)
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: dict(current["report"]))
    cfg = _cfg(tmp_path)
    watcher.check_once(cfg, root=tmp_path, now=NOW)

    _write_head(scope, sequence=4, checkpoint_hash="b" * 64, observed_at=NOW + timedelta(seconds=30))
    current["report"] = _report(scope, sequence=4, observed_at=NOW + timedelta(seconds=30))
    result = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=30))
    assert result["code"] == "WATCHER_LEDGER_REWIND"
    state = json.loads((tmp_path / "runtime" / "watcher_state.json").read_text(encoding="utf-8"))
    assert state["last_sequence"] == 5
    assert state["last_checkpoint_sha256"] == "a" * 64


def test_same_sequence_with_different_hash_is_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    current = {"report": _report(scope, sequence=5)}
    _write_head(scope, sequence=5, checkpoint_hash="a" * 64)
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: dict(current["report"]))
    cfg = _cfg(tmp_path)
    watcher.check_once(cfg, root=tmp_path, now=NOW)

    _write_head(scope, sequence=5, checkpoint_hash="b" * 64, observed_at=NOW + timedelta(seconds=30))
    current["report"] = _report(scope, sequence=5, observed_at=NOW + timedelta(seconds=30))
    result = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=30))
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_LEDGER_MUTATED"


def test_missing_liveness_after_prior_observation_is_machine_disappearance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    current = {"report": _report(scope)}
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64)
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: dict(current["report"]))
    cfg = _cfg(tmp_path)
    watcher.check_once(cfg, root=tmp_path, now=NOW)
    current["report"] = {"ok": False, "code": "RUNTIME_LIVENESS_MISSING", "issues": ["head:missing"]}
    result = watcher.check_once(cfg, root=tmp_path, now=NOW + timedelta(seconds=30))
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_MACHINE_DISAPPEARED"


def test_invalid_signed_chain_is_integrity_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_report(
        monkeypatch,
        {"ok": False, "code": "RUNTIME_LIVENESS_INVALID", "issues": ["entry:2:signature:LIVENESS_SIGNATURE_INVALID"]},
    )
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_LIVENESS_INTEGRITY"


def test_corrupt_local_watcher_state_is_not_silently_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "runtime" / "watcher_state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(watcher, "verify_liveness_ledger", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("verify should not run")))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["status"] == "CRITICAL"
    assert result["code"] == "WATCHER_STATE_CORRUPT"
    assert state.read_text(encoding="utf-8") == "{broken"
    assert len(_outbox_lines(tmp_path)) == 1


def test_webhook_failure_still_records_local_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(tmp_path)
    observed = NOW - timedelta(seconds=150)
    _write_head(scope, sequence=1, checkpoint_hash="a" * 64, observed_at=observed)
    _install_report(monkeypatch, _report(scope, observed_at=observed))
    monkeypatch.setenv(watcher.WATCHER_WEBHOOK_ENV, "https://alerts.invalid/hook")
    monkeypatch.setattr(watcher.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    result = watcher.check_once(_cfg(tmp_path), root=tmp_path, now=NOW)
    assert result["alert_emitted"] is True
    assert result["delivery"]["outbox_recorded"] is True
    assert result["delivery"]["webhook_configured"] is True
    assert result["delivery"]["webhook_delivered"] is False
    assert len(_outbox_lines(tmp_path)) == 1


def test_observer_identity_must_differ_from_trading_machine(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, observer_id="trader-pc-01")
    with pytest.raises(ValueError, match="observer_id must differ"):
        watcher.check_once(cfg, root=tmp_path, now=NOW)
