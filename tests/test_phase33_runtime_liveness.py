from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest

import src.production as production
import src.runtime_liveness as liveness
from src.audit_ledger import (
    DEFAULT_REPLICA_SUBDIR,
    LEDGER_ENTRY_FORMAT,
    LEDGER_ENTRY_VERSION,
    LEDGER_HEAD_FORMAT,
    LEDGER_HEAD_VERSION,
)
from src.key_policy import make_key_record, write_trust_store
from src.release import generate_keypair
from src.runtime_boot import BOOT_FORMAT, BOOT_VERSION


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "runtime_boot") -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "project"
    remote = tmp_path / "remote"
    (root / "release" / "keys").mkdir(parents=True)
    private = tmp_path / "runtime-private.pem"
    public = root / "release" / "keys" / "runtime-public.pem"
    generate_keypair(private, public)
    record = make_key_record(
        root,
        key_id="runtime-boot-2026-q4",
        role=role,
        public_key_path=public,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
    )
    write_trust_store(root, [record])
    scope = remote / DEFAULT_REPLICA_SUBDIR / "prod-bkk-01" / "trader-pc-01"
    (scope / "entries").mkdir(parents=True)

    def audit_report(*args, **kwargs):
        head = json.loads((scope / "head.json").read_text(encoding="utf-8"))
        entries = [item for item in (scope / "entries").iterdir() if item.is_dir()]
        return {
            "ok": True,
            "code": "AUDIT_LEDGER_VALID",
            "scope": str(scope),
            "sequence": int(head["sequence"]),
            "entries": len(entries),
            "head_boot_id": head["boot_id"],
            "issues": [],
        }

    monkeypatch.setattr(liveness, "verify_audit_ledger", audit_report)
    return root, private, remote, scope


def _write_audit_boot(
    root: Path,
    scope: Path,
    *,
    boot_id: str,
    sequence: int,
    mode: str,
    previous_boot_hash: str | None = None,
    set_local: bool = True,
) -> str:
    boot = root / "runtime" / "runtime_boot_receipt.json"
    document = {
        "version": BOOT_VERSION,
        "format": BOOT_FORMAT,
        "status": "BOOT_ATTESTED",
        "boot_id": boot_id,
        "created_at": "2026-09-20T12:00:00+00:00",
        "environment": {"id": "prod-bkk-01"},
        "machine": {"id": "trader-pc-01"},
        "release": {"source_commit": "a" * 40, "release_id": "phase33-release"},
        "chain": {"previous_boot_receipt_sha256": previous_boot_hash},
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    boot_hash = hashlib.sha256(payload).hexdigest()
    if set_local:
        boot.parent.mkdir(parents=True, exist_ok=True)
        boot.write_bytes(payload)

    entry_name = f"{sequence:08d}-{boot_id}"
    entry = scope / "entries" / entry_name
    manifest = entry / "ledger_entry.json"
    audit_doc = {
        "version": LEDGER_ENTRY_VERSION,
        "format": LEDGER_ENTRY_FORMAT,
        "sequence": sequence,
        "entry_id": boot_id,
        "environment_id": "prod-bkk-01",
        "machine_id": "trader-pc-01",
        "source_commit": "a" * 40,
        "release_id": "phase33-release",
        "chain": {"mode": mode},
        "evidence": {"boot_receipt": {"path": "evidence/runtime/runtime_boot_receipt.json", "size": len(payload), "sha256": boot_hash}},
    }
    _json(manifest, audit_doc)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    _json(
        scope / "head.json",
        {
            "version": LEDGER_HEAD_VERSION,
            "format": LEDGER_HEAD_FORMAT,
            "environment_id": "prod-bkk-01",
            "machine_id": "trader-pc-01",
            "sequence": sequence,
            "entry_dir": entry_name,
            "boot_id": boot_id,
            "boot_receipt_sha256": boot_hash,
            "entry_manifest_sha256": manifest_hash,
            "updated_at": "2026-09-20T12:00:00+00:00",
        },
    )
    return boot_hash


def _append(root: Path, private: Path, remote: Path, *, stage: str = "READY", cycle: int = 1, now: datetime | None = None) -> dict:
    return liveness.append_liveness_checkpoint(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        stage=stage,
        cycle=cycle,
        health={"status": "OK", "market_state": "OPEN", "halted": False},
        private_key_path=private,
        replica_root=remote,
        now=now or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def _verify(root: Path, remote: Path, *, now: datetime | None = None, max_age: float | None = None, current: bool = False) -> dict:
    return liveness.verify_liveness_ledger(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        replica_root=remote,
        max_age_seconds=max_age,
        require_current_boot=current,
        now=now or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def test_first_checkpoint_genesis_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    assert result["code"] == "RUNTIME_LIVENESS_APPENDED"
    assert result["chain_mode"] == "GENESIS"
    report = _verify(root, remote, current=True)
    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["current_boot_covered"] is True


def test_second_checkpoint_preserves_checkpoint_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote, cycle=1)
    second = _append(root, private, remote, stage="POST_CYCLE", cycle=2, now=datetime(2026, 9, 20, 12, 0, 30, tzinfo=timezone.utc))
    assert second["sequence"] == 2
    assert second["chain_mode"] == "CONTINUATION"
    assert _verify(root, remote)["ok"] is True


def test_boot_transition_is_anchored_to_new_audit_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote, cycle=1)
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash)
    result = _append(root, private, remote, cycle=1, now=datetime(2026, 9, 20, 12, 1, tzinfo=timezone.utc))
    assert result["sequence"] == 2
    report = _verify(root, remote, current=True)
    assert report["ok"] is True
    assert report["latest_boot_id"] == "boot002"


def test_first_liveness_checkpoint_can_anchor_existing_audit_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS", set_local=False)
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash)
    result = _append(root, private, remote)
    assert result["chain_mode"] == "ANCHOR"
    assert _verify(root, remote)["anchored_history"] is True


def test_stale_latest_checkpoint_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _append(root, private, remote, now=stamp)
    report = _verify(root, remote, now=stamp + timedelta(minutes=3), max_age=60)
    assert report["ok"] is False
    assert "freshness:stale" in report["issues"]


def test_checkpoint_manifest_tamper_breaks_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    entry = next((Path(result["scope"]) / "entries").iterdir())
    checkpoint = entry / "checkpoint.json"
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    document["status"] = "EVIL"
    _json(checkpoint, document)
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("signature" in issue for issue in report["issues"])


def test_missing_checkpoint_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    entry = next((Path(result["scope"]) / "entries").iterdir())
    for item in entry.iterdir():
        item.unlink()
    entry.rmdir()
    report = _verify(root, remote)
    assert report["ok"] is False
    assert "entries:count" in report["issues"]


def test_audit_anchor_tamper_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote)
    manifest = next((scope / "entries").iterdir()) / "ledger_entry.json"
    audit_doc = json.loads(manifest.read_text(encoding="utf-8"))
    audit_doc["release_id"] = "tampered"
    _json(manifest, audit_doc)
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("audit_anchor:manifest_sha256" in issue or "release_id" in issue for issue in report["issues"])


def test_old_process_cannot_publish_after_new_boot_becomes_audit_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash, set_local=False)
    with pytest.raises(RuntimeError, match="LIVENESS_BOOT_NOT_AUDIT_HEAD"):
        _append(root, private, remote)


def test_wrong_key_role_cannot_sign_liveness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch, role="release")
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    with pytest.raises(Exception, match="SIGNING_KEY_NOT_TRUSTED"):
        _append(root, private, remote)


def test_production_liveness_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(liveness.REQUIRE_ENV, "1")
    monkeypatch.setattr(liveness, "append_liveness_from_env", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote unavailable")))
    with pytest.raises(production.RuntimeLivenessError, match="remote unavailable"):
        production._publish_runtime_liveness(
            stage="READY",
            cycle=1,
            symbol="EURUSD",
            report=None,
            snapshot=None,
            production_halt=None,
        )
