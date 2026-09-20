from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ci_attestation import DEFAULT_ATTESTATION, DEFAULT_SIGNATURE, create_ci_attestation, sign_ci_attestation, verify_ci_attestation
from src.key_policy import (
    DEFAULT_TRUST_STORE,
    KeyPolicyError,
    authorize_signing_key,
    make_key_record,
    resolve_verification_key,
    write_trust_store,
)
from src.release import generate_keypair, load_private_key, public_key_fingerprint


def _project(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
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
    (root / "release" / "keys").mkdir(parents=True)

    private_dir = tmp_path / "private"
    private_dir.mkdir()
    paths: dict[str, Path] = {}
    for name in ("release-old", "release-new", "ci-old", "ci-new"):
        private = private_dir / f"{name}.pem"
        public = root / "release" / "keys" / f"{name}.pem"
        generate_keypair(private, public)
        paths[f"{name}-private"] = private
        paths[f"{name}-public"] = public
    return root, paths


def _record(root: Path, paths: dict[str, Path], name: str, role: str, *, valid_from: str, valid_until: str | None = None, revoked: bool = False) -> dict:
    return make_key_record(
        root,
        key_id=name,
        role=role,
        public_key_path=paths[f"{name}-public"],
        valid_from=valid_from,
        valid_until=valid_until,
        revoked=revoked,
    )


def test_rotation_overlap_allows_both_signing_keys(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    write_trust_store(
        root,
        [
            _record(root, paths, "release-old", "release", valid_from="2026-01-01T00:00:00Z", valid_until="2026-10-01T00:00:00Z"),
            _record(root, paths, "release-new", "release", valid_from="2026-09-01T00:00:00Z", valid_until="2027-09-01T00:00:00Z"),
        ],
    )
    old_fp = public_key_fingerprint(load_private_key(paths["release-old-private"]).public_key())
    new_fp = public_key_fingerprint(load_private_key(paths["release-new-private"]).public_key())
    assert authorize_signing_key(root, DEFAULT_TRUST_STORE, role="release", fingerprint=old_fp, at_time="2026-09-20T00:00:00Z")["key_id"] == "release-old"
    assert authorize_signing_key(root, DEFAULT_TRUST_STORE, role="release", fingerprint=new_fp, at_time="2026-09-20T00:00:00Z")["key_id"] == "release-new"


def test_expired_key_cannot_create_new_signature(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    write_trust_store(root, [_record(root, paths, "release-old", "release", valid_from="2025-01-01T00:00:00Z", valid_until="2026-09-01T00:00:00Z")])
    fingerprint = public_key_fingerprint(load_private_key(paths["release-old-private"]).public_key())
    with pytest.raises(KeyPolicyError, match="SIGNING_KEY_EXPIRED"):
        authorize_signing_key(root, DEFAULT_TRUST_STORE, role="release", fingerprint=fingerprint, at_time="2026-09-20T00:00:00Z")


def test_not_yet_valid_key_cannot_create_signature(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    write_trust_store(root, [_record(root, paths, "release-new", "release", valid_from="2026-10-01T00:00:00Z")])
    fingerprint = public_key_fingerprint(load_private_key(paths["release-new-private"]).public_key())
    with pytest.raises(KeyPolicyError, match="SIGNING_KEY_NOT_YET_VALID"):
        authorize_signing_key(root, DEFAULT_TRUST_STORE, role="release", fingerprint=fingerprint, at_time="2026-09-20T00:00:00Z")


def test_revocation_is_fail_closed_even_for_historical_signature(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    record = _record(root, paths, "release-old", "release", valid_from="2025-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z", revoked=True)
    write_trust_store(root, [record])
    with pytest.raises(KeyPolicyError, match="VERIFICATION_KEY_REVOKED"):
        resolve_verification_key(
            root,
            DEFAULT_TRUST_STORE,
            role="release",
            key_id="release-old",
            fingerprint=record["fingerprint"],
            signed_at="2026-01-01T00:00:00Z",
        )


def test_expired_key_still_verifies_signature_made_inside_valid_window(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    record = _record(root, paths, "release-old", "release", valid_from="2025-01-01T00:00:00Z", valid_until="2026-09-01T00:00:00Z")
    write_trust_store(root, [record])
    result = resolve_verification_key(
        root,
        DEFAULT_TRUST_STORE,
        role="release",
        key_id="release-old",
        fingerprint=record["fingerprint"],
        signed_at="2026-08-31T23:59:59Z",
    )
    assert result["status"] == "VALID_AT_SIGNATURE"


def test_ci_signature_v2_resolves_key_id_from_trust_store(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    ci = _record(root, paths, "ci-new", "ci_attestation", valid_from="2025-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z")
    write_trust_store(root, [ci])
    create_ci_attestation(
        root,
        source_commit="a" * 40,
        run_id="29",
        run_url="https://github.com/Fargolff/Forex-trade/actions/runs/29",
        event_name="push",
        ref="refs/heads/main",
        soak_cycles=300,
    )
    signed = sign_ci_attestation(root, private_key_path=paths["ci-new-private"], trust_store_path=DEFAULT_TRUST_STORE)
    assert signed["version"] == 2
    assert signed["key_id"] == "ci-new"
    report = verify_ci_attestation(root, expected_source_commit="a" * 40, trust_store_path=DEFAULT_TRUST_STORE)
    assert report["ok"] is True
    assert report["key_id"] == "ci-new"


def test_wrong_key_id_is_rejected_before_crypto_acceptance(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    records = [
        _record(root, paths, "ci-old", "ci_attestation", valid_from="2025-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z"),
        _record(root, paths, "ci-new", "ci_attestation", valid_from="2025-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z"),
    ]
    write_trust_store(root, records)
    create_ci_attestation(root, source_commit="b" * 40, run_id="29", run_url="https://github.com/Fargolff/Forex-trade/actions/runs/29", ref="refs/heads/main", soak_cycles=300)
    sign_ci_attestation(root, private_key_path=paths["ci-old-private"], trust_store_path=DEFAULT_TRUST_STORE)
    signature_path = root / DEFAULT_SIGNATURE
    document = json.loads(signature_path.read_text(encoding="utf-8"))
    document["key_id"] = "ci-new"
    signature_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="b" * 40, trust_store_path=DEFAULT_TRUST_STORE)
    assert report["ok"] is False
    assert report["issues"][0].startswith("signature:CI_KEY_POLICY_REJECTED")


def test_trust_store_tamper_changes_ci_source_tree(tmp_path: Path) -> None:
    root, paths = _project(tmp_path)
    ci = _record(root, paths, "ci-new", "ci_attestation", valid_from="2025-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z")
    write_trust_store(root, [ci])
    create_ci_attestation(root, source_commit="c" * 40, run_id="29", run_url="https://github.com/Fargolff/Forex-trade/actions/runs/29", ref="refs/heads/main", soak_cycles=300)
    sign_ci_attestation(root, private_key_path=paths["ci-new-private"], trust_store_path=DEFAULT_TRUST_STORE)
    store_path = root / DEFAULT_TRUST_STORE
    document = json.loads(store_path.read_text(encoding="utf-8"))
    document["keys"][0]["valid_until"] = "2098-01-01T00:00:00+00:00"
    store_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="c" * 40, trust_store_path=DEFAULT_TRUST_STORE)
    assert report["ok"] is False
    assert "CI_SOURCE_TREE_MISMATCH" in report["issues"]
