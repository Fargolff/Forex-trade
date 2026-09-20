from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Phase 29 patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_section(path: str, start: str, end: str, replacement: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    start_at = text.find(start)
    if start_at < 0:
        raise RuntimeError(f"Phase 29 start anchor missing in {path}: {start!r}")
    end_at = text.find(end, start_at)
    if end_at < 0:
        raise RuntimeError(f"Phase 29 end anchor missing in {path}: {end!r}")
    target.write_text(text[:start_at] + replacement + text[end_at:], encoding="utf-8")


KEY_POLICY = r'''from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


TRUST_STORE_FORMAT = "forex-auto-trader-signing-key-trust-store"
TRUST_STORE_VERSION = 1
DEFAULT_TRUST_STORE = "release/signing_key_trust.json"
ALLOWED_ROLES = {"release", "ci_attestation"}
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class KeyPolicyError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: str | datetime, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except Exception as exc:
            raise KeyPolicyError("KEY_TIME_INVALID", f"{field} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise KeyPolicyError("KEY_TIME_NAIVE", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _safe_path(root: Path, value: str | Path) -> Path:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise KeyPolicyError("KEY_PATH_UNSAFE", f"public key path must be project-relative: {value!r}")
    target = (root.resolve() / path.as_posix()).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise KeyPolicyError("KEY_PATH_ESCAPE", f"public key path escapes project root: {value!r}")
    return target


def _public_key(path: Path) -> Ed25519PublicKey:
    if not path.is_file():
        raise KeyPolicyError("KEY_FILE_MISSING", f"public key is missing: {path}")
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise KeyPolicyError("KEY_TYPE_INVALID", f"public key must be Ed25519: {path}")
    return key


def public_key_fingerprint_from_path(path: str | Path) -> str:
    key = _public_key(Path(path))
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _key_id(value: Any) -> str:
    text = str(value).strip()
    if not _KEY_ID.fullmatch(text):
        raise KeyPolicyError("KEY_ID_INVALID", f"invalid key_id: {value!r}")
    return text


def _role(value: Any) -> str:
    text = str(value).strip()
    if text not in ALLOWED_ROLES:
        raise KeyPolicyError("KEY_ROLE_INVALID", f"role must be one of {sorted(ALLOWED_ROLES)}")
    return text


def make_key_record(
    root: str | Path,
    *,
    key_id: str,
    role: str,
    public_key_path: str | Path,
    valid_from: str | datetime,
    valid_until: str | datetime | None = None,
    revoked: bool = False,
    revoked_at: str | datetime | None = None,
    revocation_reason: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    key_path = Path(public_key_path).resolve()
    if key_path != root and root not in key_path.parents:
        raise KeyPolicyError("KEY_PATH_OUTSIDE_ROOT", "trusted public keys must be stored inside the project root")
    relative = key_path.relative_to(root).as_posix()
    start = _time(valid_from, "valid_from")
    end = _time(valid_until, "valid_until") if valid_until is not None else None
    if end is not None and end <= start:
        raise KeyPolicyError("KEY_WINDOW_INVALID", "valid_until must be after valid_from")
    revoked_time = _time(revoked_at, "revoked_at") if revoked_at is not None else None
    record: dict[str, Any] = {
        "key_id": _key_id(key_id),
        "role": _role(role),
        "public_key": relative,
        "fingerprint": public_key_fingerprint_from_path(key_path),
        "valid_from": start.isoformat(),
        "valid_until": end.isoformat() if end is not None else None,
        "revoked": bool(revoked),
        "revoked_at": revoked_time.isoformat() if revoked_time is not None else None,
    }
    if revocation_reason:
        record["revocation_reason"] = str(revocation_reason).strip()
    return record


def _normalize_record(root: Path, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KeyPolicyError("KEY_RECORD_INVALID", "key record must be a JSON object")
    key_id = _key_id(value.get("key_id"))
    role = _role(value.get("role"))
    public_path = _safe_path(root, str(value.get("public_key", "")))
    fingerprint = str(value.get("fingerprint", "")).strip().lower()
    actual = public_key_fingerprint_from_path(public_path)
    if fingerprint != actual:
        raise KeyPolicyError("KEY_FINGERPRINT_MISMATCH", f"{key_id} fingerprint does not match {public_path}")
    start = _time(value.get("valid_from"), "valid_from")
    end_value = value.get("valid_until")
    end = _time(end_value, "valid_until") if end_value not in (None, "") else None
    if end is not None and end <= start:
        raise KeyPolicyError("KEY_WINDOW_INVALID", f"{key_id} valid_until must be after valid_from")
    revoked_at_value = value.get("revoked_at")
    revoked_at = _time(revoked_at_value, "revoked_at") if revoked_at_value not in (None, "") else None
    return {
        "key_id": key_id,
        "role": role,
        "public_key": public_path.relative_to(root).as_posix(),
        "public_key_path": str(public_path),
        "fingerprint": actual,
        "valid_from": start.isoformat(),
        "valid_until": end.isoformat() if end is not None else None,
        "revoked": bool(value.get("revoked", False)),
        "revoked_at": revoked_at.isoformat() if revoked_at is not None else None,
        "revocation_reason": str(value.get("revocation_reason", "")).strip() or None,
    }


def load_trust_store(root: str | Path, path: str | Path = DEFAULT_TRUST_STORE) -> dict[str, Any]:
    root = Path(root).resolve()
    store_path = Path(path)
    store_path = store_path.resolve() if store_path.is_absolute() else (root / store_path).resolve()
    if store_path != root and root not in store_path.parents:
        raise KeyPolicyError("TRUST_STORE_PATH_ESCAPE", f"trust store escapes project root: {path}")
    if not store_path.is_file():
        raise KeyPolicyError("TRUST_STORE_MISSING", f"trust store is missing: {store_path}")
    try:
        document = json.loads(store_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise KeyPolicyError("TRUST_STORE_JSON_INVALID", str(exc)) from exc
    if not isinstance(document, dict) or document.get("version") != TRUST_STORE_VERSION or document.get("format") != TRUST_STORE_FORMAT:
        raise KeyPolicyError("TRUST_STORE_FORMAT_INVALID", "unsupported signing-key trust store format")
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise KeyPolicyError("TRUST_STORE_EMPTY", "trust store must contain at least one key")
    keys = [_normalize_record(root, item) for item in raw_keys]
    seen_ids: set[str] = set()
    seen_role_fp: set[tuple[str, str]] = set()
    for item in keys:
        if item["key_id"] in seen_ids:
            raise KeyPolicyError("KEY_ID_DUPLICATE", item["key_id"])
        seen_ids.add(item["key_id"])
        role_fp = (item["role"], item["fingerprint"])
        if role_fp in seen_role_fp:
            raise KeyPolicyError("KEY_FINGERPRINT_DUPLICATE", f"{item['role']}:{item['fingerprint']}")
        seen_role_fp.add(role_fp)
    return {
        "version": TRUST_STORE_VERSION,
        "format": TRUST_STORE_FORMAT,
        "path": str(store_path),
        "keys": keys,
    }


def write_trust_store(
    root: str | Path,
    keys: Iterable[dict[str, Any]],
    path: str | Path = DEFAULT_TRUST_STORE,
) -> dict[str, Any]:
    root = Path(root).resolve()
    target = Path(path)
    target = target.resolve() if target.is_absolute() else (root / target).resolve()
    if target != root and root not in target.parents:
        raise KeyPolicyError("TRUST_STORE_PATH_ESCAPE", f"trust store escapes project root: {path}")
    payload_keys = []
    for item in keys:
        clean = dict(item)
        clean.pop("public_key_path", None)
        payload_keys.append(clean)
    document = {
        "version": TRUST_STORE_VERSION,
        "format": TRUST_STORE_FORMAT,
        "keys": payload_keys,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(target)
    return load_trust_store(root, target)


def _status(record: dict[str, Any], moment: datetime) -> str:
    if bool(record.get("revoked")) or record.get("revoked_at"):
        return "REVOKED"
    start = _time(record["valid_from"], "valid_from")
    if moment < start:
        return "NOT_YET_VALID"
    end = _time(record["valid_until"], "valid_until") if record.get("valid_until") else None
    if end is not None and moment >= end:
        return "EXPIRED"
    return "ACTIVE"


def authorize_signing_key(
    root: str | Path,
    trust_store_path: str | Path,
    *,
    role: str,
    fingerprint: str,
    at_time: str | datetime | None = None,
) -> dict[str, Any]:
    role = _role(role)
    fingerprint = str(fingerprint).strip().lower()
    store = load_trust_store(root, trust_store_path)
    matches = [item for item in store["keys"] if item["role"] == role and item["fingerprint"] == fingerprint]
    if len(matches) != 1:
        raise KeyPolicyError("SIGNING_KEY_NOT_TRUSTED", f"expected exactly one trusted {role} key for fingerprint {fingerprint}")
    moment = _time(at_time, "at_time") if at_time is not None else _now()
    status = _status(matches[0], moment)
    if status != "ACTIVE":
        raise KeyPolicyError(f"SIGNING_KEY_{status}", f"{matches[0]['key_id']} is {status.lower()} at {moment.isoformat()}")
    return {**matches[0], "status": status, "checked_at": moment.isoformat()}


def resolve_verification_key(
    root: str | Path,
    trust_store_path: str | Path,
    *,
    role: str,
    key_id: str,
    fingerprint: str,
    signed_at: str | datetime,
) -> dict[str, Any]:
    role = _role(role)
    key_id = _key_id(key_id)
    fingerprint = str(fingerprint).strip().lower()
    store = load_trust_store(root, trust_store_path)
    matches = [item for item in store["keys"] if item["role"] == role and item["key_id"] == key_id]
    if len(matches) != 1:
        raise KeyPolicyError("VERIFICATION_KEY_NOT_TRUSTED", f"trusted {role} key_id not found: {key_id}")
    record = matches[0]
    if record["fingerprint"] != fingerprint:
        raise KeyPolicyError("VERIFICATION_KEY_FINGERPRINT_MISMATCH", key_id)
    moment = _time(signed_at, "signed_at")
    status = _status(record, moment)
    if bool(record.get("revoked")) or record.get("revoked_at"):
        raise KeyPolicyError("VERIFICATION_KEY_REVOKED", f"{key_id} is revoked")
    if status == "NOT_YET_VALID":
        raise KeyPolicyError("VERIFICATION_KEY_NOT_YET_VALID", f"{key_id} was not valid at signature time")
    if status == "EXPIRED":
        raise KeyPolicyError("VERIFICATION_KEY_EXPIRED_AT_SIGNATURE", f"{key_id} was expired at signature time")
    return {**record, "status": "VALID_AT_SIGNATURE", "signed_at": moment.isoformat()}


def rotation_report(
    root: str | Path,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    *,
    role: str | None = None,
    at_time: str | datetime | None = None,
) -> dict[str, Any]:
    store = load_trust_store(root, trust_store_path)
    moment = _time(at_time, "at_time") if at_time is not None else _now()
    rows = []
    for item in store["keys"]:
        if role is not None and item["role"] != _role(role):
            continue
        rows.append({**item, "status": _status(item, moment)})
    return {
        "ok": True,
        "checked_at": moment.isoformat(),
        "keys": rows,
        "active_key_ids": [item["key_id"] for item in rows if item["status"] == "ACTIVE"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 29 signing-key rotation, expiry and revocation policy")
    parser.add_argument("--mode", choices=["verify-store", "status"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--trust-store", default=DEFAULT_TRUST_STORE)
    parser.add_argument("--role", choices=sorted(ALLOWED_ROLES))
    args = parser.parse_args()
    if args.mode == "verify-store":
        result = load_trust_store(args.root, args.trust_store)
        print(json.dumps({"ok": True, "path": result["path"], "keys": len(result["keys"])}, indent=2, sort_keys=True))
        return
    print(json.dumps(rotation_report(args.root, args.trust_store, role=args.role), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''

TESTS = r'''from __future__ import annotations

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
    sign_ci_attestation(root, private_key_path=paths["ci-new-private"])
    store_path = root / DEFAULT_TRUST_STORE
    document = json.loads(store_path.read_text(encoding="utf-8"))
    document["keys"][0]["valid_until"] = "2098-01-01T00:00:00+00:00"
    store_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="c" * 40)
    assert report["ok"] is False
    assert "CI_SOURCE_TREE_MISMATCH" in report["issues"]
'''

DOCS = r'''# Phase 29 — Signing-Key Rotation, Expiry & Revocation

Phase 29 turns signing keys into managed identities instead of permanent single-file trust anchors.

## Trust store

The default policy file is `release/signing_key_trust.json`. It is versioned JSON with one record per public key:

- `key_id`: stable operator-visible ID, for example `release-2026-q4`
- `role`: `release` or `ci_attestation`
- `public_key`: project-relative PEM path, normally under `release/keys/`
- `fingerprint`: SHA-256 fingerprint of the Ed25519 SubjectPublicKeyInfo DER
- `valid_from`: timezone-aware ISO-8601 timestamp
- `valid_until`: optional exclusive expiry timestamp
- `revoked`: hard-revocation switch
- `revoked_at` / `revocation_reason`: optional incident metadata

The policy and trusted public PEMs are included in deployment/source-tree provenance. Editing them after CI attestation therefore invalidates the attested source tree.

## Rotation model

Planned rotation uses overlapping validity windows. During the overlap, both old and new keys may be trusted for signing. The signer is selected by the private key fingerprint and the matching `key_id` is embedded into new signature documents.

After the old key's `valid_until`, it cannot create a new signature. Existing signatures made before expiry remain verifiable because verification evaluates the signature's recorded `signed_at` against that key's historical validity window.

## Revocation model

`revoked: true` is deliberately stronger than expiry. A revoked key is rejected for verification even if the signature was created before the revocation. Use hard revocation for suspected compromise. Use `valid_until` for ordinary planned retirement.

## Signature version 2

Phase 29 signatures add `key_id` and use signature document version 2 for:

- release manifest signatures
- deterministic release-bundle signatures
- release-receipt signatures
- CI attestation signatures

Legacy version-1 primitives remain verifiable when used directly with an explicitly supplied public key, preserving Phase 11/12 compatibility. The Phase 29 release ceremony is stricter: it requires the trust store and version-2 key identity.

## Required operator setup

1. Keep all private keys outside the repository.
2. Put only public keys under `release/keys/`.
3. Create `release/signing_key_trust.json` from `signing_key_trust.example.json`.
4. Compute every fingerprint from the actual PEM; never hand-copy a guessed fingerprint.
5. Give CI only the current CI-attestation private key through the protected secret. Never give CI the release private key.
6. For planned rotation, add the new public key and policy record before the new key's `valid_from`.
7. Keep the retiring public key and record so historical releases remain verifiable.
8. For compromise, set `revoked: true`, document the reason, re-run CI, and rotate immediately.

## Fail-closed cases

Release/attestation verification fails when:

- the trust store is missing or malformed
- `key_id` is unknown or duplicated
- a PEM fingerprint differs from the policy
- a signing key is expired, not yet valid, or revoked
- a signature claims a different fingerprint for its `key_id`
- a signature timestamp is outside the key's validity window
- the trust store or trusted public-key set changes after CI attestation

These controls protect release identity and provenance. They do not guarantee trading profitability, order fills, broker availability, or strategy performance.
'''

EXAMPLE = r'''{
  "version": 1,
  "format": "forex-auto-trader-signing-key-trust-store",
  "keys": [
    {
      "key_id": "release-2026-q4",
      "role": "release",
      "public_key": "release/keys/release-2026-q4.pem",
      "fingerprint": "REPLACE_WITH_SHA256_PUBLIC_KEY_FINGERPRINT",
      "valid_from": "2026-10-01T00:00:00+00:00",
      "valid_until": "2027-10-01T00:00:00+00:00",
      "revoked": false,
      "revoked_at": null
    },
    {
      "key_id": "ci-2026-q4",
      "role": "ci_attestation",
      "public_key": "release/keys/ci-2026-q4.pem",
      "fingerprint": "REPLACE_WITH_SHA256_PUBLIC_KEY_FINGERPRINT",
      "valid_from": "2026-10-01T00:00:00+00:00",
      "valid_until": "2027-10-01T00:00:00+00:00",
      "revoked": false,
      "revoked_at": null
    }
  ]
}
'''

write("src/key_policy.py", KEY_POLICY)
write("tests/test_phase29_key_policy.py", TESTS)
write("docs/phase29-key-rotation.md", DOCS)
write("signing_key_trust.example.json", EXAMPLE)

replace_once(
    "src/recovery.py",
    '''DEPLOYMENT_PATTERNS = (\n    "src/**/*.py",\n    "requirements.txt",\n    "config.example.yaml",\n    "production.example.yaml",\n    "watchdog.example.yaml",\n    "deploy/windows/*.ps1",\n)''',
    '''DEPLOYMENT_PATTERNS = (\n    "src/**/*.py",\n    "requirements.txt",\n    "config.example.yaml",\n    "production.example.yaml",\n    "watchdog.example.yaml",\n    "deploy/windows/*.ps1",\n    "release/signing_key_trust.json",\n    "release/keys/*.pem",\n)''',
)

replace_once(
    "src/release.py",
    '''def sign_manifest(\n    manifest_path: str | Path,\n    private_key_path: str | Path,\n    signature_path: str | Path,\n) -> dict[str, Any]:''',
    '''def sign_manifest(\n    manifest_path: str | Path,\n    private_key_path: str | Path,\n    signature_path: str | Path,\n    *,\n    key_id: str | None = None,\n) -> dict[str, Any]:''',
)
replace_once("src/release.py", '''        "version": 1,\n        "algorithm": "Ed25519",\n        "manifest_sha256": _sha256(manifest_bytes),''', '''        "version": 2 if key_id else 1,\n        "algorithm": "Ed25519",\n        "manifest_sha256": _sha256(manifest_bytes),''')
replace_once("src/release.py", '''        "signature_b64": base64.b64encode(signature).decode("ascii"),\n    }\n    target = Path(signature_path)''', '''        "signature_b64": base64.b64encode(signature).decode("ascii"),\n    }\n    if key_id:\n        payload["key_id"] = str(key_id)\n    target = Path(signature_path)''')
replace_once("src/release.py", '''    if signature_doc.get("version") != 1 or signature_doc.get("algorithm") != "Ed25519":\n        raise ValueError("unsupported release signature format")''', '''    version = signature_doc.get("version")\n    if version not in (1, 2) or signature_doc.get("algorithm") != "Ed25519":\n        raise ValueError("unsupported release signature format")\n    if version == 2 and not str(signature_doc.get("key_id", "")).strip():\n        return {"ok": False, "code": "KEY_ID_MISSING"}''')
replace_once("src/release.py", '''        "signed_at": signature_doc.get("signed_at"),\n    }''', '''        "signed_at": signature_doc.get("signed_at"),\n        "key_id": signature_doc.get("key_id"),\n    }''')

replace_once(
    "src/artifact.py",
    '''def sign_release_bundle(\n    archive_path: str | Path,\n    private_key_path: str | Path,\n    signature_path: str | Path,\n) -> dict[str, Any]:''',
    '''def sign_release_bundle(\n    archive_path: str | Path,\n    private_key_path: str | Path,\n    signature_path: str | Path,\n    *,\n    key_id: str | None = None,\n) -> dict[str, Any]:''',
)
replace_once("src/artifact.py", '''        "version": 1,\n        "algorithm": "Ed25519",\n        "bundle_sha256": sha256_bytes(archive_bytes),''', '''        "version": 2 if key_id else 1,\n        "algorithm": "Ed25519",\n        "bundle_sha256": sha256_bytes(archive_bytes),''')
replace_once("src/artifact.py", '''        "signature_b64": base64.b64encode(signature).decode("ascii"),\n    }\n    target = Path(signature_path)''', '''        "signature_b64": base64.b64encode(signature).decode("ascii"),\n    }\n    if key_id:\n        payload["key_id"] = str(key_id)\n    target = Path(signature_path)''')
replace_once("src/artifact.py", '''    if document.get("version") != 1 or document.get("algorithm") != "Ed25519":\n        raise ValueError("unsupported bundle signature format")''', '''    version = document.get("version")\n    if version not in (1, 2) or document.get("algorithm") != "Ed25519":\n        raise ValueError("unsupported bundle signature format")\n    if version == 2 and not str(document.get("key_id", "")).strip():\n        return {"ok": False, "code": "KEY_ID_MISSING"}''')
replace_once("src/artifact.py", '''        "signed_at": document.get("signed_at"),\n    }''', '''        "signed_at": document.get("signed_at"),\n        "key_id": document.get("key_id"),\n    }''')

replace_once("src/ci_attestation.py", '''from .recovery import DEPLOYMENT_PATTERNS, sha256_file\nfrom .release import generate_keypair, load_private_key, load_public_key, public_key_fingerprint''', '''from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key\nfrom .recovery import DEPLOYMENT_PATTERNS, sha256_file\nfrom .release import generate_keypair, load_private_key, load_public_key, public_key_fingerprint''')

replace_section(
    "src/ci_attestation.py",
    "def sign_ci_attestation(",
    "def verify_ci_attestation(",
    r'''def sign_ci_attestation(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    private_key_path: str | Path | None = None,
    signature_path: str | Path = DEFAULT_SIGNATURE,
    *,
    trust_store_path: str | Path | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"CI attestation private key path is required via argument or {PRIVATE_KEY_ENV}")
    private_path = _outside_private_key(root, key_value)
    attestation = _path(root, attestation_path)
    payload = attestation.read_bytes()
    private = load_private_key(private_path)
    fingerprint = public_key_fingerprint(private.public_key())
    policy = None
    if trust_store_path is not None:
        policy = authorize_signing_key(root, trust_store_path, role="ci_attestation", fingerprint=fingerprint)
        if key_id is not None and str(key_id) != policy["key_id"]:
            raise ValueError("explicit CI key_id does not match the trust-store identity")
        key_id = policy["key_id"]
    signature = private.sign(payload)
    document = {
        "version": 2 if key_id else 1,
        "algorithm": "Ed25519",
        "document": ATTESTATION_FORMAT,
        "attestation_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key_fingerprint": fingerprint,
        "signed_at": _now(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    if key_id:
        document["key_id"] = str(key_id)
    _atomic_json(_path(root, signature_path), document)
    return {**document, "key_policy": policy}


def verify_ci_signature(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    signature_path: str | Path = DEFAULT_SIGNATURE,
    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,
    *,
    trust_store_path: str | Path | None = None,
) -> dict[str, Any]:
    try:
        root = Path(root).resolve()
        attestation = _path(root, attestation_path)
        signature_file = _path(root, signature_path)
        payload = attestation.read_bytes()
        document = json.loads(signature_file.read_text(encoding="utf-8"))
        version = document.get("version") if isinstance(document, dict) else None
        if not isinstance(document, dict) or version not in (1, 2) or document.get("algorithm") != "Ed25519" or document.get("document") != ATTESTATION_FORMAT:
            raise ValueError("unsupported CI attestation signature format")
        expected_hash = hashlib.sha256(payload).hexdigest()
        if document.get("attestation_sha256") != expected_hash:
            return {"ok": False, "code": "CI_ATTESTATION_HASH_MISMATCH"}
        claimed_fingerprint = str(document.get("public_key_fingerprint", "")).strip().lower()
        policy = None
        if trust_store_path is not None:
            if version != 2 or not str(document.get("key_id", "")).strip():
                return {"ok": False, "code": "CI_KEY_ID_REQUIRED"}
            try:
                policy = resolve_verification_key(
                    root,
                    trust_store_path,
                    role="ci_attestation",
                    key_id=str(document.get("key_id")),
                    fingerprint=claimed_fingerprint,
                    signed_at=str(document.get("signed_at", "")),
                )
            except Exception as exc:
                return {"ok": False, "code": f"CI_KEY_POLICY_REJECTED:{type(exc).__name__}:{exc}"}
            public_file = Path(policy["public_key_path"])
        else:
            public_file = _path(root, public_key_path)
        public = load_public_key(public_file)
        fingerprint = public_key_fingerprint(public)
        if claimed_fingerprint != fingerprint:
            return {"ok": False, "code": "CI_PUBLIC_KEY_FINGERPRINT_MISMATCH"}
        try:
            signature = base64.b64decode(str(document.get("signature_b64", "")), validate=True)
        except Exception:
            return {"ok": False, "code": "CI_SIGNATURE_ENCODING_INVALID"}
        try:
            public.verify(signature, payload)
        except InvalidSignature:
            return {"ok": False, "code": "CI_SIGNATURE_INVALID"}
        return {
            "ok": True,
            "code": "CI_SIGNATURE_VALID",
            "attestation_sha256": expected_hash,
            "public_key_fingerprint": fingerprint,
            "signed_at": document.get("signed_at"),
            "key_id": document.get("key_id"),
            "key_policy": policy,
        }
    except Exception as exc:
        return {"ok": False, "code": f"CI_SIGNATURE_ERROR:{type(exc).__name__}:{exc}"}


''',
)
replace_once("src/ci_attestation.py", '''    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,\n    *,\n    expected_source_commit: str | None = None,''', '''    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,\n    trust_store_path: str | Path | None = None,\n    *,\n    expected_source_commit: str | None = None,''')
replace_once("src/ci_attestation.py", '''    signature = verify_ci_signature(root, attestation_path, signature_path, public_key_path)''', '''    signature = verify_ci_signature(root, attestation_path, signature_path, public_key_path, trust_store_path=trust_store_path)''')
replace_once("src/ci_attestation.py", '''        "public_key_fingerprint": signature.get("public_key_fingerprint"),\n        "signature": signature,''', '''        "public_key_fingerprint": signature.get("public_key_fingerprint"),\n        "key_id": signature.get("key_id"),\n        "signature": signature,''')
replace_once("src/ci_attestation.py", '''    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)\n    parser.add_argument("--private-key")''', '''    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)\n    parser.add_argument("--trust-store", default=None)\n    parser.add_argument("--private-key")''')
replace_once("src/ci_attestation.py", '''        result = sign_ci_attestation(root, args.attestation, args.private_key, args.signature)''', '''        result = sign_ci_attestation(root, args.attestation, args.private_key, args.signature, trust_store_path=args.trust_store)''')
replace_once("src/ci_attestation.py", '''        args.public_key,\n        expected_source_commit=args.expected_source_commit,''', '''        args.public_key,\n        args.trust_store,\n        expected_source_commit=args.expected_source_commit,''')

replace_once("src/release_ceremony.py", '''from .artifact import DEFAULT_BUNDLE, DEFAULT_BUNDLE_SIGNATURE, build_release_bundle, sign_release_bundle, verify_release_bundle\nfrom .calendar_provenance''', '''from .artifact import DEFAULT_BUNDLE, DEFAULT_BUNDLE_SIGNATURE, build_release_bundle, sign_release_bundle, verify_release_bundle\nfrom .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key\nfrom .calendar_provenance''')

replace_section(
    "src/release_ceremony.py",
    "def sign_receipt(",
    "def release_preflight(",
    r'''def sign_receipt(
    receipt_path: str | Path,
    private_key_path: str | Path,
    signature_path: str | Path,
    *,
    key_id: str | None = None,
) -> dict[str, Any]:
    payload = Path(receipt_path).read_bytes()
    private = load_private_key(private_key_path)
    signature = private.sign(payload)
    document = {
        "version": 2 if key_id else 1,
        "algorithm": "Ed25519",
        "document": RECEIPT_FORMAT,
        "receipt_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key_fingerprint": public_key_fingerprint(private.public_key()),
        "signed_at": _now(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    if key_id:
        document["key_id"] = str(key_id)
    Path(signature_path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def verify_receipt_signature(receipt_path: str | Path, signature_path: str | Path, public_key_path: str | Path) -> dict[str, Any]:
    try:
        payload = Path(receipt_path).read_bytes()
        document = json.loads(Path(signature_path).read_text(encoding="utf-8"))
        version = document.get("version") if isinstance(document, dict) else None
        if not isinstance(document, dict) or version not in (1, 2) or document.get("algorithm") != "Ed25519" or document.get("document") != RECEIPT_FORMAT:
            raise ValueError("unsupported release receipt signature format")
        if version == 2 and not str(document.get("key_id", "")).strip():
            return {"ok": False, "code": "KEY_ID_MISSING"}
        expected = hashlib.sha256(payload).hexdigest()
        if document.get("receipt_sha256") != expected:
            return {"ok": False, "code": "RECEIPT_HASH_MISMATCH"}
        public = load_public_key(public_key_path)
        fingerprint = public_key_fingerprint(public)
        if document.get("public_key_fingerprint") != fingerprint:
            return {"ok": False, "code": "PUBLIC_KEY_FINGERPRINT_MISMATCH"}
        try:
            signature = base64.b64decode(str(document.get("signature_b64", "")), validate=True)
        except Exception:
            return {"ok": False, "code": "SIGNATURE_ENCODING_INVALID"}
        try:
            public.verify(signature, payload)
        except InvalidSignature:
            return {"ok": False, "code": "SIGNATURE_INVALID"}
        return {"ok": True, "code": "SIGNATURE_VALID", "receipt_sha256": expected, "public_key_fingerprint": fingerprint, "signed_at": document.get("signed_at"), "key_id": document.get("key_id")}
    except Exception as exc:
        return {"ok": False, "code": f"RECEIPT_SIGNATURE_INVALID:{type(exc).__name__}:{exc}"}


''',
)

replace_once("src/release_ceremony.py", '''def release_preflight(root: str | Path, *, source_commit: str, release_id: str, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, private_key_path: str | Path | None = None, calendar_path: str | Path = DEFAULT_CALENDAR_PATH,''', '''def release_preflight(root: str | Path, *, source_commit: str, release_id: str, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, private_key_path: str | Path | None = None, trust_store_path: str | Path = DEFAULT_TRUST_STORE, calendar_path: str | Path = DEFAULT_CALENDAR_PATH,''')
replace_once("src/release_ceremony.py", '''    ci_check = verify_ci_attestation(root, DEFAULT_CI_ATTESTATION, DEFAULT_CI_SIGNATURE, DEFAULT_CI_PUBLIC_KEY, expected_source_commit=commit, expected_repository=DEFAULT_CI_REPOSITORY, expected_workflow=DEFAULT_CI_WORKFLOW, require_main_ref=True)\n    if not ci_check["ok"]:\n        raise RuntimeError(f"CI attestation preflight failed: {ci_check['issues']}")\n    _, fingerprint = _fingerprint_pair(root, private_key_path, public_key_path)''', '''    ci_check = verify_ci_attestation(root, DEFAULT_CI_ATTESTATION, DEFAULT_CI_SIGNATURE, DEFAULT_CI_PUBLIC_KEY, trust_store_path, expected_source_commit=commit, expected_repository=DEFAULT_CI_REPOSITORY, expected_workflow=DEFAULT_CI_WORKFLOW, require_main_ref=True)\n    if not ci_check["ok"]:\n        raise RuntimeError(f"CI attestation preflight failed: {ci_check['issues']}")\n    public, fingerprint = _fingerprint_pair(root, private_key_path, public_key_path)\n    release_policy = authorize_signing_key(root, trust_store_path, role="release", fingerprint=fingerprint)\n    if Path(release_policy["public_key_path"]).resolve() != public.resolve():\n        raise ValueError("release public key path does not match the trust-store key identity")''')
replace_once("src/release_ceremony.py", '''        "public_key_fingerprint": fingerprint,\n        "ci_attestation": ci_check,''', '''        "public_key_fingerprint": fingerprint,\n        "release_key_id": release_policy["key_id"],\n        "ci_attestation": ci_check,''')

replace_once("src/release_ceremony.py", '''def run_release_ceremony(root: str | Path, *, source_commit: str, release_id: str, private_key_path: str | Path, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, manifest_path:''', '''def run_release_ceremony(root: str | Path, *, source_commit: str, release_id: str, private_key_path: str | Path, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, trust_store_path: str | Path = DEFAULT_TRUST_STORE, manifest_path:''')
replace_once("src/release_ceremony.py", '''    preflight = release_preflight(root, source_commit=commit, release_id=rid, public_key_path=public_key_path, private_key_path=private, calendar_path=calendar_path,''', '''    preflight = release_preflight(root, source_commit=commit, release_id=rid, public_key_path=public_key_path, private_key_path=private, trust_store_path=trust_store_path, calendar_path=calendar_path,''')
replace_once("src/release_ceremony.py", '''    ci_check = preflight["ci_attestation"]\n    runtime_kwargs''', '''    ci_check = preflight["ci_attestation"]\n    release_key_id = preflight["release_key_id"]\n    runtime_kwargs''')
replace_once("src/release_ceremony.py", '''        release_sig = sign_manifest(staged["manifest"], private, staged["release_signature"])''', '''        release_sig = sign_manifest(staged["manifest"], private, staged["release_signature"], key_id=release_key_id)''')
replace_once("src/release_ceremony.py", '''        bundle_sig = sign_release_bundle(staged["bundle"], private, staged["bundle_signature"])''', '''        bundle_sig = sign_release_bundle(staged["bundle"], private, staged["bundle_signature"], key_id=release_key_id)''')
replace_once("src/release_ceremony.py", '''            "public_key_fingerprint": fingerprint,\n            "deployment_entries": len(manifest["entries"]),''', '''            "public_key_fingerprint": fingerprint,\n            "release_key_id": release_key_id,\n            "deployment_entries": len(manifest["entries"]),''')
replace_once("src/release_ceremony.py", '''                "public_key_fingerprint": ci_check.get("public_key_fingerprint"),\n                "repository": ci_check.get("repository"),''', '''                "public_key_fingerprint": ci_check.get("public_key_fingerprint"),\n                "key_id": ci_check.get("key_id"),\n                "repository": ci_check.get("repository"),''')
replace_once("src/release_ceremony.py", '''        sign_receipt(staged["receipt"], private, staged["receipt_signature"])''', '''        sign_receipt(staged["receipt"], private, staged["receipt_signature"], key_id=release_key_id)''')
replace_once("src/release_ceremony.py", '''    final = verify_release_receipt(root, receipt_path=receipt_path, receipt_signature_path=receipt_signature_path, public_key_path=public_key_path, calendar_path=calendar_path,''', '''    final = verify_release_receipt(root, receipt_path=receipt_path, receipt_signature_path=receipt_signature_path, public_key_path=public_key_path, trust_store_path=trust_store_path, calendar_path=calendar_path,''')

replace_once("src/release_ceremony.py", '''def verify_release_receipt(root: str | Path, *, receipt_path: str | Path = DEFAULT_RECEIPT, receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, calendar_path:''', '''def verify_release_receipt(root: str | Path, *, receipt_path: str | Path = DEFAULT_RECEIPT, receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, trust_store_path: str | Path = DEFAULT_TRUST_STORE, calendar_path:''')
replace_once("src/release_ceremony.py", '''    public = _under(root, public_key_path)\n    sig = verify_receipt_signature(receipt, signature, public)\n    if not sig["ok"]:\n        return {"ok": False, "code": "RELEASE_RECEIPT_INVALID", "issues": [f"receipt_signature:{sig['code']}"], "signature": sig}\n    document = json.loads(receipt.read_text(encoding="utf-8"))''', '''    public = _under(root, public_key_path)\n    try:\n        signature_doc = json.loads(signature.read_text(encoding="utf-8"))\n        release_policy = resolve_verification_key(\n            root,\n            trust_store_path,\n            role="release",\n            key_id=str(signature_doc.get("key_id", "")),\n            fingerprint=str(signature_doc.get("public_key_fingerprint", "")),\n            signed_at=str(signature_doc.get("signed_at", "")),\n        )\n        public = Path(release_policy["public_key_path"])\n    except Exception as exc:\n        return {"ok": False, "code": "RELEASE_RECEIPT_INVALID", "issues": [f"receipt_key_policy:{type(exc).__name__}:{exc}"]}\n    sig = verify_receipt_signature(receipt, signature, public)\n    if not sig["ok"]:\n        return {"ok": False, "code": "RELEASE_RECEIPT_INVALID", "issues": [f"receipt_signature:{sig['code']}"], "signature": sig}\n    document = json.loads(receipt.read_text(encoding="utf-8"))''')
replace_once("src/release_ceremony.py", '''    if document.get("public_key_fingerprint") != fingerprint:\n        issues.append("receipt:public_key_fingerprint")''', '''    if document.get("public_key_fingerprint") != fingerprint:\n        issues.append("receipt:public_key_fingerprint")\n    if document.get("release_key_id") != sig.get("key_id"):\n        issues.append("receipt:release_key_id")''')
replace_once("src/release_ceremony.py", '''        ci_check = verify_ci_attestation(root, ci_paths["attestation"], ci_paths["signature"], DEFAULT_CI_PUBLIC_KEY, expected_source_commit=commit,''', '''        ci_check = verify_ci_attestation(root, ci_paths["attestation"], ci_paths["signature"], DEFAULT_CI_PUBLIC_KEY, trust_store_path, expected_source_commit=commit,''')
replace_once("src/release_ceremony.py", '''        for field in ("public_key_fingerprint", "repository", "workflow", "run_id", "run_attempt", "run_url", "source_tree_sha256"):''', '''        for field in ("public_key_fingerprint", "key_id", "repository", "workflow", "run_id", "run_attempt", "run_url", "source_tree_sha256"):''')
replace_once("src/release_ceremony.py", '''    return {"ok": not issues, "code": "RELEASE_RECEIPT_VALID" if not issues else "RELEASE_RECEIPT_INVALID", "issues": issues, "source_commit": commit, "release_id": rid, "public_key_fingerprint": fingerprint, "signature": sig,''', '''    return {"ok": not issues, "code": "RELEASE_RECEIPT_VALID" if not issues else "RELEASE_RECEIPT_INVALID", "issues": issues, "source_commit": commit, "release_id": rid, "public_key_fingerprint": fingerprint, "release_key_id": sig.get("key_id"), "signature": sig,''')
replace_once("src/release_ceremony.py", '''    parser = argparse.ArgumentParser(description="Phase 28 CI-attested deterministic release ceremony and signed receipt")''', '''    parser = argparse.ArgumentParser(description="Phase 29 key-policy-gated CI-attested release ceremony")''')
replace_once("src/release_ceremony.py", '''    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)\n    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)''', '''    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)\n    parser.add_argument("--trust-store", default=DEFAULT_TRUST_STORE)\n    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)''')
replace_once("src/release_ceremony.py", '''        result = verify_release_receipt(args.root, receipt_path=args.receipt, receipt_signature_path=args.receipt_signature, public_key_path=args.public_key, calendar_path=args.calendar,''', '''        result = verify_release_receipt(args.root, receipt_path=args.receipt, receipt_signature_path=args.receipt_signature, public_key_path=args.public_key, trust_store_path=args.trust_store, calendar_path=args.calendar,''')
replace_once("src/release_ceremony.py", '''        result = release_preflight(args.root, source_commit=args.source_commit, release_id=args.release_id, public_key_path=args.public_key, private_key_path=private, calendar_path=args.calendar,''', '''        result = release_preflight(args.root, source_commit=args.source_commit, release_id=args.release_id, public_key_path=args.public_key, private_key_path=private, trust_store_path=args.trust_store, calendar_path=args.calendar,''')
replace_once("src/release_ceremony.py", '''    result = run_release_ceremony(args.root, source_commit=args.source_commit, release_id=args.release_id, private_key_path=private, public_key_path=args.public_key, manifest_path=args.manifest,''', '''    result = run_release_ceremony(args.root, source_commit=args.source_commit, release_id=args.release_id, private_key_path=private, public_key_path=args.public_key, trust_store_path=args.trust_store, manifest_path=args.manifest,''')

replace_once("tests/test_phase27_release_ceremony.py", '''from src.ci_attestation import DEFAULT_ATTESTATION as DEFAULT_CI_ATTESTATION, DEFAULT_SIGNATURE as DEFAULT_CI_SIGNATURE, create_ci_attestation, sign_ci_attestation\nfrom src.release import generate_keypair''', '''from src.ci_attestation import DEFAULT_ATTESTATION as DEFAULT_CI_ATTESTATION, DEFAULT_SIGNATURE as DEFAULT_CI_SIGNATURE, create_ci_attestation, sign_ci_attestation\nfrom src.key_policy import DEFAULT_TRUST_STORE, make_key_record, write_trust_store\nfrom src.release import generate_keypair''')
replace_once("tests/test_phase27_release_ceremony.py", '''    sign_ci_attestation(root, DEFAULT_CI_ATTESTATION, root.parent / "ci-keys" / "ci-private.pem", DEFAULT_CI_SIGNATURE)''', '''    sign_ci_attestation(root, DEFAULT_CI_ATTESTATION, root.parent / "ci-keys" / "ci-private.pem", DEFAULT_CI_SIGNATURE, trust_store_path=DEFAULT_TRUST_STORE)''')
replace_once("tests/test_phase27_release_ceremony.py", '''    ci_public = root / "release" / "forex-ci-attestation-public.pem"\n    generate_keypair(ci_private, ci_public)\n    _attest(root, "a" * 40)''', '''    ci_public = root / "release" / "forex-ci-attestation-public.pem"\n    generate_keypair(ci_private, ci_public)\n    write_trust_store(\n        root,\n        [\n            make_key_record(root, key_id="release-test-2026", role="release", public_key_path=public, valid_from="2020-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z"),\n            make_key_record(root, key_id="ci-test-2026", role="ci_attestation", public_key_path=ci_public, valid_from="2020-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z"),\n        ],\n    )\n    _attest(root, "a" * 40)''')
replace_once("tests/test_phase27_release_ceremony.py", '''    assert receipt["artifacts"]["bundle"]["path"] == "release/forex-release-bundle.zip"''', '''    assert receipt["artifacts"]["bundle"]["path"] == "release/forex-release-bundle.zip"\n    assert receipt["release_key_id"] == "release-test-2026"\n    assert receipt["ci_attestation"]["key_id"] == "ci-test-2026"\n    assert json.loads((root / "release" / "release_signature.json").read_text(encoding="utf-8"))["version"] == 2''')

README = ROOT / "README.md"
readme_text = README.read_text(encoding="utf-8")
section = r'''

## Phase 29 — Signing-Key Rotation, Expiry & Revocation

Production signing now supports a versioned trust store (`release/signing_key_trust.json`) with stable `key_id` values, validity windows, overlapping rotations, and hard revocation. New release/CI signature documents use version 2 and embed the selected `key_id`; expired keys cannot create new signatures, signatures made before planned expiry remain historically verifiable, while revoked keys fail closed even for historical verification. The trust store and `release/keys/*.pem` are included in deployment/source-tree provenance so post-CI trust changes invalidate the attestation. See `docs/phase29-key-rotation.md` and `signing_key_trust.example.json`.
'''
if "## Phase 29 — Signing-Key Rotation, Expiry & Revocation" not in readme_text:
    README.write_text(readme_text.rstrip() + section + "\n", encoding="utf-8")

print("Phase 29 source migration applied")
