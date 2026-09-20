from __future__ import annotations

import json
from pathlib import Path

from src.ci_attestation import (
    DEFAULT_ATTESTATION,
    DEFAULT_PUBLIC_KEY,
    DEFAULT_REPOSITORY,
    DEFAULT_SIGNATURE,
    DEFAULT_WORKFLOW,
    create_ci_attestation,
    sign_ci_attestation,
    verify_ci_attestation,
)
from src.release import generate_keypair


def _fixture(tmp_path: Path, *, commit: str = "1" * 40, ref: str = "refs/heads/main", pytest_status: str = "passed") -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "deploy" / "windows").mkdir(parents=True)
    (root / "deploy" / "windows" / "run.ps1").write_text("Write-Host ok\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "tests.yml").write_text("name: Forex Auto Trader Tests\n", encoding="utf-8")
    (root / "requirements.txt").write_text("pytest>=8\n", encoding="utf-8")
    (root / "config.example.yaml").write_text("mode: backtest\n", encoding="utf-8")
    (root / "production.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "watchdog.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "release").mkdir()

    keys = tmp_path / "ci-keys"
    keys.mkdir()
    private = keys / "ci-private.pem"
    public = root / DEFAULT_PUBLIC_KEY
    generate_keypair(private, public)
    create_ci_attestation(
        root,
        DEFAULT_ATTESTATION,
        source_commit=commit,
        repository=DEFAULT_REPOSITORY,
        workflow=DEFAULT_WORKFLOW,
        run_id="12345",
        run_attempt=1,
        run_url="https://github.com/Fargolff/Forex-trade/actions/runs/12345",
        event_name="push",
        ref=ref,
        pytest_status=pytest_status,
        soak_status="passed",
        soak_cycles=300,
    )
    sign_ci_attestation(root, DEFAULT_ATTESTATION, private, DEFAULT_SIGNATURE)
    return root, private


def test_signed_ci_attestation_verifies_source_commit_and_tree(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is True
    assert report["source_commit"] == "1" * 40
    assert report["run_id"] == "12345"


def test_source_commit_mismatch_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    report = verify_ci_attestation(root, expected_source_commit="2" * 40)
    assert report["ok"] is False
    assert "CI_SOURCE_COMMIT_MISMATCH" in report["issues"]


def test_source_tree_tamper_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    (root / "src" / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_SOURCE_TREE_MISMATCH" in report["issues"]


def test_workflow_tamper_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    (root / ".github" / "workflows" / "tests.yml").write_text("name: tampered\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_WORKFLOW_FILE_MISMATCH" in report["issues"]


def test_non_main_ref_is_not_release_eligible(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, ref="refs/heads/feature")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_REF_NOT_MAIN" in report["issues"]


def test_failed_pytest_attestation_is_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, pytest_status="failed")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_PYTEST_NOT_PASSED" in report["issues"]


def test_wrong_ci_public_key_is_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_private = other / "private.pem"
    other_public = other / "public.pem"
    generate_keypair(other_private, other_public)
    report = verify_ci_attestation(root, public_key_path=other_public, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert report["issues"][0].startswith("signature:")


def test_attestation_document_tamper_breaks_signature(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    path = root / DEFAULT_ATTESTATION
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["workflow"]["run_id"] = "99999"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert report["issues"][0].startswith("signature:")
