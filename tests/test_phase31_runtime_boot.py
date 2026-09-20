from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import src.runtime_boot as boot
from src.key_policy import DEFAULT_TRUST_STORE, make_key_record, write_trust_store
from src.release import generate_keypair


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "runtime_boot") -> tuple[Path, Path]:
    root = tmp_path / "project"
    (root / "release" / "keys").mkdir(parents=True)
    (root / "runtime").mkdir(parents=True)

    release_receipt = root / "release" / "release_receipt.json"
    release_receipt.write_text(json.dumps({
        "source_commit": "a" * 40,
        "release_id": "phase31-release",
        "release_key_id": "release-key",
        "runtime": {"design_fingerprint": "design-31"},
    }, sort_keys=True) + "\n", encoding="utf-8")
    (root / "release" / "release_receipt.signature.json").write_text("{}\n", encoding="utf-8")

    approval = {
        "version": 1,
        "format": "forex-auto-trader-deployment-approval",
        "status": "APPROVED",
        "environment": {"id": "prod-bkk-01"},
        "release": {
            "source_commit": "a" * 40,
            "release_id": "phase31-release",
            "release_key_id": "release-key",
            "runtime_design_fingerprint": "design-31",
        },
        "approval": {
            "key_id": "approval-key",
            "valid_until": "2099-01-01T00:00:00+00:00",
        },
    }
    (root / "release" / "deployment_approval.json").write_text(json.dumps(approval, sort_keys=True) + "\n", encoding="utf-8")
    (root / "release" / "deployment_approval.signature.json").write_text("{}\n", encoding="utf-8")

    keys = tmp_path / "runtime-key"
    keys.mkdir()
    private = keys / "runtime-private.pem"
    public = root / "release" / "keys" / "runtime-public.pem"
    generate_keypair(private, public)
    record = make_key_record(
        root,
        key_id="runtime-2026-q4",
        role=role,
        public_key_path=public,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
    )
    write_trust_store(root, [record])

    def valid_approval(*args, **kwargs):
        expected_commit = kwargs.get("expected_source_commit")
        expected_release = kwargs.get("expected_release_id")
        env = kwargs.get("environment_id")
        ok = env == "prod-bkk-01" and expected_commit in (None, "a" * 40) and expected_release in (None, "phase31-release")
        return {
            "ok": ok,
            "stage": "DEPLOYABLE" if ok else "APPROVED",
            "issues": [] if ok else ["binding"],
            "source_commit": "a" * 40,
            "release_id": "phase31-release",
            "approval_key_id": "approval-key",
            "valid_until": "2099-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(boot, "verify_deployment_approval", valid_approval)
    return root, private


def _create(root: Path, private: Path, now: datetime):
    return boot.create_runtime_boot_attestation(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        private_key_path=private,
        now=now,
    )


def _verify(root: Path, now: datetime, **kwargs):
    return boot.verify_runtime_boot_attestation(
        root,
        environment_id=kwargs.pop("environment_id", "prod-bkk-01"),
        machine_id=kwargs.pop("machine_id", "trader-pc-01"),
        now=now,
        **kwargs,
    )


def test_create_and_verify_fresh_boot_attestation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    created = _create(root, private, now)
    assert created["ok"] is True
    report = _verify(root, now + timedelta(seconds=30))
    assert report["ok"] is True
    assert report["source_commit"] == "a" * 40
    assert report["release_id"] == "phase31-release"
    assert report["runtime_boot_key_id"] == "runtime-2026-q4"


def test_environment_binding_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    report = _verify(root, now, environment_id="prod-bkk-02")
    assert report["ok"] is False
    assert "environment:id" in report["issues"]


def test_machine_binding_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    report = _verify(root, now, machine_id="trader-pc-02")
    assert report["ok"] is False
    assert "machine:id" in report["issues"]


def test_stale_boot_receipt_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    report = _verify(root, now + timedelta(minutes=6))
    assert report["ok"] is False
    assert "boot_receipt:stale" in report["issues"]


def test_approval_tamper_after_boot_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    with (root / boot.DEFAULT_APPROVAL).open("a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    report = _verify(root, now)
    assert report["ok"] is False
    assert any(issue.startswith("deployment_approval:approval:") for issue in report["issues"])


def test_release_receipt_tamper_after_boot_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    with (root / boot.DEFAULT_RECEIPT).open("a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    report = _verify(root, now)
    assert report["ok"] is False
    assert any(issue.startswith("release:receipt:") for issue in report["issues"])


def test_non_runtime_role_key_cannot_sign_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch, role="deployment_approval")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(Exception, match="SIGNING_KEY_NOT_TRUSTED"):
        _create(root, private, now)


def test_revoked_runtime_key_invalidates_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _create(root, private, now)
    store_path = root / DEFAULT_TRUST_STORE
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store["keys"][0]["revoked"] = True
    store["keys"][0]["revoked_at"] = now.isoformat()
    store_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = _verify(root, now)
    assert report["ok"] is False
    assert any("REVOKED" in issue for issue in report["issues"])


def test_restart_creates_hash_chain_and_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    first_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    first = _create(root, private, first_time)
    first_hash = first["receipt_sha256"]
    second = _create(root, private, first_time + timedelta(minutes=1))
    assert second["previous_boot_receipt_sha256"] == first_hash
    archive = root / boot.DEFAULT_BOOT_ARCHIVE_DIR
    assert (archive / f"{first['boot_id']}.json").is_file()
    assert (archive / f"{first['boot_id']}.signature.json").is_file()
    assert (archive / f"{second['boot_id']}.json").is_file()
    assert (archive / f"{second['boot_id']}.signature.json").is_file()


def test_runtime_private_key_must_be_outside_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private = _fixture(tmp_path, monkeypatch)
    inside = root / "runtime-private.pem"
    inside.write_bytes(private.read_bytes())
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="outside the project root"):
        _create(root, inside, now)
