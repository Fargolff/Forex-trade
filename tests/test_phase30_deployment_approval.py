from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import src.deployment_approval as approval
from src.key_policy import DEFAULT_TRUST_STORE, make_key_record, write_trust_store
from src.release import generate_keypair


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "deployment_approval") -> tuple[Path, Path]:
    root = tmp_path / "project"
    (root / "release" / "keys").mkdir(parents=True)
    receipt = root / "release" / "release_receipt.json"
    receipt_signature = root / "release" / "release_receipt.signature.json"
    receipt.write_text(json.dumps({
        "source_commit": "a" * 40,
        "release_id": "phase30-release",
        "release_key_id": "release-key",
        "runtime": {"design_fingerprint": "design-123"},
    }, sort_keys=True) + "\n", encoding="utf-8")
    receipt_signature.write_text("{}\n", encoding="utf-8")

    keys = tmp_path / "approval-keys"
    keys.mkdir()
    private = keys / "approval-private.pem"
    public = root / "release" / "keys" / "approval-public.pem"
    generate_keypair(private, public)
    record = make_key_record(
        root,
        key_id="approval-2026-q4",
        role=role,
        public_key_path=public,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
    )
    write_trust_store(root, [record])

    def valid_release(*args, **kwargs):
        expected_commit = kwargs.get("expected_source_commit")
        expected_release = kwargs.get("expected_release_id")
        ok = (expected_commit in (None, "a" * 40)) and (expected_release in (None, "phase30-release"))
        return {
            "ok": ok,
            "issues": [] if ok else ["anti_rollback"],
            "source_commit": "a" * 40,
            "release_id": "phase30-release",
        }

    monkeypatch.setattr(approval, "verify_release_receipt", valid_release)
    return root, private


def _prepare(root: Path) -> None:
    approval.prepare_deployment_approval(
        root,
        environment_id="prod-bkk-01",
        expected_source_commit="a" * 40,
        expected_release_id="phase30-release",
    )


def _approve(root: Path, private: Path, *, now: datetime | None = None, valid_hours: float = 24) -> None:
    approval.approve_deployment(
        root,
        private_key_path=private,
        valid_for_hours=valid_hours,
        now=now,
    )


def test_verified_to_approved_to_deployable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    candidate = json.loads((root / approval.DEFAULT_APPROVAL).read_text(encoding="utf-8"))
    assert candidate["status"] == "VERIFIED"
    _approve(root, private)
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-01")
    assert report["ok"] is True
    assert report["stage"] == "DEPLOYABLE"
    assert report["approval_key_id"] == "approval-2026-q4"


def test_environment_binding_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    _approve(root, private)
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-02")
    assert report["ok"] is False
    assert "environment:id" in report["issues"]


def test_release_receipt_tamper_invalidates_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    _approve(root, private)
    with (root / approval.DEFAULT_RECEIPT).open("a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-01")
    assert report["ok"] is False
    assert "release:receipt:size" in report["issues"] or "release:receipt:sha256" in report["issues"]


def test_approval_expiry_blocks_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    signed_at = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    _approve(root, private, now=signed_at, valid_hours=1)
    report = approval.verify_deployment_approval(
        root,
        environment_id="prod-bkk-01",
        now=signed_at + timedelta(hours=2),
    )
    assert report["ok"] is False
    assert "approval:expired" in report["issues"]


def test_revoked_approval_key_invalidates_historical_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    _approve(root, private)
    store_path = root / DEFAULT_TRUST_STORE
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store["keys"][0]["revoked"] = True
    store["keys"][0]["revoked_at"] = datetime.now(timezone.utc).isoformat()
    store["keys"][0]["revocation_reason"] = "compromise-test"
    store_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-01")
    assert report["ok"] is False
    assert any("APPROVAL_SIGNATURE_ERROR" in issue and "REVOKED" in issue for issue in report["issues"])


def test_release_role_key_cannot_approve_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch, role="release")
    _prepare(root)
    with pytest.raises(Exception, match="SIGNING_KEY_NOT_TRUSTED"):
        _approve(root, private)


def test_approval_validity_is_capped_at_seven_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    with pytest.raises(ValueError, match="valid_for_hours"):
        _approve(root, private, valid_hours=169)


def test_unsigned_candidate_is_not_deployable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-01")
    assert report["ok"] is False
    assert report["stage"] == "VERIFIED"
    assert "approval_signature:missing" in report["issues"]


def test_approval_document_tamper_breaks_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    _prepare(root)
    _approve(root, private)
    path = root / approval.DEFAULT_APPROVAL
    document = json.loads(path.read_text(encoding="utf-8"))
    document["environment"]["id"] = "prod-bkk-02"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = approval.verify_deployment_approval(root, environment_id="prod-bkk-02")
    assert report["ok"] is False
    assert "approval_signature:APPROVAL_HASH_MISMATCH" in report["issues"]


def test_invalid_environment_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="environment_id"):
        approval.prepare_deployment_approval(root, environment_id="prod bangkok")
