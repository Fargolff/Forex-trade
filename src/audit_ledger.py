from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature

from .deployment_approval import DEFAULT_APPROVAL, DEFAULT_APPROVAL_SIGNATURE
from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key
from .recovery import sha256_file
from .release import load_private_key, load_public_key, public_key_fingerprint
from .release_ceremony import DEFAULT_RECEIPT, DEFAULT_RECEIPT_SIGNATURE
from .runtime_boot import (
    BOOT_FORMAT,
    BOOT_VERSION,
    DEFAULT_BOOT_RECEIPT,
    DEFAULT_BOOT_SIGNATURE,
    DEFAULT_MAX_AGE_SECONDS,
    PRIVATE_KEY_ENV,
    verify_runtime_boot_attestation,
)

LEDGER_ENTRY_FORMAT = "forex-auto-trader-remote-audit-ledger-entry"
LEDGER_ENTRY_VERSION = 1
LEDGER_HEAD_FORMAT = "forex-auto-trader-remote-audit-ledger-head"
LEDGER_HEAD_VERSION = 1
REPLICA_ROOT_ENV = "FOREX_AUDIT_LEDGER_ROOT"
REPLICA_SUBDIR_ENV = "FOREX_AUDIT_LEDGER_SUBDIR"
DEFAULT_REPLICA_SUBDIR = "Forex-trade/audit-ledger"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _identity(value: str, field: str) -> str:
    text = str(value).strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    return text


def _safe_relative(value: str | Path, field: str) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"{field} must be a safe relative path: {value!r}")
    return path.as_posix()


def _under(root: Path, value: str | Path, field: str = "path") -> Path:
    relative = _safe_relative(value, field)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"{field} escapes root: {value!r}")
    return target


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError("runtime boot private key must remain outside the project root")
    return path


def _replica_root(project_root: Path, explicit: str | Path | None, subdir: str) -> Path:
    raw = str(explicit).strip() if explicit is not None else os.getenv(REPLICA_ROOT_ENV, "").strip()
    if not raw:
        raise ValueError(f"remote audit ledger root is required via --replica-root or {REPLICA_ROOT_ENV}")
    base = Path(raw).expanduser().resolve()
    project = project_root.resolve()
    if base == project or project in base.parents:
        raise ValueError("remote audit ledger root must be outside the project root")
    relative = _safe_relative(subdir, "replica_subdir")
    return (base / relative).resolve()


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    if temp.exists():
        temp.unlink()
    with source.open("rb") as src, temp.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    temp.replace(target)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _descriptor(path: Path, relative: str) -> dict[str, Any]:
    return {"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)}


def _descriptor_issues(binding: Any, path: Path, label: str) -> list[str]:
    if not isinstance(binding, dict):
        return [f"{label}:binding"]
    issues: list[str] = []
    if not path.is_file():
        return [f"{label}:missing"]
    if path.stat().st_size != int(binding.get("size", -1)):
        issues.append(f"{label}:size")
    if sha256_file(path) != str(binding.get("sha256", "")):
        issues.append(f"{label}:sha256")
    return issues


def _signed_json_report(
    trust_root: Path,
    trust_store_path: str | Path,
    *,
    payload_path: Path,
    signature_path: Path,
    role: str,
    document_name: str,
    hash_field: str,
) -> dict[str, Any]:
    try:
        payload = payload_path.read_bytes()
        signature_doc = _load_json(signature_path, f"{document_name} signature")
        if signature_doc.get("version") != 2 or signature_doc.get("algorithm") != "Ed25519" or signature_doc.get("document") != document_name:
            return {"ok": False, "code": "SIGNATURE_FORMAT_INVALID"}
        if signature_doc.get(hash_field) != hashlib.sha256(payload).hexdigest():
            return {"ok": False, "code": "SIGNATURE_HASH_MISMATCH"}
        policy = resolve_verification_key(
            trust_root,
            trust_store_path,
            role=role,
            key_id=str(signature_doc.get("key_id", "")),
            fingerprint=str(signature_doc.get("public_key_fingerprint", "")),
            signed_at=str(signature_doc.get("signed_at", "")),
        )
        public = load_public_key(policy["public_key_path"])
        signature = base64.b64decode(str(signature_doc.get("signature_b64", "")), validate=True)
        public.verify(signature, payload)
        return {"ok": True, "key_id": policy["key_id"], "fingerprint": policy["fingerprint"]}
    except InvalidSignature:
        return {"ok": False, "code": "SIGNATURE_INVALID"}
    except Exception as exc:
        return {"ok": False, "code": f"SIGNATURE_ERROR:{type(exc).__name__}:{exc}"}


def _evidence_sources(root: Path, *, boot_receipt: str | Path, boot_signature: str | Path, approval: str | Path, approval_signature: str | Path, release_receipt: str | Path, release_receipt_signature: str | Path) -> dict[str, Path]:
    return {
        "boot_receipt": _under(root, boot_receipt, "boot_receipt"),
        "boot_signature": _under(root, boot_signature, "boot_signature"),
        "deployment_approval": _under(root, approval, "deployment_approval"),
        "deployment_approval_signature": _under(root, approval_signature, "deployment_approval_signature"),
        "release_receipt": _under(root, release_receipt, "release_receipt"),
        "release_receipt_signature": _under(root, release_receipt_signature, "release_receipt_signature"),
    }


def _evidence_relatives() -> dict[str, str]:
    return {
        "boot_receipt": "evidence/runtime/runtime_boot_receipt.json",
        "boot_signature": "evidence/runtime/runtime_boot_receipt.signature.json",
        "deployment_approval": "evidence/release/deployment_approval.json",
        "deployment_approval_signature": "evidence/release/deployment_approval.signature.json",
        "release_receipt": "evidence/release/release_receipt.json",
        "release_receipt_signature": "evidence/release/release_receipt.signature.json",
    }


def _ledger_scope(replica_base: Path, environment_id: str, machine_id: str) -> Path:
    return replica_base / _identity(environment_id, "environment_id") / _identity(machine_id, "machine_id")


def _load_head(scope: Path) -> dict[str, Any] | None:
    path = scope / "head.json"
    if not path.exists():
        return None
    head = _load_json(path, "audit ledger head")
    if head.get("version") != LEDGER_HEAD_VERSION or head.get("format") != LEDGER_HEAD_FORMAT:
        raise RuntimeError("AUDIT_LEDGER_HEAD_FORMAT_INVALID")
    return head


def _sign_manifest(project_root: Path, trust_store_path: str | Path, private_key_path: str | Path, manifest: Path, moment: datetime) -> dict[str, Any]:
    private_path = _outside_private_key(project_root, private_key_path)
    private = load_private_key(private_path)
    fingerprint = public_key_fingerprint(private.public_key())
    policy = authorize_signing_key(
        project_root,
        trust_store_path,
        role="runtime_boot",
        fingerprint=fingerprint,
        at_time=moment,
    )
    payload = manifest.read_bytes()
    return {
        "version": 2,
        "algorithm": "Ed25519",
        "document": LEDGER_ENTRY_FORMAT,
        "ledger_entry_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": policy["key_id"],
        "public_key_fingerprint": fingerprint,
        "signed_at": _iso(moment),
        "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
    }


def append_audit_entry(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    private_key_path: str | Path | None = None,
    replica_root: str | Path | None = None,
    replica_subdir: str = DEFAULT_REPLICA_SUBDIR,
    boot_receipt: str | Path = DEFAULT_BOOT_RECEIPT,
    boot_signature: str | Path = DEFAULT_BOOT_SIGNATURE,
    approval: str | Path = DEFAULT_APPROVAL,
    approval_signature: str | Path = DEFAULT_APPROVAL_SIGNATURE,
    release_receipt: str | Path = DEFAULT_RECEIPT,
    release_receipt_signature: str | Path = DEFAULT_RECEIPT_SIGNATURE,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    max_boot_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    moment = (now or _now()).astimezone(timezone.utc)
    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"runtime boot private key is required via argument or {PRIVATE_KEY_ENV}")

    current = verify_runtime_boot_attestation(
        root,
        environment_id=env_id,
        machine_id=machine,
        receipt_path=boot_receipt,
        signature_path=boot_signature,
        approval_path=approval,
        approval_signature_path=approval_signature,
        release_receipt_path=release_receipt,
        release_receipt_signature_path=release_receipt_signature,
        trust_store_path=trust_store_path,
        max_age_seconds=max_boot_age_seconds,
        now=moment,
    )
    if not current.get("ok"):
        raise RuntimeError(f"AUDIT_BOOT_NOT_VERIFIED:{current.get('issues')}")

    sources = _evidence_sources(
        root,
        boot_receipt=boot_receipt,
        boot_signature=boot_signature,
        approval=approval,
        approval_signature=approval_signature,
        release_receipt=release_receipt,
        release_receipt_signature=release_receipt_signature,
    )
    for label, path in sources.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label}:{path}")

    boot_doc = _load_json(sources["boot_receipt"], "runtime boot receipt")
    boot_id = _identity(str(boot_doc.get("boot_id", "")), "boot_id")
    boot_hash = sha256_file(sources["boot_receipt"])
    previous_boot_hash = ((boot_doc.get("chain") or {}) if isinstance(boot_doc.get("chain"), dict) else {}).get("previous_boot_receipt_sha256")

    replica_base = _replica_root(root, replica_root, replica_subdir)
    scope = _ledger_scope(replica_base, env_id, machine)
    entries = scope / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    head = _load_head(scope)

    if head is not None and str(head.get("boot_id")) == boot_id:
        if str(head.get("boot_receipt_sha256")) != boot_hash:
            raise RuntimeError("AUDIT_LEDGER_BOOT_ID_COLLISION")
        report = verify_audit_ledger(
            root,
            environment_id=env_id,
            machine_id=machine,
            replica_root=replica_root,
            replica_subdir=replica_subdir,
            trust_store_path=trust_store_path,
        )
        if not report["ok"]:
            raise RuntimeError(f"AUDIT_LEDGER_EXISTING_INVALID:{report['issues']}")
        return {"ok": True, "code": "AUDIT_LEDGER_ALREADY_APPENDED", "sequence": int(head["sequence"]), "boot_id": boot_id, "scope": str(scope)}

    for candidate in entries.glob(f"*-{boot_id}"):
        if candidate.is_dir():
            raise RuntimeError("AUDIT_LEDGER_REPLAY_OR_FORK")

    if head is None:
        sequence = 1
        previous_manifest_hash = None
        chain_mode = "GENESIS" if previous_boot_hash in (None, "") else "ANCHOR"
    else:
        sequence = int(head.get("sequence", 0)) + 1
        previous_manifest_hash = str(head.get("entry_manifest_sha256", ""))
        chain_mode = "CONTINUATION"
        if previous_boot_hash != head.get("boot_receipt_sha256"):
            raise RuntimeError("AUDIT_LEDGER_BOOT_CHAIN_MISMATCH")

    entry_name = f"{sequence:08d}-{boot_id}"
    final_dir = entries / entry_name
    if final_dir.exists():
        raise RuntimeError("AUDIT_LEDGER_ENTRY_COLLISION")
    temp_dir = entries / f".tmp-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=False, exist_ok=False)
    try:
        relatives = _evidence_relatives()
        evidence: dict[str, dict[str, Any]] = {}
        for label, source in sources.items():
            target = temp_dir / relatives[label]
            _atomic_copy(source, target)
            if sha256_file(target) != sha256_file(source):
                raise RuntimeError(f"AUDIT_LEDGER_COPY_HASH_MISMATCH:{label}")
            evidence[label] = _descriptor(target, relatives[label])

        release = boot_doc.get("release") if isinstance(boot_doc.get("release"), dict) else {}
        manifest_doc = {
            "version": LEDGER_ENTRY_VERSION,
            "format": LEDGER_ENTRY_FORMAT,
            "sequence": sequence,
            "entry_id": boot_id,
            "environment_id": env_id,
            "machine_id": machine,
            "created_at": str(boot_doc.get("created_at", "")),
            "replicated_at": _iso(moment),
            "source_commit": str(release.get("source_commit", "")),
            "release_id": str(release.get("release_id", "")),
            "chain": {
                "mode": chain_mode,
                "previous_entry_manifest_sha256": previous_manifest_hash,
                "previous_boot_receipt_sha256": previous_boot_hash,
            },
            "evidence": evidence,
        }
        manifest = temp_dir / "ledger_entry.json"
        _atomic_json(manifest, manifest_doc)
        signature_doc = _sign_manifest(root, trust_store_path, key_value, manifest, moment)
        signature = temp_dir / "ledger_entry.signature.json"
        _atomic_json(signature, signature_doc)

        temp_dir.replace(final_dir)
        manifest_hash = sha256_file(final_dir / "ledger_entry.json")
        head_doc = {
            "version": LEDGER_HEAD_VERSION,
            "format": LEDGER_HEAD_FORMAT,
            "environment_id": env_id,
            "machine_id": machine,
            "sequence": sequence,
            "entry_dir": entry_name,
            "boot_id": boot_id,
            "boot_receipt_sha256": boot_hash,
            "entry_manifest_sha256": manifest_hash,
            "updated_at": _iso(moment),
        }
        _atomic_json(scope / "head.json", head_doc)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    report = verify_audit_ledger(
        root,
        environment_id=env_id,
        machine_id=machine,
        replica_root=replica_root,
        replica_subdir=replica_subdir,
        trust_store_path=trust_store_path,
    )
    if not report["ok"]:
        raise RuntimeError(f"AUDIT_LEDGER_POST_APPEND_VERIFY_FAILED:{report['issues']}")
    return {"ok": True, "code": "AUDIT_LEDGER_APPENDED", "sequence": sequence, "boot_id": boot_id, "scope": str(scope), "chain_mode": chain_mode}


def verify_audit_ledger(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    replica_root: str | Path | None = None,
    replica_subdir: str = DEFAULT_REPLICA_SUBDIR,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    replica_base = _replica_root(root, replica_root, replica_subdir)
    scope = _ledger_scope(replica_base, env_id, machine)
    issues: list[str] = []
    try:
        head = _load_head(scope)
    except Exception as exc:
        return {"ok": False, "code": "AUDIT_LEDGER_INVALID", "entries": 0, "issues": [f"head:{type(exc).__name__}:{exc}"]}
    if head is None:
        return {"ok": False, "code": "AUDIT_LEDGER_MISSING", "entries": 0, "issues": ["head:missing"]}
    if head.get("environment_id") != env_id:
        issues.append("head:environment")
    if head.get("machine_id") != machine:
        issues.append("head:machine")

    entries_root = scope / "entries"
    entry_dirs = sorted([p for p in entries_root.iterdir() if p.is_dir() and not p.name.startswith(".tmp-")]) if entries_root.exists() else []
    expected_count = int(head.get("sequence", 0))
    if len(entry_dirs) != expected_count:
        issues.append("entries:count")

    previous_manifest_hash: str | None = None
    previous_boot_hash: str | None = None
    last_boot_id = None
    last_manifest_hash = None
    anchor = False
    for expected_sequence, entry_dir in enumerate(entry_dirs, start=1):
        manifest = entry_dir / "ledger_entry.json"
        signature = entry_dir / "ledger_entry.signature.json"
        if not manifest.is_file() or not signature.is_file():
            issues.append(f"entry:{expected_sequence}:files")
            continue
        try:
            doc = _load_json(manifest, "ledger entry")
        except Exception as exc:
            issues.append(f"entry:{expected_sequence}:json:{type(exc).__name__}")
            continue
        if doc.get("version") != LEDGER_ENTRY_VERSION or doc.get("format") != LEDGER_ENTRY_FORMAT:
            issues.append(f"entry:{expected_sequence}:format")
        if int(doc.get("sequence", -1)) != expected_sequence:
            issues.append(f"entry:{expected_sequence}:sequence")
        if doc.get("environment_id") != env_id or doc.get("machine_id") != machine:
            issues.append(f"entry:{expected_sequence}:identity")
        expected_name_prefix = f"{expected_sequence:08d}-"
        if not entry_dir.name.startswith(expected_name_prefix):
            issues.append(f"entry:{expected_sequence}:directory_sequence")

        signed = _signed_json_report(
            root,
            trust_store_path,
            payload_path=manifest,
            signature_path=signature,
            role="runtime_boot",
            document_name=LEDGER_ENTRY_FORMAT,
            hash_field="ledger_entry_sha256",
        )
        if not signed["ok"]:
            issues.append(f"entry:{expected_sequence}:manifest_signature:{signed.get('code')}")

        evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
        relatives = _evidence_relatives()
        evidence_paths: dict[str, Path] = {}
        for label, relative in relatives.items():
            path = entry_dir / relative
            evidence_paths[label] = path
            binding = evidence.get(label)
            if not isinstance(binding, dict) or binding.get("path") != relative:
                issues.append(f"entry:{expected_sequence}:{label}:path")
            issues.extend(f"entry:{expected_sequence}:{item}" for item in _descriptor_issues(binding, path, label))

        boot_path = evidence_paths["boot_receipt"]
        boot_sig_path = evidence_paths["boot_signature"]
        if boot_path.is_file() and boot_sig_path.is_file():
            boot_signed = _signed_json_report(
                root,
                trust_store_path,
                payload_path=boot_path,
                signature_path=boot_sig_path,
                role="runtime_boot",
                document_name=BOOT_FORMAT,
                hash_field="boot_receipt_sha256",
            )
            if not boot_signed["ok"]:
                issues.append(f"entry:{expected_sequence}:boot_signature:{boot_signed.get('code')}")
            try:
                boot_doc = _load_json(boot_path, "boot receipt")
                if boot_doc.get("version") != BOOT_VERSION or boot_doc.get("format") != BOOT_FORMAT or boot_doc.get("status") != "BOOT_ATTESTED":
                    issues.append(f"entry:{expected_sequence}:boot_format")
                if ((boot_doc.get("environment") or {}) if isinstance(boot_doc.get("environment"), dict) else {}).get("id") != env_id:
                    issues.append(f"entry:{expected_sequence}:boot_environment")
                if ((boot_doc.get("machine") or {}) if isinstance(boot_doc.get("machine"), dict) else {}).get("id") != machine:
                    issues.append(f"entry:{expected_sequence}:boot_machine")
                boot_id = str(boot_doc.get("boot_id", ""))
                if boot_id != str(doc.get("entry_id", "")):
                    issues.append(f"entry:{expected_sequence}:boot_id")
                boot_hash = sha256_file(boot_path)
                chain = doc.get("chain") if isinstance(doc.get("chain"), dict) else {}
                boot_chain = boot_doc.get("chain") if isinstance(boot_doc.get("chain"), dict) else {}
                if chain.get("previous_boot_receipt_sha256") != boot_chain.get("previous_boot_receipt_sha256"):
                    issues.append(f"entry:{expected_sequence}:boot_chain_manifest")
                if expected_sequence == 1:
                    if chain.get("previous_entry_manifest_sha256") not in (None, ""):
                        issues.append("entry:1:previous_manifest")
                    if boot_chain.get("previous_boot_receipt_sha256") in (None, ""):
                        if chain.get("mode") != "GENESIS":
                            issues.append("entry:1:mode")
                    else:
                        anchor = True
                        if chain.get("mode") != "ANCHOR":
                            issues.append("entry:1:mode")
                else:
                    if chain.get("mode") != "CONTINUATION":
                        issues.append(f"entry:{expected_sequence}:mode")
                    if chain.get("previous_entry_manifest_sha256") != previous_manifest_hash:
                        issues.append(f"entry:{expected_sequence}:previous_manifest")
                    if boot_chain.get("previous_boot_receipt_sha256") != previous_boot_hash:
                        issues.append(f"entry:{expected_sequence}:previous_boot")

                release_binding = boot_doc.get("release") if isinstance(boot_doc.get("release"), dict) else {}
                approval_binding = boot_doc.get("deployment_approval") if isinstance(boot_doc.get("deployment_approval"), dict) else {}
                for label, binding, key in (
                    ("release_receipt", release_binding.get("receipt"), "release_receipt"),
                    ("release_receipt_signature", release_binding.get("receipt_signature"), "release_receipt_signature"),
                    ("deployment_approval", approval_binding.get("approval"), "deployment_approval"),
                    ("deployment_approval_signature", approval_binding.get("signature"), "deployment_approval_signature"),
                ):
                    issues.extend(f"entry:{expected_sequence}:boot_binding:{item}" for item in _descriptor_issues(binding, evidence_paths[key], label))
                if not ((boot_doc.get("verification") or {}) if isinstance(boot_doc.get("verification"), dict) else {}).get("deployment_approval"):
                    issues.append(f"entry:{expected_sequence}:boot_verification")
                previous_boot_hash = boot_hash
                last_boot_id = boot_id
            except Exception as exc:
                issues.append(f"entry:{expected_sequence}:boot_json:{type(exc).__name__}:{exc}")

        last_manifest_hash = sha256_file(manifest)
        previous_manifest_hash = last_manifest_hash

    if expected_count > 0:
        if head.get("entry_manifest_sha256") != last_manifest_hash:
            issues.append("head:manifest_sha256")
        if head.get("boot_receipt_sha256") != previous_boot_hash:
            issues.append("head:boot_sha256")
        if head.get("boot_id") != last_boot_id:
            issues.append("head:boot_id")
        if not entry_dirs or head.get("entry_dir") != entry_dirs[-1].name:
            issues.append("head:entry_dir")

    return {
        "ok": not issues,
        "code": "AUDIT_LEDGER_VALID" if not issues else "AUDIT_LEDGER_INVALID",
        "environment_id": env_id,
        "machine_id": machine,
        "entries": len(entry_dirs),
        "sequence": expected_count,
        "anchored_history": anchor,
        "head_boot_id": head.get("boot_id"),
        "issues": issues,
        "scope": str(scope),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 32 append-only off-device runtime audit ledger")
    parser.add_argument("--mode", choices=["append", "verify"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--replica-root")
    parser.add_argument("--replica-subdir", default=os.getenv(REPLICA_SUBDIR_ENV, DEFAULT_REPLICA_SUBDIR))
    parser.add_argument("--private-key")
    parser.add_argument("--boot-receipt", default=DEFAULT_BOOT_RECEIPT)
    parser.add_argument("--boot-signature", default=DEFAULT_BOOT_SIGNATURE)
    parser.add_argument("--approval", default=DEFAULT_APPROVAL)
    parser.add_argument("--approval-signature", default=DEFAULT_APPROVAL_SIGNATURE)
    parser.add_argument("--release-receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--release-receipt-signature", default=DEFAULT_RECEIPT_SIGNATURE)
    parser.add_argument("--trust-store", default=DEFAULT_TRUST_STORE)
    parser.add_argument("--max-boot-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args()
    common = dict(
        root=args.root,
        environment_id=args.environment_id,
        machine_id=args.machine_id,
        replica_root=args.replica_root,
        replica_subdir=args.replica_subdir,
        trust_store_path=args.trust_store,
    )
    if args.mode == "append":
        result = append_audit_entry(
            **common,
            private_key_path=args.private_key,
            boot_receipt=args.boot_receipt,
            boot_signature=args.boot_signature,
            approval=args.approval,
            approval_signature=args.approval_signature,
            release_receipt=args.release_receipt,
            release_receipt_signature=args.release_receipt_signature,
            max_boot_age_seconds=args.max_boot_age_seconds,
        )
    else:
        result = verify_audit_ledger(**common)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
