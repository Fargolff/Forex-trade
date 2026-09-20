from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import string
from typing import Any

from cryptography.exceptions import InvalidSignature

from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key
from .recovery import DEPLOYMENT_PATTERNS, sha256_file
from .release import generate_keypair, load_private_key, load_public_key, public_key_fingerprint

ATTESTATION_FORMAT = "forex-auto-trader-ci-attestation"
ATTESTATION_VERSION = 1
DEFAULT_ATTESTATION = "release/ci_attestation.json"
DEFAULT_SIGNATURE = "release/ci_attestation.signature.json"
DEFAULT_PUBLIC_KEY = "release/forex-ci-attestation-public.pem"
PRIVATE_KEY_ENV = "FOREX_CI_ATTESTATION_PRIVATE_KEY"
DEFAULT_REPOSITORY = "Fargolff/Forex-trade"
DEFAULT_WORKFLOW = "Forex Auto Trader Tests"
DEFAULT_WORKFLOW_PATH = ".github/workflows/tests.yml"
DEFAULT_MAIN_REF = "refs/heads/main"
DEFAULT_SOAK_CYCLES = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(value: str) -> str:
    text = str(value).strip().lower()
    if len(text) not in (40, 64) or any(char not in string.hexdigits for char in text):
        raise ValueError("CI source_commit must be a full 40- or 64-character hexadecimal commit identifier")
    return text


def _path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError("CI attestation private key must remain outside the project root")
    return path


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def source_tree_report(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    found: dict[str, Path] = {}
    for pattern in DEPLOYMENT_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                found[path.relative_to(root).as_posix()] = path
    entries = [
        {"path": relative, "size": found[relative].stat().st_size, "sha256": sha256_file(found[relative])}
        for relative in sorted(found)
    ]
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "entries_count": len(entries),
        "entries": entries,
    }


def workflow_report(root: str | Path, workflow_path: str | Path = DEFAULT_WORKFLOW_PATH) -> dict[str, Any]:
    root = Path(root).resolve()
    target = _path(root, workflow_path)
    if not target.is_file():
        raise FileNotFoundError(f"CI workflow file is missing: {target}")
    return {
        "path": target.relative_to(root).as_posix(),
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
    }


def create_ci_attestation(
    root: str | Path,
    output_path: str | Path = DEFAULT_ATTESTATION,
    *,
    source_commit: str,
    repository: str = DEFAULT_REPOSITORY,
    workflow: str = DEFAULT_WORKFLOW,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    run_id: str | int,
    run_attempt: str | int = 1,
    run_url: str,
    event_name: str = "push",
    ref: str = DEFAULT_MAIN_REF,
    pytest_status: str = "passed",
    soak_status: str = "passed",
    soak_cycles: int = DEFAULT_SOAK_CYCLES,
) -> dict[str, Any]:
    root = Path(root).resolve()
    commit = _commit(source_commit)
    if not str(repository).strip() or not str(workflow).strip():
        raise ValueError("repository and workflow are required")
    if int(run_attempt) < 1 or int(soak_cycles) < 0:
        raise ValueError("run_attempt must be >=1 and soak_cycles cannot be negative")
    document = {
        "version": ATTESTATION_VERSION,
        "format": ATTESTATION_FORMAT,
        "created_at": _now(),
        "repository": str(repository).strip(),
        "source_commit": commit,
        "source_tree": source_tree_report(root),
        "workflow": {
            "name": str(workflow).strip(),
            **workflow_report(root, workflow_path),
            "run_id": str(run_id),
            "run_attempt": int(run_attempt),
            "run_url": str(run_url).strip(),
            "event_name": str(event_name).strip(),
            "ref": str(ref).strip(),
        },
        "checks": {
            "pytest": {"status": str(pytest_status).strip().lower(), "command": "python -m pytest -q"},
            "operational_soak": {
                "status": str(soak_status).strip().lower(),
                "command": f"python -m src.soak --cycles {int(soak_cycles)}",
                "cycles": int(soak_cycles),
            },
        },
    }
    _atomic_json(_path(root, output_path), document)
    return document


def sign_ci_attestation(
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


def verify_ci_attestation(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    signature_path: str | Path = DEFAULT_SIGNATURE,
    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,
    trust_store_path: str | Path | None = None,
    *,
    expected_source_commit: str | None = None,
    expected_repository: str = DEFAULT_REPOSITORY,
    expected_workflow: str = DEFAULT_WORKFLOW,
    required_soak_cycles: int = DEFAULT_SOAK_CYCLES,
    require_main_ref: bool = True,
) -> dict[str, Any]:
    root = Path(root).resolve()
    signature = verify_ci_signature(root, attestation_path, signature_path, public_key_path, trust_store_path=trust_store_path)
    if not signature["ok"]:
        return {"ok": False, "code": "CI_ATTESTATION_INVALID", "issues": [f"signature:{signature['code']}"], "signature": signature}
    try:
        document = json.loads(_path(root, attestation_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "code": "CI_ATTESTATION_INVALID", "issues": [f"document:{type(exc).__name__}:{exc}"], "signature": signature}

    issues: list[str] = []
    if not isinstance(document, dict) or document.get("version") != ATTESTATION_VERSION or document.get("format") != ATTESTATION_FORMAT:
        issues.append("CI_ATTESTATION_FORMAT_INVALID")
    try:
        commit = _commit(str(document.get("source_commit", "")))
    except Exception as exc:
        commit = ""
        issues.append(f"CI_SOURCE_COMMIT_INVALID:{type(exc).__name__}")
    if expected_source_commit is not None and commit != _commit(expected_source_commit):
        issues.append("CI_SOURCE_COMMIT_MISMATCH")
    if str(document.get("repository", "")) != str(expected_repository):
        issues.append("CI_REPOSITORY_MISMATCH")

    workflow = document.get("workflow") if isinstance(document.get("workflow"), dict) else {}
    if str(workflow.get("name", "")) != str(expected_workflow):
        issues.append("CI_WORKFLOW_MISMATCH")
    if str(workflow.get("event_name", "")) not in {"push", "workflow_dispatch"}:
        issues.append("CI_EVENT_NOT_RELEASE_ELIGIBLE")
    if require_main_ref and str(workflow.get("ref", "")) != DEFAULT_MAIN_REF:
        issues.append("CI_REF_NOT_MAIN")
    try:
        if int(workflow.get("run_attempt", 0)) < 1:
            issues.append("CI_RUN_ATTEMPT_INVALID")
    except Exception:
        issues.append("CI_RUN_ATTEMPT_INVALID")
    if not str(workflow.get("run_id", "")).strip() or not str(workflow.get("run_url", "")).strip():
        issues.append("CI_RUN_IDENTITY_MISSING")

    checks = document.get("checks") if isinstance(document.get("checks"), dict) else {}
    pytest_check = checks.get("pytest") if isinstance(checks.get("pytest"), dict) else {}
    soak_check = checks.get("operational_soak") if isinstance(checks.get("operational_soak"), dict) else {}
    if str(pytest_check.get("status", "")).lower() != "passed":
        issues.append("CI_PYTEST_NOT_PASSED")
    if str(soak_check.get("status", "")).lower() != "passed":
        issues.append("CI_SOAK_NOT_PASSED")
    try:
        if int(soak_check.get("cycles", -1)) < int(required_soak_cycles):
            issues.append("CI_SOAK_CYCLES_INSUFFICIENT")
    except Exception:
        issues.append("CI_SOAK_CYCLES_INVALID")

    actual_tree = source_tree_report(root)
    signed_tree = document.get("source_tree") if isinstance(document.get("source_tree"), dict) else {}
    if str(signed_tree.get("sha256", "")) != actual_tree["sha256"]:
        issues.append("CI_SOURCE_TREE_MISMATCH")
    if int(signed_tree.get("entries_count", -1)) != actual_tree["entries_count"]:
        issues.append("CI_SOURCE_TREE_ENTRY_COUNT_MISMATCH")
    if signed_tree.get("entries") != actual_tree["entries"]:
        issues.append("CI_SOURCE_TREE_ENTRIES_MISMATCH")

    try:
        current_workflow = workflow_report(root, str(workflow.get("path", DEFAULT_WORKFLOW_PATH)))
        if str(workflow.get("sha256", "")) != current_workflow["sha256"]:
            issues.append("CI_WORKFLOW_FILE_MISMATCH")
        if int(workflow.get("size", -1)) != current_workflow["size"]:
            issues.append("CI_WORKFLOW_FILE_SIZE_MISMATCH")
    except Exception as exc:
        issues.append(f"CI_WORKFLOW_FILE_INVALID:{type(exc).__name__}:{exc}")

    return {
        "ok": not issues,
        "code": "CI_ATTESTATION_VALID" if not issues else "CI_ATTESTATION_INVALID",
        "issues": issues,
        "source_commit": commit,
        "repository": document.get("repository"),
        "workflow": workflow.get("name"),
        "run_id": workflow.get("run_id"),
        "run_attempt": workflow.get("run_attempt"),
        "run_url": workflow.get("run_url"),
        "event_name": workflow.get("event_name"),
        "ref": workflow.get("ref"),
        "source_tree_sha256": actual_tree["sha256"],
        "public_key_fingerprint": signature.get("public_key_fingerprint"),
        "key_id": signature.get("key_id"),
        "signature": signature,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 28 CI attestation and source-tree provenance")
    parser.add_argument("--mode", choices=["generate-keypair", "create", "sign", "verify"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--attestation", default=DEFAULT_ATTESTATION)
    parser.add_argument("--signature", default=DEFAULT_SIGNATURE)
    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--trust-store", default=None)
    parser.add_argument("--private-key")
    parser.add_argument("--source-commit")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--expected-repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--expected-workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--workflow-path", default=DEFAULT_WORKFLOW_PATH)
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt", type=int, default=1)
    parser.add_argument("--run-url")
    parser.add_argument("--event-name", default="push")
    parser.add_argument("--ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--pytest-status", default="passed")
    parser.add_argument("--soak-status", default="passed")
    parser.add_argument("--soak-cycles", type=int, default=DEFAULT_SOAK_CYCLES)
    args = parser.parse_args()
    root = Path(args.root).resolve()

    if args.mode == "generate-keypair":
        if not args.private_key:
            raise ValueError("generate-keypair requires --private-key")
        result = generate_keypair(args.private_key, _path(root, args.public_key))
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "create":
        if not args.source_commit or not args.run_id or not args.run_url:
            raise ValueError("create requires --source-commit, --run-id and --run-url")
        result = create_ci_attestation(
            root,
            args.attestation,
            source_commit=args.source_commit,
            repository=args.repository,
            workflow=args.workflow,
            workflow_path=args.workflow_path,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            run_url=args.run_url,
            event_name=args.event_name,
            ref=args.ref,
            pytest_status=args.pytest_status,
            soak_status=args.soak_status,
            soak_cycles=args.soak_cycles,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "sign":
        result = sign_ci_attestation(root, args.attestation, args.private_key, args.signature, trust_store_path=args.trust_store)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    result = verify_ci_attestation(
        root,
        args.attestation,
        args.signature,
        args.public_key,
        args.trust_store,
        expected_source_commit=args.expected_source_commit,
        expected_repository=args.expected_repository,
        expected_workflow=args.expected_workflow,
        required_soak_cycles=args.soak_cycles,
        require_main_ref=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
