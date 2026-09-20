from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

import src.audit_ledger as ledger
from src.key_policy import make_key_record, write_trust_store
from src.release import generate_keypair, load_private_key, public_key_fingerprint
from src.runtime_boot import BOOT_FORMAT, BOOT_VERSION


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _descriptor(path: Path, relative: str) -> dict:
    return {"path": relative, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "runtime_boot") -> tuple[Path, Path, Path]:
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

    release_receipt = root / "release" / "release_receipt.json"
    release_sig = root / "release" / "release_receipt.signature.json"
    approval = root / "release" / "deployment_approval.json"
    approval_sig = root / "release" / "deployment_approval.signature.json"
    _json(release_receipt, {"source_commit": "a" * 40, "release_id": "phase32-release", "runtime": {"design_fingerprint": "design-32"}})
    _json(release_sig, {"proof": "release-signature"})
    _json(approval, {"status": "APPROVED", "environment": {"id": "prod-bkk-01"}})
    _json(approval_sig, {"proof": "approval-signature"})

    monkeypatch.setattr(ledger, "verify_runtime_boot_attestation", lambda *args, **kwargs: {"ok": True, "code": "RUNTIME_BOOT_ATTESTATION_VALID", "issues": []})
    return root, private, remote


def _write_boot(root: Path, private: Path, *, boot_id: str, previous: str | None = None, created_at: str = "2026-09-20T12:00:00+00:00") -> str:
    release_receipt = root / "release" / "release_receipt.json"
    release_sig = root / "release" / "release_receipt.signature.json"
    approval = root / "release" / "deployment_approval.json"
    approval_sig = root / "release" / "deployment_approval.signature.json"
    boot = root / "runtime" / "runtime_boot_receipt.json"
    boot_sig = root / "runtime" / "runtime_boot_receipt.signature.json"
    document = {
        "version": BOOT_VERSION,
        "format": BOOT_FORMAT,
        "status": "BOOT_ATTESTED",
        "boot_id": boot_id,
        "created_at": created_at,
        "environment": {"id": "prod-bkk-01"},
        "machine": {"id": "trader-pc-01"},
        "release": {
            "source_commit": "a" * 40,
            "release_id": "phase32-release",
            "release_key_id": "release-key",
            "runtime_design_fingerprint": "design-32",
            "receipt": _descriptor(release_receipt, "release/release_receipt.json"),
            "receipt_signature": _descriptor(release_sig, "release/release_receipt.signature.json"),
        },
        "deployment_approval": {
            "approval": _descriptor(approval, "release/deployment_approval.json"),
            "signature": _descriptor(approval_sig, "release/deployment_approval.signature.json"),
            "approval_key_id": "approval-key",
            "valid_until": "2099-01-01T00:00:00+00:00",
        },
        "runtime_boot_key": {"key_id": "runtime-boot-2026-q4"},
        "chain": {"previous_boot_receipt_sha256": previous},
        "verification": {"deployment_approval": True},
    }
    _json(boot, document)
    key = load_private_key(private)
    fingerprint = public_key_fingerprint(key.public_key())
    payload = boot.read_bytes()
    _json(boot_sig, {
        "version": 2,
        "algorithm": "Ed25519",
        "document": BOOT_FORMAT,
        "boot_receipt_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": "runtime-boot-2026-q4",
        "public_key_fingerprint": fingerprint,
        "signed_at": created_at,
        "signature_b64": base64.b64encode(key.sign(payload)).decode("ascii"),
    })
    return hashlib.sha256(payload).hexdigest()


def _append(root: Path, private: Path, remote: Path) -> dict:
    return ledger.append_audit_entry(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        private_key_path=private,
        replica_root=remote,
        now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def _verify(root: Path, remote: Path) -> dict:
    return ledger.verify_audit_ledger(root, environment_id="prod-bkk-01", machine_id="trader-pc-01", replica_root=remote)


def test_first_entry_genesis_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    result = _append(root, private, remote)
    assert result["code"] == "AUDIT_LEDGER_APPENDED"
    assert result["chain_mode"] == "GENESIS"
    report = _verify(root, remote)
    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["anchored_history"] is False


def test_first_entry_can_explicitly_anchor_pre_phase32_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001", previous="f" * 64)
    result = _append(root, private, remote)
    assert result["chain_mode"] == "ANCHOR"
    assert _verify(root, remote)["anchored_history"] is True


def test_second_entry_requires_and_preserves_double_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    first_hash = _write_boot(root, private, boot_id="boot001")
    _append(root, private, remote)
    _write_boot(root, private, boot_id="boot002", previous=first_hash)
    result = _append(root, private, remote)
    assert result["sequence"] == 2
    report = _verify(root, remote)
    assert report["ok"] is True
    assert report["entries"] == 2


def test_same_boot_append_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    _append(root, private, remote)
    again = _append(root, private, remote)
    assert again["code"] == "AUDIT_LEDGER_ALREADY_APPENDED"
    assert _verify(root, remote)["entries"] == 1


def test_chain_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    _append(root, private, remote)
    _write_boot(root, private, boot_id="boot002", previous="0" * 64)
    with pytest.raises(RuntimeError, match="AUDIT_LEDGER_BOOT_CHAIN_MISMATCH"):
        _append(root, private, remote)


def test_remote_evidence_tamper_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    result = _append(root, private, remote)
    scope = Path(result["scope"])
    entry = next((scope / "entries").iterdir())
    with (entry / "evidence" / "release" / "release_receipt.json").open("a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("release_receipt" in issue and "sha256" in issue for issue in report["issues"])


def test_ledger_manifest_tamper_breaks_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    result = _append(root, private, remote)
    entry = next((Path(result["scope"]) / "entries").iterdir())
    manifest = entry / "ledger_entry.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["release_id"] = "evil"
    _json(manifest, doc)
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("manifest_signature" in issue for issue in report["issues"])


def test_wrong_key_role_cannot_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch, role="release")
    _write_boot(root, private, boot_id="boot001")
    with pytest.raises(Exception, match="SIGNING_KEY_NOT_TRUSTED"):
        _append(root, private, remote)


def test_remote_root_inside_project_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, _ = _fixture(tmp_path, monkeypatch)
    _write_boot(root, private, boot_id="boot001")
    with pytest.raises(ValueError, match="outside the project root"):
        ledger.append_audit_entry(root, environment_id="prod-bkk-01", machine_id="trader-pc-01", private_key_path=private, replica_root=root / "remote")


def test_missing_entry_or_rewound_head_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote = _fixture(tmp_path, monkeypatch)
    first_hash = _write_boot(root, private, boot_id="boot001")
    first = _append(root, private, remote)
    _write_boot(root, private, boot_id="boot002", previous=first_hash)
    _append(root, private, remote)
    scope = Path(first["scope"])
    second = sorted((scope / "entries").iterdir())[-1]
    for item in second.rglob("*"):
        if item.is_file():
            item.unlink()
    for item in sorted(second.rglob("*"), reverse=True):
        if item.is_dir():
            item.rmdir()
    second.rmdir()
    report = _verify(root, remote)
    assert report["ok"] is False
    assert "entries:count" in report["issues"]
