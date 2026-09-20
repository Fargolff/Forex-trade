from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.ci_attestation import DEFAULT_ATTESTATION as DEFAULT_CI_ATTESTATION, DEFAULT_SIGNATURE as DEFAULT_CI_SIGNATURE, create_ci_attestation, sign_ci_attestation
from src.release import generate_keypair
from src.release_ceremony import release_preflight, run_release_ceremony, verify_release_receipt
from src.runtime_provenance import compute_design_fingerprint


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _attest(root: Path, commit: str) -> None:
    create_ci_attestation(
        root,
        DEFAULT_CI_ATTESTATION,
        source_commit=commit,
        run_id="phase28-test-run",
        run_attempt=1,
        run_url="https://github.com/Fargolff/Forex-trade/actions/runs/phase28-test-run",
        event_name="push",
        ref="refs/heads/main",
        soak_cycles=300,
    )
    sign_ci_attestation(root, DEFAULT_CI_ATTESTATION, root.parent / "ci-keys" / "ci-private.pem", DEFAULT_CI_SIGNATURE)


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "deploy" / "windows").mkdir(parents=True)
    (root / "deploy" / "windows" / "run.ps1").write_text("Write-Host ok\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "tests.yml").write_text("name: Forex Auto Trader Tests\n", encoding="utf-8")
    (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
    (root / "config.example.yaml").write_text("mode: backtest\n", encoding="utf-8")
    (root / "production.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "watchdog.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "production.yaml").write_text("{}\n", encoding="utf-8")
    (root / "watchdog.yaml").write_text("{}\n", encoding="utf-8")
    (root / "reconcile.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config.yaml").write_text(
        "\n".join([
            "mode: backtest",
            "live:",
            "  enabled: false",
            "  weights_path: results/portfolio/portfolio_weights.csv",
            "  candidates_path: results/portfolio/portfolio_candidates.csv",
            "",
        ]),
        encoding="utf-8",
    )

    design = [
        {"strategy": "ema_trend", "weight": 0.6, "selected_params": {"fast": 12, "slow": 30}},
        {"strategy": "rsi_reversion", "weight": 0.4, "selected_params": {"period": 14}},
    ]
    fingerprint = compute_design_fingerprint(design)
    _write_csv(
        root / "results" / "portfolio" / "portfolio_weights.csv",
        ["strategy", "weight", "selected_params", "design_fingerprint"],
        [
            {
                "strategy": row["strategy"],
                "weight": row["weight"],
                "selected_params": json.dumps(row["selected_params"], sort_keys=True),
                "design_fingerprint": fingerprint,
            }
            for row in design
        ],
    )
    _write_csv(
        root / "results" / "portfolio" / "portfolio_candidates.csv",
        ["strategy", "selected_params", "design_fingerprint"],
        [
            {
                "strategy": row["strategy"],
                "selected_params": json.dumps(row["selected_params"], sort_keys=True),
                "design_fingerprint": fingerprint,
            }
            for row in design
        ],
    )
    _write_csv(
        root / "results" / "portfolio" / "portfolio_frozen_design.csv",
        ["strategy", "weight", "selected_params"],
        [
            {
                "strategy": row["strategy"],
                "weight": row["weight"],
                "selected_params": json.dumps(row["selected_params"], sort_keys=True),
            }
            for row in design
        ],
    )

    (root / "market_calendar.yaml").write_text(
        "\n".join([
            "version: 1",
            'generated_at: "2026-01-01T00:00:00Z"',
            'valid_through: "2099-12-31"',
            "days: {}",
            "closures: []",
            "",
        ]),
        encoding="utf-8",
    )
    (root / "release").mkdir()
    keys = tmp_path / "offline-keys"
    keys.mkdir()
    private = keys / "release-private.pem"
    public = root / "release" / "forex-release-public.pem"
    generate_keypair(private, public)
    ci_keys = tmp_path / "ci-keys"
    ci_keys.mkdir()
    ci_private = ci_keys / "ci-private.pem"
    ci_public = root / "release" / "forex-ci-attestation-public.pem"
    generate_keypair(ci_private, ci_public)
    _attest(root, "a" * 40)
    return root, private, fingerprint


def _run(root: Path, private: Path, *, overwrite: bool = False) -> dict:
    _attest(root, "a" * 40)
    return run_release_ceremony(
        root,
        source_commit="a" * 40,
        release_id="phase27-test-release",
        private_key_path=private,
        require_calendar=True,
        overwrite=overwrite,
    )


def test_full_ceremony_publishes_signed_receipt_and_verifies_defaults(tmp_path: Path) -> None:
    root, private, fingerprint = _fixture(tmp_path)
    result = _run(root, private)
    assert result["ok"] is True
    assert (root / "release" / "release_manifest.json").is_file()
    assert (root / "release" / "release_signature.json").is_file()
    assert (root / "release" / "forex-release-bundle.zip").is_file()
    assert (root / "release" / "forex-release-bundle.signature.json").is_file()
    assert (root / "release" / "release_receipt.json").is_file()
    assert (root / "release" / "release_receipt.signature.json").is_file()

    receipt = json.loads((root / "release" / "release_receipt.json").read_text(encoding="utf-8"))
    assert receipt["runtime"]["design_fingerprint"] == fingerprint
    assert receipt["artifacts"]["manifest"]["path"] == "release/release_manifest.json"
    assert receipt["artifacts"]["bundle"]["path"] == "release/forex-release-bundle.zip"

    verified = verify_release_receipt(
        root,
        expected_source_commit="a" * 40,
        expected_release_id="phase27-test-release",
    )
    assert verified["ok"] is True
    assert verified["code"] == "RELEASE_RECEIPT_VALID"


def test_preflight_is_read_only_for_release_outputs(tmp_path: Path) -> None:
    root, private, fingerprint = _fixture(tmp_path)
    _attest(root, "b" * 40)
    result = release_preflight(
        root,
        source_commit="b" * 40,
        release_id="preflight-only",
        private_key_path=private,
        require_calendar=True,
    )
    assert result["ok"] is True
    assert result["runtime"]["design_fingerprint"] == fingerprint
    assert not (root / "release" / "release_manifest.json").exists()
    assert not (root / "release" / "release_receipt.json").exists()


def test_runtime_tamper_after_release_invalidates_receipt(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    _run(root, private)
    with (root / "config.yaml").open("a", encoding="utf-8") as handle:
        handle.write("initial_equity: 999999\n")
    verified = verify_release_receipt(root)
    assert verified["ok"] is False
    assert any(issue.startswith("runtime:RUNTIME_FILE_SHA256_MISMATCH:config") for issue in verified["issues"])


def test_bundle_tamper_after_release_invalidates_receipt(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    _run(root, private)
    bundle = root / "release" / "forex-release-bundle.zip"
    bundle.write_bytes(bundle.read_bytes() + b"tamper")
    verified = verify_release_receipt(root)
    assert verified["ok"] is False
    assert "artifact:bundle:size" in verified["issues"] or "artifact:bundle:sha256" in verified["issues"]


def test_receipt_tamper_is_caught_by_detached_signature(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    _run(root, private)
    receipt = root / "release" / "release_receipt.json"
    receipt.write_text(receipt.read_text(encoding="utf-8").replace("phase27-test-release", "tampered-release"), encoding="utf-8")
    verified = verify_release_receipt(root)
    assert verified["ok"] is False
    assert verified["issues"][0].startswith("receipt_signature:")


def test_key_mismatch_fails_before_published_outputs(tmp_path: Path) -> None:
    root, _private, _ = _fixture(tmp_path)
    wrong_dir = tmp_path / "wrong-keys"
    wrong_dir.mkdir()
    wrong_private = wrong_dir / "private.pem"
    wrong_public = wrong_dir / "public.pem"
    generate_keypair(wrong_private, wrong_public)
    with pytest.raises(ValueError, match="does not match"):
        _run(root, wrong_private)
    assert not (root / "release" / "release_manifest.json").exists()
    assert not (root / "release" / "release_receipt.json").exists()


def test_private_key_inside_project_is_rejected(tmp_path: Path) -> None:
    root, _private, _ = _fixture(tmp_path)
    inside_private = root / "release" / "private.pem"
    outside_public = tmp_path / "inside-pair-public.pem"
    generate_keypair(inside_private, outside_public)
    with pytest.raises(ValueError, match="outside the project root"):
        _run(root, inside_private)


def test_existing_artifacts_require_explicit_overwrite(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    _run(root, private)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _run(root, private)
    rerun = _run(root, private, overwrite=True)
    assert rerun["ok"] is True


def test_expected_release_pin_mismatch_fails_verification(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    _run(root, private)
    verified = verify_release_receipt(root, expected_release_id="different-release")
    assert verified["ok"] is False
    assert "anti_rollback:release_id" in verified["issues"]


def test_stale_required_calendar_fails_before_publish(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    calendar = root / "market_calendar.yaml"
    calendar.write_text(calendar.read_text(encoding="utf-8").replace("2099-12-31", "2020-01-01"), encoding="utf-8")
    with pytest.raises(ValueError, match="freshness preflight failed"):
        _run(root, private)
    assert not (root / "release" / "release_manifest.json").exists()
    assert not (root / "release" / "release_receipt.json").exists()


def test_missing_ci_attestation_fails_before_release_signing(tmp_path: Path) -> None:
    root, private, _ = _fixture(tmp_path)
    (root / DEFAULT_CI_ATTESTATION).unlink()
    with pytest.raises(RuntimeError, match="CI attestation preflight failed"):
        run_release_ceremony(
            root,
            source_commit="a" * 40,
            release_id="missing-ci-attestation",
            private_key_path=private,
            require_calendar=True,
        )
    assert not (root / "release" / "release_manifest.json").exists()
