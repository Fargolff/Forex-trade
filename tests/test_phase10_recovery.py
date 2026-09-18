from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import zipfile

from src.recovery import (
    create_backup,
    create_deployment_manifest,
    restore_backup,
    verify_backup,
    verify_deployment_manifest,
)
from src.soak import SoakConfig, run_soak
from src.watchdog import WatchdogConfig, evaluate_heartbeat, watchdog_once


def test_watchdog_fresh_stale_degraded_and_future():
    now = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)
    fresh = evaluate_heartbeat(
        {"updated_at": now.isoformat(), "status": "OK", "incidents": []},
        max_age_seconds=120,
        max_future_seconds=10,
        now=now,
    )
    assert fresh["status"] == "OK"

    stale = evaluate_heartbeat(
        {"updated_at": (now - timedelta(seconds=121)).isoformat(), "status": "OK", "incidents": []},
        max_age_seconds=120,
        max_future_seconds=10,
        now=now,
    )
    assert stale["code"] == "HEARTBEAT_TIMEOUT"

    degraded = evaluate_heartbeat(
        {"updated_at": now.isoformat(), "status": "DEGRADED", "incidents": []},
        max_age_seconds=120,
        max_future_seconds=10,
        now=now,
    )
    assert degraded["status"] == "WARN"

    future = evaluate_heartbeat(
        {"updated_at": (now + timedelta(seconds=11)).isoformat(), "status": "OK", "incidents": []},
        max_age_seconds=120,
        max_future_seconds=10,
        now=now,
    )
    assert future["code"] == "HEARTBEAT_CLOCK_AHEAD"


def test_watchdog_production_halt_overrides_fresh_heartbeat(tmp_path):
    now = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)
    heartbeat = tmp_path / "heartbeat.json"
    halt = tmp_path / "halt.json"
    heartbeat.write_text(json.dumps({"updated_at": now.isoformat(), "status": "OK", "incidents": []}), encoding="utf-8")
    halt.write_text(json.dumps({"halted": True, "code": "TEST", "detail": "operator review"}), encoding="utf-8")
    cfg = WatchdogConfig(
        heartbeat_path=str(heartbeat),
        production_halt_path=str(halt),
        state_path=str(tmp_path / "state.json"),
        alert_outbox_path=str(tmp_path / "alerts.jsonl"),
        alert_state_path=str(tmp_path / "alert-state.json"),
    )
    result = watchdog_once(cfg, now=now)
    assert result["status"] == "CRITICAL"
    assert result["code"] == "PRODUCTION_HALT_PRESENT"


def test_backup_verify_restore_and_tamper_detection(tmp_path):
    root = tmp_path / "root"
    source = root / "runtime" / "live_state.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"halted": false}', encoding="utf-8")
    archive = tmp_path / "backup.zip"

    create_backup(root, archive, paths=["runtime/live_state.json"])
    assert verify_backup(archive)["ok"] is True

    restore_target = tmp_path / "restore"
    restored = restore_backup(archive, restore_target)
    assert restored["ok"] is True
    assert (restore_target / "runtime" / "live_state.json").read_text(encoding="utf-8") == '{"halted": false}'

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "r") as source_zip, zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
        for name in source_zip.namelist():
            payload = source_zip.read(name)
            if name == "runtime/live_state.json":
                payload = b'{"halted": true}'
            target_zip.writestr(name, payload)
    check = verify_backup(tampered)
    assert check["ok"] is False
    assert any(item.startswith("sha256:") for item in check["issues"])


def test_deployment_manifest_detects_code_drift(tmp_path):
    root = tmp_path / "deploy"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    create_deployment_manifest(root, manifest, patterns=["src/**/*.py"])
    assert verify_deployment_manifest(root, manifest)["ok"] is True

    source.write_text("VALUE = 2\n", encoding="utf-8")
    result = verify_deployment_manifest(root, manifest)
    assert result["ok"] is False
    assert result["issues"] == ["sha256:src/engine.py"]


def test_operational_soak_failure_paths_recover():
    result = run_soak(SoakConfig(cycles=300, step_seconds=30, max_heartbeat_age_seconds=120, max_future_seconds=10))
    assert result["ok"] is True
    assert result["status_counts"]["CRITICAL"] > 0
    assert result["status_counts"]["WARN"] > 0
    assert all(result["expectations"].values())
