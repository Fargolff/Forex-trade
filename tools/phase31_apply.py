from __future__ import annotations

import json
from pathlib import Path

ROOT = Path('.').resolve()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding='utf-8')
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f'marker not found in {path}: {old[:80]!r}')
    target.write_text(text.replace(old, new, 1), encoding='utf-8')


runtime_boot = r'''from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature

from .deployment_approval import (
    DEFAULT_APPROVAL,
    DEFAULT_APPROVAL_SIGNATURE,
    ENVIRONMENT_ID_ENV,
    verify_deployment_approval,
)
from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key
from .recovery import sha256_file
from .release import load_private_key, load_public_key, public_key_fingerprint
from .release_ceremony import DEFAULT_RECEIPT, DEFAULT_RECEIPT_SIGNATURE

BOOT_FORMAT = "forex-auto-trader-runtime-boot-attestation"
BOOT_VERSION = 1
DEFAULT_BOOT_RECEIPT = "runtime/runtime_boot_receipt.json"
DEFAULT_BOOT_SIGNATURE = "runtime/runtime_boot_receipt.signature.json"
DEFAULT_BOOT_ARCHIVE_DIR = "runtime/boot_attestations"
PRIVATE_KEY_ENV = "FOREX_RUNTIME_BOOT_PRIVATE_KEY"
MACHINE_ID_ENV = "FOREX_RUNTIME_MACHINE_ID"
DEFAULT_MAX_AGE_SECONDS = 300.0
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


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
            raise ValueError(f"{field} must be ISO-8601 with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _identity(value: str, field: str) -> str:
    text = str(value).strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    return text


def _safe(value: str | Path) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"runtime boot path must be project-relative: {value!r}")
    return path.as_posix()


def _under(root: Path, value: str | Path) -> Path:
    relative = _safe(value)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"runtime boot path escapes project root: {value!r}")
    return target


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError("runtime boot private key must remain outside the project root")
    return path


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_bytes(source.read_bytes())
    temp.replace(target)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _descriptor(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _check_descriptor(root: Path, binding: Any, actual: Path, label: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(binding, dict):
        return [f"{label}:binding"]
    try:
        bound = _under(root, str(binding.get("path", "")))
    except Exception as exc:
        return [f"{label}:path:{type(exc).__name__}"]
    if bound != actual.resolve():
        issues.append(f"{label}:effective_path")
        return issues
    if not actual.is_file():
        issues.append(f"{label}:missing")
        return issues
    if actual.stat().st_size != int(binding.get("size", -1)):
        issues.append(f"{label}:size")
    if sha256_file(actual) != str(binding.get("sha256", "")):
        issues.append(f"{label}:sha256")
    return issues


def create_runtime_boot_attestation(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    private_key_path: str | Path | None = None,
    receipt_path: str | Path = DEFAULT_BOOT_RECEIPT,
    signature_path: str | Path = DEFAULT_BOOT_SIGNATURE,
    archive_dir: str | Path = DEFAULT_BOOT_ARCHIVE_DIR,
    approval_path: str | Path = DEFAULT_APPROVAL,
    approval_signature_path: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    release_receipt_path: str | Path = DEFAULT_RECEIPT,
    release_receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    moment = (now or _now()).astimezone(timezone.utc)

    approval_check = verify_deployment_approval(
        root,
        environment_id=env_id,
        approval_path=approval_path,
        signature_path=approval_signature_path,
        receipt_path=release_receipt_path,
        receipt_signature_path=release_receipt_signature_path,
        trust_store_path=trust_store_path,
        now=moment,
    )
    if not approval_check.get("ok") or approval_check.get("stage") != "DEPLOYABLE":
        raise RuntimeError(f"deployment is not deployable: {approval_check.get('issues')}")

    approval = _under(root, approval_path)
    approval_signature = _under(root, approval_signature_path)
    release_receipt = _under(root, release_receipt_path)
    release_receipt_signature = _under(root, release_receipt_signature_path)
    for path in (approval, approval_signature, release_receipt, release_receipt_signature):
        if not path.is_file():
            raise FileNotFoundError(path)

    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"runtime boot private key is required via argument or {PRIVATE_KEY_ENV}")
    private_path = _outside_private_key(root, key_value)
    private = load_private_key(private_path)
    fingerprint = public_key_fingerprint(private.public_key())
    policy = authorize_signing_key(
        root,
        trust_store_path,
        role="runtime_boot",
        fingerprint=fingerprint,
        at_time=moment,
    )

    approval_doc = _load_json(approval, "deployment approval")
    release_doc = _load_json(release_receipt, "release receipt")
    approval_release = approval_doc.get("release") if isinstance(approval_doc.get("release"), dict) else {}
    approval_meta = approval_doc.get("approval") if isinstance(approval_doc.get("approval"), dict) else {}
    runtime = release_doc.get("runtime") if isinstance(release_doc.get("runtime"), dict) else {}

    target = _under(root, receipt_path)
    previous_hash = sha256_file(target) if target.is_file() else None
    boot_id = uuid.uuid4().hex
    document = {
        "version": BOOT_VERSION,
        "format": BOOT_FORMAT,
        "status": "BOOT_ATTESTED",
        "boot_id": boot_id,
        "created_at": _iso(moment),
        "environment": {"id": env_id},
        "machine": {"id": machine},
        "release": {
            "source_commit": str(approval_release.get("source_commit", release_doc.get("source_commit", ""))),
            "release_id": str(approval_release.get("release_id", release_doc.get("release_id", ""))),
            "release_key_id": approval_release.get("release_key_id", release_doc.get("release_key_id")),
            "runtime_design_fingerprint": approval_release.get("runtime_design_fingerprint", runtime.get("design_fingerprint")),
            "receipt": _descriptor(release_receipt, root),
            "receipt_signature": _descriptor(release_receipt_signature, root),
        },
        "deployment_approval": {
            "approval": _descriptor(approval, root),
            "signature": _descriptor(approval_signature, root),
            "approval_key_id": approval_check.get("approval_key_id") or approval_meta.get("key_id"),
            "valid_until": approval_check.get("valid_until") or approval_meta.get("valid_until"),
        },
        "runtime_boot_key": {
            "key_id": policy["key_id"],
            "public_key_fingerprint": fingerprint,
        },
        "chain": {"previous_boot_receipt_sha256": previous_hash},
        "verification": {"deployment_approval": True},
    }
    _atomic_json(target, document)

    payload = target.read_bytes()
    signature = private.sign(payload)
    signature_doc = {
        "version": 2,
        "algorithm": "Ed25519",
        "document": BOOT_FORMAT,
        "boot_receipt_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": policy["key_id"],
        "public_key_fingerprint": fingerprint,
        "signed_at": _iso(moment),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    signature_target = _under(root, signature_path)
    _atomic_json(signature_target, signature_doc)

    archive = _under(root, archive_dir)
    _atomic_copy(target, archive / f"{boot_id}.json")
    _atomic_copy(signature_target, archive / f"{boot_id}.signature.json")

    return {
        "ok": True,
        "code": "RUNTIME_BOOT_ATTESTED",
        "boot_id": boot_id,
        "environment_id": env_id,
        "machine_id": machine,
        "source_commit": document["release"]["source_commit"],
        "release_id": document["release"]["release_id"],
        "key_id": policy["key_id"],
        "receipt_sha256": signature_doc["boot_receipt_sha256"],
        "previous_boot_receipt_sha256": previous_hash,
    }


def verify_runtime_boot_attestation(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    receipt_path: str | Path = DEFAULT_BOOT_RECEIPT,
    signature_path: str | Path = DEFAULT_BOOT_SIGNATURE,
    approval_path: str | Path = DEFAULT_APPROVAL,
    approval_signature_path: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    release_receipt_path: str | Path = DEFAULT_RECEIPT,
    release_receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    max_age_seconds: float | None = DEFAULT_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    moment = (now or _now()).astimezone(timezone.utc)
    receipt = _under(root, receipt_path)
    signature_file = _under(root, signature_path)
    issues: list[str] = []
    if not receipt.is_file():
        return {"ok": False, "code": "RUNTIME_BOOT_ATTESTATION_MISSING", "issues": ["boot_receipt:missing"]}
    if not signature_file.is_file():
        return {"ok": False, "code": "RUNTIME_BOOT_ATTESTATION_MISSING", "issues": ["boot_signature:missing"]}

    try:
        document = _load_json(receipt, "runtime boot receipt")
        signature_doc = _load_json(signature_file, "runtime boot signature")
    except Exception as exc:
        return {"ok": False, "code": "RUNTIME_BOOT_ATTESTATION_INVALID", "issues": [f"json:{type(exc).__name__}:{exc}"]}

    if document.get("version") != BOOT_VERSION or document.get("format") != BOOT_FORMAT or document.get("status") != "BOOT_ATTESTED":
        issues.append("boot_receipt:format")
    environment = document.get("environment") if isinstance(document.get("environment"), dict) else {}
    machine_doc = document.get("machine") if isinstance(document.get("machine"), dict) else {}
    if environment.get("id") != env_id:
        issues.append("environment:id")
    if machine_doc.get("id") != machine:
        issues.append("machine:id")

    if signature_doc.get("version") != 2 or signature_doc.get("algorithm") != "Ed25519" or signature_doc.get("document") != BOOT_FORMAT:
        issues.append("boot_signature:format")
    payload = receipt.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if signature_doc.get("boot_receipt_sha256") != digest:
        issues.append("boot_signature:hash")

    key_policy = None
    if not issues:
        try:
            key_policy = resolve_verification_key(
                root,
                trust_store_path,
                role="runtime_boot",
                key_id=str(signature_doc.get("key_id", "")),
                fingerprint=str(signature_doc.get("public_key_fingerprint", "")),
                signed_at=str(signature_doc.get("signed_at", "")),
            )
            public = load_public_key(key_policy["public_key_path"])
            encoded = base64.b64decode(str(signature_doc.get("signature_b64", "")), validate=True)
            public.verify(encoded, payload)
        except InvalidSignature:
            issues.append("boot_signature:invalid")
        except Exception as exc:
            issues.append(f"boot_signature:policy:{type(exc).__name__}:{exc}")

    created = signed = None
    try:
        created = _time(str(document.get("created_at", "")), "created_at")
        signed = _time(str(signature_doc.get("signed_at", "")), "signed_at")
        if abs((signed - created).total_seconds()) > 60:
            issues.append("boot_signature:time_mismatch")
        age = (moment - created).total_seconds()
        if age < -60:
            issues.append("boot_receipt:future")
        if max_age_seconds is not None and age > float(max_age_seconds):
            issues.append("boot_receipt:stale")
    except Exception as exc:
        issues.append(f"boot_receipt:time:{type(exc).__name__}:{exc}")

    approval = _under(root, approval_path)
    approval_signature = _under(root, approval_signature_path)
    release_receipt = _under(root, release_receipt_path)
    release_receipt_signature = _under(root, release_receipt_signature_path)
    approval_binding = document.get("deployment_approval") if isinstance(document.get("deployment_approval"), dict) else {}
    release_binding = document.get("release") if isinstance(document.get("release"), dict) else {}
    issues.extend(_check_descriptor(root, approval_binding.get("approval"), approval, "deployment_approval:approval"))
    issues.extend(_check_descriptor(root, approval_binding.get("signature"), approval_signature, "deployment_approval:signature"))
    issues.extend(_check_descriptor(root, release_binding.get("receipt"), release_receipt, "release:receipt"))
    issues.extend(_check_descriptor(root, release_binding.get("receipt_signature"), release_receipt_signature, "release:receipt_signature"))

    approval_check: dict[str, Any] | None = None
    if not any(issue.startswith("deployment_approval:") or issue.startswith("release:") for issue in issues):
        try:
            approval_check = verify_deployment_approval(
                root,
                environment_id=env_id,
                approval_path=approval_path,
                signature_path=approval_signature_path,
                receipt_path=release_receipt_path,
                receipt_signature_path=release_receipt_signature_path,
                trust_store_path=trust_store_path,
                expected_source_commit=str(release_binding.get("source_commit", "")),
                expected_release_id=str(release_binding.get("release_id", "")),
                now=moment,
            )
            if not approval_check.get("ok") or approval_check.get("stage") != "DEPLOYABLE":
                issues.extend(f"deployment_approval:{issue}" for issue in approval_check.get("issues", ["not_deployable"]))
            if approval_binding.get("approval_key_id") != approval_check.get("approval_key_id"):
                issues.append("deployment_approval:key_id")
            if approval_binding.get("valid_until") != approval_check.get("valid_until"):
                issues.append("deployment_approval:valid_until")
        except Exception as exc:
            issues.append(f"deployment_approval:verify:{type(exc).__name__}:{exc}")

    if release_receipt.is_file():
        try:
            release_doc = _load_json(release_receipt, "release receipt")
            runtime = release_doc.get("runtime") if isinstance(release_doc.get("runtime"), dict) else {}
            if release_binding.get("source_commit") != release_doc.get("source_commit"):
                issues.append("release:source_commit")
            if release_binding.get("release_id") != release_doc.get("release_id"):
                issues.append("release:release_id")
            if release_binding.get("release_key_id") != release_doc.get("release_key_id"):
                issues.append("release:release_key_id")
            if release_binding.get("runtime_design_fingerprint") != runtime.get("design_fingerprint"):
                issues.append("release:runtime_design_fingerprint")
        except Exception as exc:
            issues.append(f"release:json:{type(exc).__name__}:{exc}")

    runtime_key = document.get("runtime_boot_key") if isinstance(document.get("runtime_boot_key"), dict) else {}
    if key_policy is not None:
        if runtime_key.get("key_id") != key_policy.get("key_id"):
            issues.append("runtime_boot_key:key_id")
        if runtime_key.get("public_key_fingerprint") != key_policy.get("fingerprint"):
            issues.append("runtime_boot_key:fingerprint")

    return {
        "ok": not issues,
        "code": "RUNTIME_BOOT_ATTESTATION_VALID" if not issues else "RUNTIME_BOOT_ATTESTATION_INVALID",
        "issues": issues,
        "boot_id": document.get("boot_id"),
        "environment_id": environment.get("id"),
        "machine_id": machine_doc.get("id"),
        "source_commit": release_binding.get("source_commit"),
        "release_id": release_binding.get("release_id"),
        "runtime_boot_key_id": runtime_key.get("key_id"),
        "created_at": document.get("created_at"),
        "receipt_sha256": digest,
        "previous_boot_receipt_sha256": (document.get("chain") or {}).get("previous_boot_receipt_sha256") if isinstance(document.get("chain"), dict) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 31 runtime boot attestation")
    parser.add_argument("--mode", choices=["create", "verify"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--environment-id", default=os.getenv(ENVIRONMENT_ID_ENV, ""))
    parser.add_argument("--machine-id", default=os.getenv(MACHINE_ID_ENV, ""))
    parser.add_argument("--private-key", default=os.getenv(PRIVATE_KEY_ENV))
    parser.add_argument("--receipt", default=DEFAULT_BOOT_RECEIPT)
    parser.add_argument("--signature", default=DEFAULT_BOOT_SIGNATURE)
    parser.add_argument("--archive-dir", default=DEFAULT_BOOT_ARCHIVE_DIR)
    parser.add_argument("--approval", default=DEFAULT_APPROVAL)
    parser.add_argument("--approval-signature", default=DEFAULT_APPROVAL_SIGNATURE)
    parser.add_argument("--release-receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--release-receipt-signature", default=DEFAULT_RECEIPT_SIGNATURE)
    parser.add_argument("--trust-store", default=DEFAULT_TRUST_STORE)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args()
    if args.mode == "create":
        result = create_runtime_boot_attestation(
            args.root,
            environment_id=args.environment_id,
            machine_id=args.machine_id,
            private_key_path=args.private_key,
            receipt_path=args.receipt,
            signature_path=args.signature,
            archive_dir=args.archive_dir,
            approval_path=args.approval,
            approval_signature_path=args.approval_signature,
            release_receipt_path=args.release_receipt,
            release_receipt_signature_path=args.release_receipt_signature,
            trust_store_path=args.trust_store,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    result = verify_runtime_boot_attestation(
        args.root,
        environment_id=args.environment_id,
        machine_id=args.machine_id,
        receipt_path=args.receipt,
        signature_path=args.signature,
        approval_path=args.approval,
        approval_signature_path=args.approval_signature,
        release_receipt_path=args.release_receipt,
        release_receipt_signature_path=args.release_receipt_signature,
        trust_store_path=args.trust_store,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
'''
write('src/runtime_boot.py', runtime_boot)


tests = r'''from __future__ import annotations

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
'''
write('tests/test_phase31_runtime_boot.py', tests)


docs = r'''# Phase 31 — Deployment Receipt & Runtime Boot Attestation

Phase 31 creates a fresh, signed boot receipt immediately before every supervised-live start or restart. The goal is to prove which approved release a specific Trading PC actually attempted to boot.

## Trust separation

Phase 31 adds a fourth managed signing role: `runtime_boot`.

- `ci_attestation` proves tested source provenance.
- `release` signs release artifacts.
- `deployment_approval` authorizes one release for one environment.
- `runtime_boot` is held by the Trading PC and can sign only runtime boot attestations.

The runtime boot private key must remain outside the repository. Compromise of this key does not grant permission to build a release or approve a deployment.

## Boot receipt bindings

Each receipt binds:

- fresh random `boot_id`
- UTC creation time
- exact `environment_id`
- exact operator-configured `machine_id`
- source commit and release ID
- release signing key ID
- portfolio frozen-design fingerprint
- release receipt and release receipt signature path/size/SHA-256
- deployment approval and approval signature path/size/SHA-256
- deployment approval key ID and validity deadline
- runtime boot key ID and public-key fingerprint
- SHA-256 of the previous current boot receipt

A detached Ed25519 signature protects the full boot receipt.

## Replay protection and history

Supervisor verification requires the current boot receipt to be no older than 300 seconds by default. Every restart creates a new receipt before launching supervised live.

Each successful creation is also archived under:

`runtime/boot_attestations/<boot_id>.json`

and

`runtime/boot_attestations/<boot_id>.signature.json`

The current receipt carries `previous_boot_receipt_sha256`, giving restarts a simple tamper-evident hash chain while archived receipts remain available for audit.

## Required production environment variables

When signed-release/deployment approval gates are enabled, Phase 31 defaults on as well:

- `FOREX_RUNTIME_MACHINE_ID=trader-pc-01`
- `FOREX_RUNTIME_BOOT_PRIVATE_KEY=C:\\secure\\runtime-boot-private.pem`

Existing Phase 30 `FOREX_DEPLOYMENT_ENVIRONMENT_ID` remains required.

Optional migration bypass:

`FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION=0`

This should only be used during a controlled migration.

## Supervisor order

The supervisor now runs:

1. signed bundle verification
2. signed release verification
3. market calendar provenance
4. runtime/portfolio provenance
5. deployment approval verification
6. restart reconciliation
7. create + verify fresh runtime boot attestation
8. supervised live process

The boot receipt therefore represents a point-in-time proof that all preceding gates were accepted immediately before launch.

## Fail-closed cases

Startup is blocked when any of these occur:

- runtime boot private key is missing or inside the project tree
- runtime boot key is not trusted for role `runtime_boot`
- runtime boot key is expired, not-yet-valid, or revoked
- machine/environment ID differs
- receipt is stale or from the future
- approval or release receipt changed after boot receipt creation
- any bound file path/size/SHA-256 differs
- detached boot signature is invalid
- current deployment approval is no longer DEPLOYABLE

Phase 31 does not enable live trading and does not send, retry, repair, resize, or flatten broker orders/positions.
'''
write('docs/phase31-runtime-boot.md', docs)

# Add the runtime_boot signing role.
replace_once(
    'src/key_policy.py',
    'ALLOWED_ROLES = {"release", "ci_attestation", "deployment_approval"}',
    'ALLOWED_ROLES = {"release", "ci_attestation", "deployment_approval", "runtime_boot"}',
)

# Add an example runtime boot public key record.
trust_example = ROOT / 'signing_key_trust.example.json'
example = json.loads(trust_example.read_text(encoding='utf-8'))
if not any(item.get('role') == 'runtime_boot' for item in example.get('keys', [])):
    example['keys'].append({
        'key_id': 'runtime-boot-2026-q4',
        'role': 'runtime_boot',
        'public_key': 'release/keys/runtime-boot-2026-q4.pem',
        'fingerprint': 'REPLACE_WITH_SHA256_PUBLIC_KEY_FINGERPRINT',
        'valid_from': '2026-10-01T00:00:00+00:00',
        'valid_until': '2027-10-01T00:00:00+00:00',
        'revoked': False,
        'revoked_at': None,
    })
    trust_example.write_text(json.dumps(example, indent=2, sort_keys=False) + '\n', encoding='utf-8')

# Phase 31 supervisor configuration.
supervisor = ROOT / 'deploy/windows/run-live-supervisor.ps1'
text = supervisor.read_text(encoding='utf-8')
config_marker = '# Phase 14 broker/local restart reconciliation is fail-closed by default.'
config_block = r'''# Phase 31 creates a fresh signed runtime boot receipt immediately before each
# supervised-live start/restart. It defaults on whenever Phase 30 approval is on.
$RequireRuntimeBootRaw = [string]$env:FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION
if ([string]::IsNullOrWhiteSpace($RequireRuntimeBootRaw)) {
    $RuntimeBootEnabled = $DeploymentApprovalEnabled
} else {
    $RuntimeBootEnabled = @("1", "true", "yes", "on") -contains $RequireRuntimeBootRaw.ToLowerInvariant()
}
if ($RuntimeBootEnabled -and -not $DeploymentApprovalEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION requires the Phase 30 deployment approval gate."
}

$RuntimeMachineId = [string]$env:FOREX_RUNTIME_MACHINE_ID
if ($RuntimeBootEnabled -and [string]::IsNullOrWhiteSpace($RuntimeMachineId)) {
    throw "FOREX_RUNTIME_MACHINE_ID is required when runtime boot attestation is enabled."
}

$RuntimeBootPrivateKey = [string]$env:FOREX_RUNTIME_BOOT_PRIVATE_KEY
if ($RuntimeBootEnabled -and [string]::IsNullOrWhiteSpace($RuntimeBootPrivateKey)) {
    throw "FOREX_RUNTIME_BOOT_PRIVATE_KEY is required when runtime boot attestation is enabled."
}

$RuntimeBootReceipt = [string]$env:FOREX_RUNTIME_BOOT_RECEIPT
if ([string]::IsNullOrWhiteSpace($RuntimeBootReceipt)) {
    $RuntimeBootReceipt = "runtime/runtime_boot_receipt.json"
}

$RuntimeBootSignature = [string]$env:FOREX_RUNTIME_BOOT_SIGNATURE
if ([string]::IsNullOrWhiteSpace($RuntimeBootSignature)) {
    $RuntimeBootSignature = "runtime/runtime_boot_receipt.signature.json"
}

'''
if 'FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION' not in text:
    if config_marker not in text:
        raise RuntimeError('Phase 31 supervisor config marker missing')
    text = text.replace(config_marker, config_block + config_marker, 1)

function_marker = 'function Test-RestartReconciliation {'
function_block = r'''function Write-RuntimeBootAttestation {
    if (-not $RuntimeBootEnabled) {
        Write-Warning "Phase 31 runtime boot attestation is explicitly disabled by FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION."
        return
    }

    & $Python -m src.runtime_boot `
        --mode create `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --private-key $RuntimeBootPrivateKey `
        --receipt $RuntimeBootReceipt `
        --signature $RuntimeBootSignature `
        --approval $DeploymentApprovalPath `
        --approval-signature $DeploymentApprovalSignature `
        --release-receipt $ReleaseReceipt `
        --release-receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore

    if ($LASTEXITCODE -ne 0) {
        throw "Runtime boot attestation creation failed. Supervised live will not start."
    }

    & $Python -m src.runtime_boot `
        --mode verify `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --machine-id $RuntimeMachineId `
        --receipt $RuntimeBootReceipt `
        --signature $RuntimeBootSignature `
        --approval $DeploymentApprovalPath `
        --approval-signature $DeploymentApprovalSignature `
        --release-receipt $ReleaseReceipt `
        --release-receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore `
        --max-age-seconds 300

    if ($LASTEXITCODE -ne 0) {
        throw "Runtime boot attestation verification failed. Supervised live will not start."
    }
}

'''
if 'function Write-RuntimeBootAttestation' not in text:
    if function_marker not in text:
        raise RuntimeError('Phase 31 supervisor function marker missing')
    text = text.replace(function_marker, function_block + function_marker, 1)

loop_old = '    Test-DeploymentApproval\n    Test-RestartReconciliation\n\n    & $Python -m src.production --mode supervised-live --arm-live $ArmPhrase'
loop_new = '    Test-DeploymentApproval\n    Test-RestartReconciliation\n    Write-RuntimeBootAttestation\n\n    & $Python -m src.production --mode supervised-live --arm-live $ArmPhrase'
if '    Write-RuntimeBootAttestation\n\n    & $Python -m src.production' not in text:
    if loop_old not in text:
        raise RuntimeError('Phase 31 supervisor loop marker missing')
    text = text.replace(loop_old, loop_new, 1)
supervisor.write_text(text, encoding='utf-8')

# README summary.
readme = ROOT / 'README.md'
readme_text = readme.read_text(encoding='utf-8')
section = r'''

## Phase 31 — Runtime Boot Attestation

Production supervisor starts/restarts can now create a fresh Ed25519-signed boot receipt after release, provenance, deployment-approval, and restart-reconciliation gates pass. The receipt binds the exact environment, machine ID, approved release, portfolio design fingerprint, approval/release artifact hashes, runtime boot key identity, and previous boot-receipt hash. Receipts older than five minutes are rejected at startup and each boot is archived under `runtime/boot_attestations/` for audit. Runtime boot keys use the dedicated `runtime_boot` trust role and cannot sign releases or deployment approvals. Live trading remains disabled by default and still requires the exact arm phrase.
'''
if '## Phase 31 — Runtime Boot Attestation' not in readme_text:
    readme.write_text(readme_text.rstrip() + section + '\n', encoding='utf-8')

print('Phase 31 source/tests/docs applied')
