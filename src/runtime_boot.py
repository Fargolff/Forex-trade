from __future__ import annotations

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
