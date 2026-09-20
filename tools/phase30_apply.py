from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEPLOYMENT_APPROVAL = r'''from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from cryptography.exceptions import InvalidSignature

from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key
from .recovery import sha256_file
from .release import load_private_key, load_public_key, public_key_fingerprint
from .release_ceremony import DEFAULT_RECEIPT, DEFAULT_RECEIPT_SIGNATURE, verify_release_receipt

APPROVAL_FORMAT = "forex-auto-trader-deployment-approval"
APPROVAL_VERSION = 1
DEFAULT_APPROVAL = "release/deployment_approval.json"
DEFAULT_APPROVAL_SIGNATURE = "release/deployment_approval.signature.json"
PRIVATE_KEY_ENV = "FOREX_DEPLOYMENT_APPROVAL_PRIVATE_KEY"
ENVIRONMENT_ID_ENV = "FOREX_DEPLOYMENT_ENVIRONMENT_ID"
DEFAULT_VALID_HOURS = 24.0
MAX_VALID_HOURS = 168.0
_ENVIRONMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


def _environment_id(value: str) -> str:
    text = str(value).strip()
    if not _ENVIRONMENT_ID.fullmatch(text):
        raise ValueError("environment_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return text


def _safe(value: str | Path) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"deployment approval path must be project-relative: {value!r}")
    return path.as_posix()


def _under(root: Path, value: str | Path) -> Path:
    relative = _safe(value)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"deployment approval path escapes project root: {value!r}")
    return target


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError("deployment approval private key must remain outside the project root")
    return path


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _descriptor(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _release_check(
    root: Path,
    receipt_path: str | Path,
    receipt_signature_path: str | Path,
    trust_store_path: str | Path,
    expected_source_commit: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    report = verify_release_receipt(
        root,
        receipt_path=receipt_path,
        receipt_signature_path=receipt_signature_path,
        trust_store_path=trust_store_path,
        expected_source_commit=expected_source_commit,
        expected_release_id=expected_release_id,
    )
    if not report.get("ok"):
        raise RuntimeError(f"release receipt verification failed: {report.get('issues')}")
    return report


def prepare_deployment_approval(
    root: str | Path,
    *,
    environment_id: str,
    approval_path: str | Path = DEFAULT_APPROVAL,
    receipt_path: str | Path = DEFAULT_RECEIPT,
    receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    expected_source_commit: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _environment_id(environment_id)
    receipt = _under(root, receipt_path)
    receipt_signature = _under(root, receipt_signature_path)
    if not receipt.is_file() or not receipt_signature.is_file():
        raise FileNotFoundError("signed release receipt and receipt signature are required before promotion")
    verified = _release_check(
        root,
        receipt_path,
        receipt_signature_path,
        trust_store_path,
        expected_source_commit,
        expected_release_id,
    )
    receipt_doc = _load_json(receipt, "release receipt")
    runtime = receipt_doc.get("runtime") if isinstance(receipt_doc.get("runtime"), dict) else {}
    document = {
        "version": APPROVAL_VERSION,
        "format": APPROVAL_FORMAT,
        "status": "VERIFIED",
        "prepared_at": _iso(_now()),
        "environment": {"id": env_id},
        "release": {
            "source_commit": str(verified.get("source_commit", receipt_doc.get("source_commit", ""))),
            "release_id": str(verified.get("release_id", receipt_doc.get("release_id", ""))),
            "release_key_id": receipt_doc.get("release_key_id"),
            "runtime_design_fingerprint": runtime.get("design_fingerprint"),
            "receipt": _descriptor(receipt, root),
            "receipt_signature": _descriptor(receipt_signature, root),
        },
        "verification": {"release_receipt": True},
    }
    target = _under(root, approval_path)
    _atomic_json(target, document)
    return document


def _verify_bound_release(
    root: Path,
    document: dict[str, Any],
    *,
    receipt_path: str | Path,
    receipt_signature_path: str | Path,
    trust_store_path: str | Path,
) -> tuple[list[str], dict[str, Any] | None]:
    issues: list[str] = []
    release = document.get("release") if isinstance(document.get("release"), dict) else {}
    actual_paths = {
        "receipt": _under(root, receipt_path),
        "receipt_signature": _under(root, receipt_signature_path),
    }
    for role, actual in actual_paths.items():
        binding = release.get(role)
        if not isinstance(binding, dict):
            issues.append(f"release:{role}:binding")
            continue
        try:
            expected_path = _under(root, str(binding.get("path", "")))
        except Exception as exc:
            issues.append(f"release:{role}:path:{type(exc).__name__}")
            continue
        if expected_path != actual:
            issues.append(f"release:{role}:effective_path")
            continue
        if not actual.is_file():
            issues.append(f"release:{role}:missing")
            continue
        if actual.stat().st_size != int(binding.get("size", -1)):
            issues.append(f"release:{role}:size")
        if sha256_file(actual) != str(binding.get("sha256", "")):
            issues.append(f"release:{role}:sha256")
    verified: dict[str, Any] | None = None
    if not issues:
        try:
            verified = _release_check(
                root,
                receipt_path,
                receipt_signature_path,
                trust_store_path,
                str(release.get("source_commit", "")),
                str(release.get("release_id", "")),
            )
        except Exception as exc:
            issues.append(f"release:verification:{type(exc).__name__}:{exc}")
    if verified is not None:
        receipt_doc = _load_json(actual_paths["receipt"], "release receipt")
        runtime = receipt_doc.get("runtime") if isinstance(receipt_doc.get("runtime"), dict) else {}
        if release.get("release_key_id") != receipt_doc.get("release_key_id"):
            issues.append("release:release_key_id")
        if release.get("runtime_design_fingerprint") != runtime.get("design_fingerprint"):
            issues.append("release:runtime_design_fingerprint")
    return issues, verified


def approve_deployment(
    root: str | Path,
    *,
    private_key_path: str | Path | None = None,
    approval_path: str | Path = DEFAULT_APPROVAL,
    signature_path: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    receipt_path: str | Path = DEFAULT_RECEIPT,
    receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    valid_for_hours: float = DEFAULT_VALID_HOURS,
    approved_by: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    approval = _under(root, approval_path)
    document = _load_json(approval, "deployment approval")
    if document.get("version") != APPROVAL_VERSION or document.get("format") != APPROVAL_FORMAT:
        raise ValueError("unsupported deployment approval format")
    if document.get("status") != "VERIFIED":
        raise ValueError("only a VERIFIED promotion candidate can be approved")
    hours = float(valid_for_hours)
    if hours <= 0 or hours > MAX_VALID_HOURS:
        raise ValueError(f"valid_for_hours must be >0 and <= {MAX_VALID_HOURS:g}")
    issues, _ = _verify_bound_release(
        root,
        document,
        receipt_path=receipt_path,
        receipt_signature_path=receipt_signature_path,
        trust_store_path=trust_store_path,
    )
    if issues:
        raise RuntimeError(f"release changed after verification: {issues}")
    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"approval private key is required via argument or {PRIVATE_KEY_ENV}")
    private_path = _outside_private_key(root, key_value)
    private = load_private_key(private_path)
    fingerprint = public_key_fingerprint(private.public_key())
    moment = (now or _now()).astimezone(timezone.utc)
    policy = authorize_signing_key(
        root,
        trust_store_path,
        role="deployment_approval",
        fingerprint=fingerprint,
        at_time=moment,
    )
    expires = moment + timedelta(hours=hours)
    document["status"] = "APPROVED"
    document["approval"] = {
        "key_id": policy["key_id"],
        "public_key_fingerprint": fingerprint,
        "approved_at": _iso(moment),
        "valid_until": _iso(expires),
        "approved_by": str(approved_by).strip() if approved_by else None,
    }
    _atomic_json(approval, document)
    payload = approval.read_bytes()
    signature = private.sign(payload)
    signature_doc = {
        "version": 2,
        "algorithm": "Ed25519",
        "document": APPROVAL_FORMAT,
        "approval_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": policy["key_id"],
        "public_key_fingerprint": fingerprint,
        "signed_at": _iso(moment),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    _atomic_json(_under(root, signature_path), signature_doc)
    return {
        "ok": True,
        "code": "DEPLOYMENT_APPROVED",
        "environment_id": document["environment"]["id"],
        "source_commit": document["release"]["source_commit"],
        "release_id": document["release"]["release_id"],
        "key_id": policy["key_id"],
        "valid_until": _iso(expires),
    }


def _verify_signature(
    root: Path,
    approval: Path,
    signature_path: Path,
    trust_store_path: str | Path,
) -> dict[str, Any]:
    try:
        payload = approval.read_bytes()
        signature_doc = _load_json(signature_path, "deployment approval signature")
        if signature_doc.get("version") != 2 or signature_doc.get("algorithm") != "Ed25519" or signature_doc.get("document") != APPROVAL_FORMAT:
            return {"ok": False, "code": "APPROVAL_SIGNATURE_FORMAT_INVALID"}
        expected_hash = hashlib.sha256(payload).hexdigest()
        if signature_doc.get("approval_sha256") != expected_hash:
            return {"ok": False, "code": "APPROVAL_HASH_MISMATCH"}
        policy = resolve_verification_key(
            root,
            trust_store_path,
            role="deployment_approval",
            key_id=str(signature_doc.get("key_id", "")),
            fingerprint=str(signature_doc.get("public_key_fingerprint", "")),
            signed_at=str(signature_doc.get("signed_at", "")),
        )
        public = load_public_key(policy["public_key_path"])
        try:
            signature = base64.b64decode(str(signature_doc.get("signature_b64", "")), validate=True)
        except Exception:
            return {"ok": False, "code": "APPROVAL_SIGNATURE_ENCODING_INVALID"}
        try:
            public.verify(signature, payload)
        except InvalidSignature:
            return {"ok": False, "code": "APPROVAL_SIGNATURE_INVALID"}
        return {
            "ok": True,
            "code": "APPROVAL_SIGNATURE_VALID",
            "key_id": policy["key_id"],
            "public_key_fingerprint": policy["fingerprint"],
            "signed_at": signature_doc.get("signed_at"),
        }
    except Exception as exc:
        return {"ok": False, "code": f"APPROVAL_SIGNATURE_ERROR:{type(exc).__name__}:{exc}"}


def verify_deployment_approval(
    root: str | Path,
    *,
    environment_id: str,
    approval_path: str | Path = DEFAULT_APPROVAL,
    signature_path: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    receipt_path: str | Path = DEFAULT_RECEIPT,
    receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    expected_source_commit: str | None = None,
    expected_release_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    expected_environment = _environment_id(environment_id)
    approval = _under(root, approval_path)
    signature_file = _under(root, signature_path)
    issues: list[str] = []
    if not approval.is_file():
        return {"ok": False, "code": "DEPLOYMENT_NOT_APPROVED", "stage": "VERIFIED", "issues": ["approval:missing"]}
    if not signature_file.is_file():
        return {"ok": False, "code": "DEPLOYMENT_NOT_APPROVED", "stage": "VERIFIED", "issues": ["approval_signature:missing"]}
    try:
        document = _load_json(approval, "deployment approval")
    except Exception as exc:
        return {"ok": False, "code": "DEPLOYMENT_APPROVAL_INVALID", "stage": "APPROVED", "issues": [f"approval:{type(exc).__name__}:{exc}"]}
    if document.get("version") != APPROVAL_VERSION or document.get("format") != APPROVAL_FORMAT:
        issues.append("approval:format")
    if document.get("status") != "APPROVED":
        issues.append("approval:status")
    environment = document.get("environment") if isinstance(document.get("environment"), dict) else {}
    if str(environment.get("id", "")) != expected_environment:
        issues.append("environment:id")
    signature = _verify_signature(root, approval, signature_file, trust_store_path)
    if not signature.get("ok"):
        issues.append(f"approval_signature:{signature.get('code')}")
    approval_info = document.get("approval") if isinstance(document.get("approval"), dict) else {}
    moment = (now or _now()).astimezone(timezone.utc)
    try:
        approved_at = _time(str(approval_info.get("approved_at", "")), "approved_at")
        valid_until = _time(str(approval_info.get("valid_until", "")), "valid_until")
        if valid_until <= approved_at:
            issues.append("approval:validity_window")
        if approved_at > moment + timedelta(minutes=5):
            issues.append("approval:from_future")
        if moment >= valid_until:
            issues.append("approval:expired")
        if (valid_until - approved_at).total_seconds() > MAX_VALID_HOURS * 3600 + 1:
            issues.append("approval:validity_too_long")
    except Exception as exc:
        approved_at = valid_until = None
        issues.append(f"approval:time:{type(exc).__name__}")
    if signature.get("ok"):
        if approval_info.get("key_id") != signature.get("key_id"):
            issues.append("approval:key_id")
        if approval_info.get("public_key_fingerprint") != signature.get("public_key_fingerprint"):
            issues.append("approval:public_key_fingerprint")
        if approved_at is not None and _time(str(signature.get("signed_at", "")), "signed_at") != approved_at:
            issues.append("approval:signed_at")
    release_issues, release_check = _verify_bound_release(
        root,
        document,
        receipt_path=receipt_path,
        receipt_signature_path=receipt_signature_path,
        trust_store_path=trust_store_path,
    )
    issues.extend(release_issues)
    release = document.get("release") if isinstance(document.get("release"), dict) else {}
    if expected_source_commit is not None and str(release.get("source_commit", "")) != str(expected_source_commit).strip().lower():
        issues.append("anti_rollback:source_commit")
    if expected_release_id is not None and str(release.get("release_id", "")) != str(expected_release_id).strip():
        issues.append("anti_rollback:release_id")
    return {
        "ok": not issues,
        "code": "DEPLOYMENT_APPROVAL_VALID" if not issues else "DEPLOYMENT_APPROVAL_INVALID",
        "stage": "DEPLOYABLE" if not issues else "APPROVED",
        "issues": issues,
        "environment_id": environment.get("id"),
        "source_commit": release.get("source_commit"),
        "release_id": release.get("release_id"),
        "approval_key_id": signature.get("key_id"),
        "valid_until": approval_info.get("valid_until"),
        "release": release_check,
        "signature": signature,
    }


def promotion_status(
    root: str | Path,
    *,
    environment_id: str | None = None,
    approval_path: str | Path = DEFAULT_APPROVAL,
    signature_path: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    receipt_path: str | Path = DEFAULT_RECEIPT,
    receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
) -> dict[str, Any]:
    root = Path(root).resolve()
    receipt = _under(root, receipt_path)
    receipt_signature = _under(root, receipt_signature_path)
    if not receipt.is_file() or not receipt_signature.is_file():
        return {"ok": False, "stage": "NOT_BUILT", "issues": ["release_receipt:missing"]}
    try:
        release = _release_check(root, receipt_path, receipt_signature_path, trust_store_path)
    except Exception as exc:
        return {"ok": False, "stage": "BUILT", "issues": [f"release_receipt:{type(exc).__name__}:{exc}"]}
    approval = _under(root, approval_path)
    signature = _under(root, signature_path)
    if not approval.is_file() or not signature.is_file():
        return {"ok": True, "stage": "VERIFIED", "issues": [], "release": release}
    try:
        document = _load_json(approval, "deployment approval")
        env = environment_id or str((document.get("environment") or {}).get("id", ""))
        report = verify_deployment_approval(
            root,
            environment_id=env,
            approval_path=approval_path,
            signature_path=signature_path,
            receipt_path=receipt_path,
            receipt_signature_path=receipt_signature_path,
            trust_store_path=trust_store_path,
        )
        return report
    except Exception as exc:
        return {"ok": False, "stage": "APPROVED", "issues": [f"approval:{type(exc).__name__}:{exc}"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 30 release promotion and deployment approval gate")
    parser.add_argument("--mode", choices=["prepare", "approve", "verify", "status"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--environment-id", default=os.getenv(ENVIRONMENT_ID_ENV, ""))
    parser.add_argument("--approval", default=DEFAULT_APPROVAL)
    parser.add_argument("--signature", default=DEFAULT_APPROVAL_SIGNATURE)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--receipt-signature", default=DEFAULT_RECEIPT_SIGNATURE)
    parser.add_argument("--trust-store", default=DEFAULT_TRUST_STORE)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--valid-hours", type=float, default=DEFAULT_VALID_HOURS)
    parser.add_argument("--approved-by", default=None)
    parser.add_argument("--expected-source-commit", default=None)
    parser.add_argument("--expected-release-id", default=None)
    args = parser.parse_args()
    if args.mode != "status" and not str(args.environment_id).strip():
        raise ValueError(f"--environment-id or {ENVIRONMENT_ID_ENV} is required")
    if args.mode == "prepare":
        result = prepare_deployment_approval(
            args.root,
            environment_id=args.environment_id,
            approval_path=args.approval,
            receipt_path=args.receipt,
            receipt_signature_path=args.receipt_signature,
            trust_store_path=args.trust_store,
            expected_source_commit=args.expected_source_commit,
            expected_release_id=args.expected_release_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "approve":
        result = approve_deployment(
            args.root,
            private_key_path=args.private_key,
            approval_path=args.approval,
            signature_path=args.signature,
            receipt_path=args.receipt,
            receipt_signature_path=args.receipt_signature,
            trust_store_path=args.trust_store,
            valid_for_hours=args.valid_hours,
            approved_by=args.approved_by,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "status":
        result = promotion_status(
            args.root,
            environment_id=args.environment_id or None,
            approval_path=args.approval,
            signature_path=args.signature,
            receipt_path=args.receipt,
            receipt_signature_path=args.receipt_signature,
            trust_store_path=args.trust_store,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        raise SystemExit(0 if result.get("ok") else 3)
    result = verify_deployment_approval(
        args.root,
        environment_id=args.environment_id,
        approval_path=args.approval,
        signature_path=args.signature,
        receipt_path=args.receipt,
        receipt_signature_path=args.receipt_signature,
        trust_store_path=args.trust_store,
        expected_source_commit=args.expected_source_commit,
        expected_release_id=args.expected_release_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
'''

TESTS = r'''from __future__ import annotations

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
'''

DOC = r'''# Phase 30 — Release Promotion & Deployment Approval Gate

Phase 30 separates **building a cryptographically valid release** from **authorizing that release for a concrete deployment environment**.

## Promotion states

1. **BUILT** — Phase 27/29 release artifacts and signed release receipt exist.
2. **VERIFIED** — the receipt, CI provenance, runtime provenance, calendar provenance and signed bundle verify successfully; `prepare` writes a promotion candidate.
3. **APPROVED** — a distinct Ed25519 key with trust-store role `deployment_approval` signs the exact VERIFIED candidate.
4. **DEPLOYABLE** — the signed approval is still valid, not revoked/expired, its receipt hashes still match, and its environment ID exactly matches the local Trading PC environment ID.

An APPROVED document by itself is not sufficient. The Trading PC re-verifies the signature, key policy, expiry and all bound release receipt data on every supervisor start/restart.

## Separation of duties

Use three independent trust roles:

- `ci_attestation`: proves tested source/CI provenance.
- `release`: builds and signs release artifacts.
- `deployment_approval`: authorizes a particular built release for a particular environment.

The deployment-approval private key must remain outside the repository and must **not** be copied to the Trading PC. The Trading PC needs only the trust store and the corresponding public key under `release/keys/`.

## Approval lifetime

Approvals default to 24 hours and may not exceed 168 hours (7 days). Short-lived approval reduces the risk that an old approval is silently reused after operational context changes.

Expiry and revocation remain different:

- expiry prevents deployment after `valid_until`;
- hard key revocation invalidates even a previously signed approval.

## Commands

Prepare a VERIFIED candidate after the release ceremony:

```powershell
python -m src.deployment_approval --mode prepare `
  --environment-id prod-bkk-01 `
  --expected-source-commit <FULL_COMMIT> `
  --expected-release-id <RELEASE_ID>
```

Approve it from an approval workstation with the approval private key kept outside the project:

```powershell
python -m src.deployment_approval --mode approve `
  --environment-id prod-bkk-01 `
  --private-key C:\secure\deployment-approval-private.pem `
  --valid-hours 24 `
  --approved-by change-ticket-1234
```

Verify on the Trading PC:

```powershell
python -m src.deployment_approval --mode verify `
  --environment-id prod-bkk-01
```

Check the promotion stage:

```powershell
python -m src.deployment_approval --mode status `
  --environment-id prod-bkk-01
```

## Supervisor gate

`deploy/windows/run-live-supervisor.ps1` enables the Phase 30 approval gate by default whenever signed-release verification is enabled. Required production setting:

```text
FOREX_DEPLOYMENT_ENVIRONMENT_ID=prod-bkk-01
```

Optional paths:

```text
FOREX_DEPLOYMENT_APPROVAL_PATH=release/deployment_approval.json
FOREX_DEPLOYMENT_APPROVAL_SIGNATURE=release/deployment_approval.signature.json
FOREX_SIGNING_KEY_TRUST_STORE=release/signing_key_trust.json
```

A deliberate migration bypass can set `FOREX_REQUIRE_DEPLOYMENT_APPROVAL=0`; this weakens the deployment trust chain and should not be used as the steady-state production configuration.

## Fail-closed conditions

Deployment is blocked if any of the following occurs:

- approval or signature missing;
- approval key is not trusted for `deployment_approval`;
- approval key is revoked;
- signature/document is modified;
- approval is expired or has an excessive validity window;
- environment ID differs;
- release receipt or receipt signature differs from the approved hashes;
- release receipt no longer verifies;
- source commit/release ID anti-rollback values differ.

Phase 30 never enables live trading, sends broker orders, retries orders, or repairs positions. The existing exact live arming phrase remains required independently.
'''

README_SECTION = r'''

## Phase 30 — Release Promotion & Deployment Approval Gate

Production deployment now has a separate authorization layer after the signed release ceremony. `src.deployment_approval` promotes a release through **BUILT → VERIFIED → APPROVED → DEPLOYABLE** and binds the approval to an exact environment ID, release receipt hashes, source commit, release ID, release signing key and portfolio design fingerprint. A distinct `deployment_approval` Ed25519 trust-store role signs short-lived approvals (24h default, 7-day maximum). The Trading PC re-verifies approval signature, revocation/expiry, exact environment binding and the complete signed release receipt before supervised live starts. The approval private key must remain off the Trading PC. See `docs/phase30-deployment-approval.md`.
'''


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


write("src/deployment_approval.py", DEPLOYMENT_APPROVAL)
write("tests/test_phase30_deployment_approval.py", TESTS)
write("docs/phase30-deployment-approval.md", DOC)

key_policy = ROOT / "src/key_policy.py"
text = key_policy.read_text(encoding="utf-8")
old = 'ALLOWED_ROLES = {"release", "ci_attestation"}'
new = 'ALLOWED_ROLES = {"release", "ci_attestation", "deployment_approval"}'
if old not in text:
    raise RuntimeError("Phase 30 key-policy anchor not found")
key_policy.write_text(text.replace(old, new, 1), encoding="utf-8")

trust_example = ROOT / "signing_key_trust.example.json"
import json
store = json.loads(trust_example.read_text(encoding="utf-8"))
if not any(item.get("role") == "deployment_approval" for item in store.get("keys", [])):
    store["keys"].append({
        "key_id": "approval-2026-q4",
        "role": "deployment_approval",
        "public_key": "release/keys/approval-2026-q4.pem",
        "fingerprint": "REPLACE_WITH_SHA256_PUBLIC_KEY_FINGERPRINT",
        "valid_from": "2026-10-01T00:00:00+00:00",
        "valid_until": "2027-10-01T00:00:00+00:00",
        "revoked": False,
        "revoked_at": None,
    })
trust_example.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")

gitignore = ROOT / ".gitignore"
git_text = gitignore.read_text(encoding="utf-8")
for item in ("release/deployment_approval.json", "release/deployment_approval.signature.json"):
    if item not in git_text:
        git_text += item + "\n"
gitignore.write_text(git_text, encoding="utf-8")

supervisor = ROOT / "deploy/windows/run-live-supervisor.ps1"
ps = supervisor.read_text(encoding="utf-8")
config_anchor = '''if ($RuntimeProvenanceEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_PROVENANCE requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

# Phase 14 broker/local restart reconciliation is fail-closed by default.'''
config_insert = '''if ($RuntimeProvenanceEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_PROVENANCE requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

# Phase 30 requires an explicit, environment-bound deployment approval after the
# release ceremony. It defaults on whenever signed-release verification is on.
$RequireDeploymentApprovalRaw = [string]$env:FOREX_REQUIRE_DEPLOYMENT_APPROVAL
if ([string]::IsNullOrWhiteSpace($RequireDeploymentApprovalRaw)) {
    $DeploymentApprovalEnabled = $SignedReleaseEnabled
} else {
    $DeploymentApprovalEnabled = @("1", "true", "yes", "on") -contains $RequireDeploymentApprovalRaw.ToLowerInvariant()
}
if ($DeploymentApprovalEnabled -and -not $SignedReleaseEnabled) {
    throw "FOREX_REQUIRE_DEPLOYMENT_APPROVAL requires FOREX_REQUIRE_SIGNED_RELEASE=1."
}

$DeploymentEnvironmentId = [string]$env:FOREX_DEPLOYMENT_ENVIRONMENT_ID
if ($DeploymentApprovalEnabled -and [string]::IsNullOrWhiteSpace($DeploymentEnvironmentId)) {
    throw "FOREX_DEPLOYMENT_ENVIRONMENT_ID is required when the deployment approval gate is enabled."
}

$DeploymentApprovalPath = [string]$env:FOREX_DEPLOYMENT_APPROVAL_PATH
if ([string]::IsNullOrWhiteSpace($DeploymentApprovalPath)) {
    $DeploymentApprovalPath = "release/deployment_approval.json"
}

$DeploymentApprovalSignature = [string]$env:FOREX_DEPLOYMENT_APPROVAL_SIGNATURE
if ([string]::IsNullOrWhiteSpace($DeploymentApprovalSignature)) {
    $DeploymentApprovalSignature = "release/deployment_approval.signature.json"
}

$SigningKeyTrustStore = [string]$env:FOREX_SIGNING_KEY_TRUST_STORE
if ([string]::IsNullOrWhiteSpace($SigningKeyTrustStore)) {
    $SigningKeyTrustStore = "release/signing_key_trust.json"
}

$ReleaseReceipt = [string]$env:FOREX_RELEASE_RECEIPT
if ([string]::IsNullOrWhiteSpace($ReleaseReceipt)) {
    $ReleaseReceipt = "release/release_receipt.json"
}

$ReleaseReceiptSignature = [string]$env:FOREX_RELEASE_RECEIPT_SIGNATURE
if ([string]::IsNullOrWhiteSpace($ReleaseReceiptSignature)) {
    $ReleaseReceiptSignature = "release/release_receipt.signature.json"
}

# Phase 14 broker/local restart reconciliation is fail-closed by default.'''
if config_anchor not in ps:
    raise RuntimeError("Phase 30 supervisor config anchor not found")
ps = ps.replace(config_anchor, config_insert, 1)

function_anchor = '''function Test-RestartReconciliation {
'''
function_insert = '''function Test-DeploymentApproval {
    if (-not $DeploymentApprovalEnabled) {
        Write-Warning "Phase 30 deployment approval gate is explicitly disabled by FOREX_REQUIRE_DEPLOYMENT_APPROVAL."
        return
    }

    & $Python -m src.deployment_approval `
        --mode verify `
        --root $ProjectRoot `
        --environment-id $DeploymentEnvironmentId `
        --approval $DeploymentApprovalPath `
        --signature $DeploymentApprovalSignature `
        --receipt $ReleaseReceipt `
        --receipt-signature $ReleaseReceiptSignature `
        --trust-store $SigningKeyTrustStore

    if ($LASTEXITCODE -ne 0) {
        throw "Deployment approval verification failed. Supervised live will not start."
    }
}

function Test-RestartReconciliation {
'''
if function_anchor not in ps:
    raise RuntimeError("Phase 30 supervisor function anchor not found")
ps = ps.replace(function_anchor, function_insert, 1)

call_anchor = '''    Test-MarketCalendarProvenance
    Test-RuntimeProvenance
    Test-RestartReconciliation
'''
call_insert = '''    Test-MarketCalendarProvenance
    Test-RuntimeProvenance
    Test-DeploymentApproval
    Test-RestartReconciliation
'''
if call_anchor not in ps:
    raise RuntimeError("Phase 30 supervisor call anchor not found")
ps = ps.replace(call_anchor, call_insert, 1)
supervisor.write_text(ps, encoding="utf-8")

readme = ROOT / "README.md"
readme_text = readme.read_text(encoding="utf-8")
if "## Phase 30 — Release Promotion & Deployment Approval Gate" not in readme_text:
    readme_text += README_SECTION
readme.write_text(readme_text, encoding="utf-8")

print("Phase 30 source migration applied")
