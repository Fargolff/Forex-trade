from __future__ import annotations

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
        raise RuntimeError(f'marker not found in {path}: {old[:140]!r}')
    target.write_text(text.replace(old, new, 1), encoding='utf-8')


runtime_liveness = r'''from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature

from .audit_ledger import (
    DEFAULT_REPLICA_SUBDIR,
    LEDGER_ENTRY_FORMAT,
    LEDGER_ENTRY_VERSION,
    LEDGER_HEAD_FORMAT,
    LEDGER_HEAD_VERSION,
    REPLICA_ROOT_ENV,
    REPLICA_SUBDIR_ENV,
    verify_audit_ledger,
)
from .key_policy import DEFAULT_TRUST_STORE, authorize_signing_key, resolve_verification_key
from .recovery import sha256_file
from .release import load_private_key, load_public_key, public_key_fingerprint
from .runtime_boot import BOOT_FORMAT, BOOT_VERSION, DEFAULT_BOOT_RECEIPT, PRIVATE_KEY_ENV

LIVENESS_FORMAT = "forex-auto-trader-runtime-liveness-checkpoint"
LIVENESS_VERSION = 1
LIVENESS_HEAD_FORMAT = "forex-auto-trader-runtime-liveness-head"
LIVENESS_HEAD_VERSION = 1
REQUIRE_ENV = "FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER"
ENVIRONMENT_ID_ENV = "FOREX_DEPLOYMENT_ENVIRONMENT_ID"
MACHINE_ID_ENV = "FOREX_RUNTIME_MACHINE_ID"
TRUST_STORE_ENV = "FOREX_SIGNING_KEY_TRUST_STORE"
DEFAULT_VERIFY_MAX_AGE_SECONDS = 180.0
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


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


def _stage(value: str) -> str:
    text = str(value).strip().upper()
    if not _STAGE.fullmatch(text):
        raise ValueError("stage must be an uppercase identifier up to 32 characters")
    return text


def _safe_relative(value: str | Path, field: str) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"{field} must be a safe project-relative path: {value!r}")
    return path.as_posix()


def _project_path(root: Path, value: str | Path, field: str) -> Path:
    relative = _safe_relative(value, field)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"{field} escapes project root: {value!r}")
    return target


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    project = root.resolve()
    if path == project or project in path.parents:
        raise ValueError("runtime liveness private key must remain outside the project root")
    return path


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _sign_checkpoint(
    root: Path,
    trust_store_path: str | Path,
    private_key_path: str | Path,
    checkpoint: Path,
    moment: datetime,
) -> dict[str, Any]:
    private_path = _outside_private_key(root, private_key_path)
    private = load_private_key(private_path)
    fingerprint = public_key_fingerprint(private.public_key())
    policy = authorize_signing_key(
        root,
        trust_store_path,
        role="runtime_boot",
        fingerprint=fingerprint,
        at_time=moment,
    )
    payload = checkpoint.read_bytes()
    return {
        "version": 2,
        "algorithm": "Ed25519",
        "document": LIVENESS_FORMAT,
        "checkpoint_sha256": hashlib.sha256(payload).hexdigest(),
        "key_id": policy["key_id"],
        "public_key_fingerprint": fingerprint,
        "signed_at": _iso(moment),
        "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
    }


def _verify_checkpoint_signature(
    root: Path,
    trust_store_path: str | Path,
    checkpoint: Path,
    signature_path: Path,
) -> dict[str, Any]:
    try:
        payload = checkpoint.read_bytes()
        signature = _load_json(signature_path, "liveness checkpoint signature")
        if (
            signature.get("version") != 2
            or signature.get("algorithm") != "Ed25519"
            or signature.get("document") != LIVENESS_FORMAT
        ):
            return {"ok": False, "code": "LIVENESS_SIGNATURE_FORMAT_INVALID"}
        if signature.get("checkpoint_sha256") != hashlib.sha256(payload).hexdigest():
            return {"ok": False, "code": "LIVENESS_SIGNATURE_HASH_MISMATCH"}
        policy = resolve_verification_key(
            root,
            trust_store_path,
            role="runtime_boot",
            key_id=str(signature.get("key_id", "")),
            fingerprint=str(signature.get("public_key_fingerprint", "")),
            signed_at=str(signature.get("signed_at", "")),
        )
        public = load_public_key(policy["public_key_path"])
        encoded = base64.b64decode(str(signature.get("signature_b64", "")), validate=True)
        public.verify(encoded, payload)
        return {
            "ok": True,
            "code": "LIVENESS_SIGNATURE_VALID",
            "key_id": policy["key_id"],
            "signed_at": signature.get("signed_at"),
        }
    except InvalidSignature:
        return {"ok": False, "code": "LIVENESS_SIGNATURE_INVALID"}
    except Exception as exc:
        return {"ok": False, "code": f"LIVENESS_SIGNATURE_ERROR:{type(exc).__name__}:{exc}"}


def _audit_context(
    root: Path,
    *,
    environment_id: str,
    machine_id: str,
    replica_root: str | Path | None,
    replica_subdir: str,
    trust_store_path: str | Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, dict[str, Any]]]:
    report = verify_audit_ledger(
        root,
        environment_id=environment_id,
        machine_id=machine_id,
        replica_root=replica_root,
        replica_subdir=replica_subdir,
        trust_store_path=trust_store_path,
    )
    if not report.get("ok"):
        raise RuntimeError(f"LIVENESS_AUDIT_LEDGER_INVALID:{report.get('issues')}")
    scope = Path(str(report.get("scope", ""))).resolve()
    head = _load_json(scope / "head.json", "audit ledger head")
    if head.get("version") != LEDGER_HEAD_VERSION or head.get("format") != LEDGER_HEAD_FORMAT:
        raise RuntimeError("LIVENESS_AUDIT_HEAD_FORMAT_INVALID")

    mapping: dict[str, dict[str, Any]] = {}
    entries_root = scope / "entries"
    entry_dirs = sorted([item for item in entries_root.iterdir() if item.is_dir() and not item.name.startswith(".tmp-")])
    for entry_dir in entry_dirs:
        manifest = entry_dir / "ledger_entry.json"
        doc = _load_json(manifest, "audit ledger entry")
        if doc.get("version") != LEDGER_ENTRY_VERSION or doc.get("format") != LEDGER_ENTRY_FORMAT:
            raise RuntimeError(f"LIVENESS_AUDIT_ENTRY_FORMAT_INVALID:{entry_dir.name}")
        boot_id = _identity(str(doc.get("entry_id", "")), "boot_id")
        if boot_id in mapping:
            raise RuntimeError(f"LIVENESS_AUDIT_BOOT_DUPLICATE:{boot_id}")
        evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
        boot_binding = evidence.get("boot_receipt") if isinstance(evidence.get("boot_receipt"), dict) else {}
        boot_hash = str(boot_binding.get("sha256", ""))
        if len(boot_hash) != 64:
            raise RuntimeError(f"LIVENESS_AUDIT_BOOT_HASH_INVALID:{boot_id}")
        chain = doc.get("chain") if isinstance(doc.get("chain"), dict) else {}
        mapping[boot_id] = {
            "audit_sequence": int(doc.get("sequence", 0)),
            "audit_entry_manifest_sha256": sha256_file(manifest),
            "boot_receipt_sha256": boot_hash,
            "source_commit": str(doc.get("source_commit", "")),
            "release_id": str(doc.get("release_id", "")),
            "audit_chain_mode": str(chain.get("mode", "")),
        }
    return report, scope, head, mapping


def _current_boot(
    root: Path,
    *,
    boot_receipt_path: str | Path,
    environment_id: str,
    machine_id: str,
) -> tuple[dict[str, Any], Path, str]:
    path = _project_path(root, boot_receipt_path, "boot_receipt")
    if not path.is_file():
        raise FileNotFoundError(path)
    document = _load_json(path, "runtime boot receipt")
    if document.get("version") != BOOT_VERSION or document.get("format") != BOOT_FORMAT or document.get("status") != "BOOT_ATTESTED":
        raise RuntimeError("LIVENESS_BOOT_FORMAT_INVALID")
    environment = document.get("environment") if isinstance(document.get("environment"), dict) else {}
    machine = document.get("machine") if isinstance(document.get("machine"), dict) else {}
    if environment.get("id") != environment_id:
        raise RuntimeError("LIVENESS_BOOT_ENVIRONMENT_MISMATCH")
    if machine.get("id") != machine_id:
        raise RuntimeError("LIVENESS_BOOT_MACHINE_MISMATCH")
    boot_id = _identity(str(document.get("boot_id", "")), "boot_id")
    return document, path, boot_id


def _liveness_root(audit_scope: Path) -> Path:
    return audit_scope / "liveness"


def _load_liveness_head(root: Path) -> dict[str, Any] | None:
    path = root / "head.json"
    if not path.exists():
        return None
    head = _load_json(path, "runtime liveness head")
    if head.get("version") != LIVENESS_HEAD_VERSION or head.get("format") != LIVENESS_HEAD_FORMAT:
        raise RuntimeError("LIVENESS_HEAD_FORMAT_INVALID")
    return head


def append_liveness_checkpoint(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    stage: str,
    cycle: int,
    health: dict[str, Any],
    private_key_path: str | Path | None = None,
    replica_root: str | Path | None = None,
    replica_subdir: str = DEFAULT_REPLICA_SUBDIR,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    boot_receipt_path: str | Path = DEFAULT_BOOT_RECEIPT,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    stage_name = _stage(stage)
    if int(cycle) < 0:
        raise ValueError("cycle must be non-negative")
    if not isinstance(health, dict):
        raise ValueError("health must be a JSON object")
    moment = (now or _now()).astimezone(timezone.utc)
    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"runtime boot private key is required via argument or {PRIVATE_KEY_ENV}")

    audit_report, audit_scope, audit_head, audit_map = _audit_context(
        root,
        environment_id=env_id,
        machine_id=machine,
        replica_root=replica_root,
        replica_subdir=replica_subdir,
        trust_store_path=trust_store_path,
    )
    boot_doc, boot_path, boot_id = _current_boot(
        root,
        boot_receipt_path=boot_receipt_path,
        environment_id=env_id,
        machine_id=machine,
    )
    boot_hash = sha256_file(boot_path)
    if str(audit_head.get("boot_id", "")) != boot_id:
        raise RuntimeError("LIVENESS_BOOT_NOT_AUDIT_HEAD")
    audit_anchor = audit_map.get(boot_id)
    if audit_anchor is None:
        raise RuntimeError("LIVENESS_BOOT_NOT_REPLICATED")
    if audit_anchor["boot_receipt_sha256"] != boot_hash:
        raise RuntimeError("LIVENESS_BOOT_HASH_MISMATCH")

    release = boot_doc.get("release") if isinstance(boot_doc.get("release"), dict) else {}
    if str(release.get("source_commit", "")) != audit_anchor["source_commit"]:
        raise RuntimeError("LIVENESS_SOURCE_COMMIT_MISMATCH")
    if str(release.get("release_id", "")) != audit_anchor["release_id"]:
        raise RuntimeError("LIVENESS_RELEASE_ID_MISMATCH")

    liveness = _liveness_root(audit_scope)
    entries = liveness / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    head = _load_liveness_head(liveness)
    if head is not None:
        existing = verify_liveness_ledger(
            root,
            environment_id=env_id,
            machine_id=machine,
            replica_root=replica_root,
            replica_subdir=replica_subdir,
            trust_store_path=trust_store_path,
            max_age_seconds=None,
            require_current_boot=False,
            now=moment,
        )
        if not existing.get("ok"):
            raise RuntimeError(f"LIVENESS_EXISTING_LEDGER_INVALID:{existing.get('issues')}")

    sequence = 1 if head is None else int(head.get("sequence", 0)) + 1
    previous_checkpoint_hash = None if head is None else str(head.get("checkpoint_manifest_sha256", ""))
    if sequence == 1:
        chain_mode = (
            "GENESIS"
            if audit_anchor["audit_sequence"] == 1 and audit_anchor["audit_chain_mode"] == "GENESIS"
            else "ANCHOR"
        )
    else:
        chain_mode = "CONTINUATION"

    checkpoint_id = uuid.uuid4().hex
    entry_name = f"{sequence:08d}-{checkpoint_id}"
    final_dir = entries / entry_name
    if final_dir.exists():
        raise RuntimeError("LIVENESS_CHECKPOINT_COLLISION")
    temp_dir = entries / f".tmp-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=False, exist_ok=False)
    try:
        normalized_health = _json_value(health)
        status = str(normalized_health.get("status", "UNKNOWN")).strip().upper() or "UNKNOWN"
        document = {
            "version": LIVENESS_VERSION,
            "format": LIVENESS_FORMAT,
            "sequence": sequence,
            "checkpoint_id": checkpoint_id,
            "observed_at": _iso(moment),
            "environment_id": env_id,
            "machine_id": machine,
            "stage": stage_name,
            "status": status,
            "cycle": int(cycle),
            "boot_id": boot_id,
            "boot_receipt_sha256": boot_hash,
            "source_commit": audit_anchor["source_commit"],
            "release_id": audit_anchor["release_id"],
            "audit_anchor": {
                "audit_sequence": audit_anchor["audit_sequence"],
                "audit_entry_manifest_sha256": audit_anchor["audit_entry_manifest_sha256"],
            },
            "chain": {
                "mode": chain_mode,
                "previous_checkpoint_sha256": previous_checkpoint_hash,
            },
            "health": normalized_health,
        }
        checkpoint = temp_dir / "checkpoint.json"
        _atomic_json(checkpoint, document)
        signature_doc = _sign_checkpoint(root, trust_store_path, key_value, checkpoint, moment)
        signature = temp_dir / "checkpoint.signature.json"
        _atomic_json(signature, signature_doc)
        temp_dir.replace(final_dir)
        checkpoint_hash = sha256_file(final_dir / "checkpoint.json")
        _atomic_json(
            liveness / "head.json",
            {
                "version": LIVENESS_HEAD_VERSION,
                "format": LIVENESS_HEAD_FORMAT,
                "environment_id": env_id,
                "machine_id": machine,
                "sequence": sequence,
                "entry_dir": entry_name,
                "checkpoint_id": checkpoint_id,
                "checkpoint_manifest_sha256": checkpoint_hash,
                "boot_id": boot_id,
                "status": status,
                "stage": stage_name,
                "observed_at": _iso(moment),
                "updated_at": _iso(moment),
            },
        )
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    verified = verify_liveness_ledger(
        root,
        environment_id=env_id,
        machine_id=machine,
        replica_root=replica_root,
        replica_subdir=replica_subdir,
        trust_store_path=trust_store_path,
        max_age_seconds=300.0,
        require_current_boot=True,
        now=moment,
    )
    if not verified.get("ok"):
        raise RuntimeError(f"LIVENESS_POST_APPEND_VERIFY_FAILED:{verified.get('issues')}")
    return {
        "ok": True,
        "code": "RUNTIME_LIVENESS_APPENDED",
        "sequence": sequence,
        "checkpoint_id": checkpoint_id,
        "boot_id": boot_id,
        "stage": stage_name,
        "status": status,
        "chain_mode": chain_mode,
        "audit_sequence": int(audit_report.get("sequence", audit_anchor["audit_sequence"])),
        "scope": str(liveness),
    }


def verify_liveness_ledger(
    root: str | Path,
    *,
    environment_id: str,
    machine_id: str,
    replica_root: str | Path | None = None,
    replica_subdir: str = DEFAULT_REPLICA_SUBDIR,
    trust_store_path: str | Path = DEFAULT_TRUST_STORE,
    max_age_seconds: float | None = None,
    require_current_boot: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    env_id = _identity(environment_id, "environment_id")
    machine = _identity(machine_id, "machine_id")
    moment = (now or _now()).astimezone(timezone.utc)
    try:
        _, audit_scope, audit_head, audit_map = _audit_context(
            root,
            environment_id=env_id,
            machine_id=machine,
            replica_root=replica_root,
            replica_subdir=replica_subdir,
            trust_store_path=trust_store_path,
        )
    except Exception as exc:
        return {
            "ok": False,
            "code": "RUNTIME_LIVENESS_INVALID",
            "entries": 0,
            "issues": [f"audit:{type(exc).__name__}:{exc}"],
        }

    liveness = _liveness_root(audit_scope)
    try:
        head = _load_liveness_head(liveness)
    except Exception as exc:
        return {"ok": False, "code": "RUNTIME_LIVENESS_INVALID", "entries": 0, "issues": [f"head:{type(exc).__name__}:{exc}"]}
    if head is None:
        return {"ok": False, "code": "RUNTIME_LIVENESS_MISSING", "entries": 0, "issues": ["head:missing"]}

    issues: list[str] = []
    if head.get("environment_id") != env_id:
        issues.append("head:environment")
    if head.get("machine_id") != machine:
        issues.append("head:machine")
    entries_root = liveness / "entries"
    entry_dirs = sorted([item for item in entries_root.iterdir() if item.is_dir() and not item.name.startswith(".tmp-")]) if entries_root.exists() else []
    expected_count = int(head.get("sequence", 0))
    if len(entry_dirs) != expected_count:
        issues.append("entries:count")

    previous_checkpoint_hash: str | None = None
    previous_observed: datetime | None = None
    last_checkpoint_hash: str | None = None
    last_checkpoint_id: str | None = None
    last_boot_id: str | None = None
    last_stage: str | None = None
    last_status: str | None = None
    last_observed: datetime | None = None
    anchored_history = False

    for expected_sequence, entry_dir in enumerate(entry_dirs, start=1):
        checkpoint = entry_dir / "checkpoint.json"
        signature = entry_dir / "checkpoint.signature.json"
        if not checkpoint.is_file() or not signature.is_file():
            issues.append(f"entry:{expected_sequence}:files")
            continue
        try:
            document = _load_json(checkpoint, "runtime liveness checkpoint")
        except Exception as exc:
            issues.append(f"entry:{expected_sequence}:json:{type(exc).__name__}")
            continue
        if document.get("version") != LIVENESS_VERSION or document.get("format") != LIVENESS_FORMAT:
            issues.append(f"entry:{expected_sequence}:format")
        if int(document.get("sequence", -1)) != expected_sequence:
            issues.append(f"entry:{expected_sequence}:sequence")
        if document.get("environment_id") != env_id or document.get("machine_id") != machine:
            issues.append(f"entry:{expected_sequence}:identity")
        if not entry_dir.name.startswith(f"{expected_sequence:08d}-"):
            issues.append(f"entry:{expected_sequence}:directory_sequence")
        try:
            _stage(str(document.get("stage", "")))
        except Exception:
            issues.append(f"entry:{expected_sequence}:stage")
        try:
            if int(document.get("cycle", -1)) < 0:
                raise ValueError
        except Exception:
            issues.append(f"entry:{expected_sequence}:cycle")
        if not isinstance(document.get("health"), dict):
            issues.append(f"entry:{expected_sequence}:health")

        signed = _verify_checkpoint_signature(root, trust_store_path, checkpoint, signature)
        if not signed.get("ok"):
            issues.append(f"entry:{expected_sequence}:signature:{signed.get('code')}")

        boot_id = str(document.get("boot_id", ""))
        anchor = audit_map.get(boot_id)
        if anchor is None:
            issues.append(f"entry:{expected_sequence}:audit_anchor:boot")
        else:
            audit_anchor = document.get("audit_anchor") if isinstance(document.get("audit_anchor"), dict) else {}
            if int(audit_anchor.get("audit_sequence", -1)) != int(anchor["audit_sequence"]):
                issues.append(f"entry:{expected_sequence}:audit_anchor:sequence")
            if audit_anchor.get("audit_entry_manifest_sha256") != anchor["audit_entry_manifest_sha256"]:
                issues.append(f"entry:{expected_sequence}:audit_anchor:manifest_sha256")
            if document.get("boot_receipt_sha256") != anchor["boot_receipt_sha256"]:
                issues.append(f"entry:{expected_sequence}:audit_anchor:boot_sha256")
            if document.get("source_commit") != anchor["source_commit"]:
                issues.append(f"entry:{expected_sequence}:source_commit")
            if document.get("release_id") != anchor["release_id"]:
                issues.append(f"entry:{expected_sequence}:release_id")

        chain = document.get("chain") if isinstance(document.get("chain"), dict) else {}
        if expected_sequence == 1:
            if chain.get("previous_checkpoint_sha256") not in (None, ""):
                issues.append("entry:1:previous_checkpoint")
            if anchor is not None and anchor["audit_sequence"] == 1 and anchor["audit_chain_mode"] == "GENESIS":
                if chain.get("mode") != "GENESIS":
                    issues.append("entry:1:mode")
            else:
                anchored_history = True
                if chain.get("mode") != "ANCHOR":
                    issues.append("entry:1:mode")
        else:
            if chain.get("mode") != "CONTINUATION":
                issues.append(f"entry:{expected_sequence}:mode")
            if chain.get("previous_checkpoint_sha256") != previous_checkpoint_hash:
                issues.append(f"entry:{expected_sequence}:previous_checkpoint")

        try:
            observed = _time(str(document.get("observed_at", "")), "observed_at")
            if previous_observed is not None and observed < previous_observed:
                issues.append(f"entry:{expected_sequence}:time_regression")
            previous_observed = observed
            last_observed = observed
            if signed.get("ok") and signed.get("signed_at"):
                signed_at = _time(str(signed["signed_at"]), "signed_at")
                if abs((signed_at - observed).total_seconds()) > 60:
                    issues.append(f"entry:{expected_sequence}:signature_time")
        except Exception as exc:
            issues.append(f"entry:{expected_sequence}:time:{type(exc).__name__}")

        previous_checkpoint_hash = sha256_file(checkpoint)
        last_checkpoint_hash = previous_checkpoint_hash
        last_checkpoint_id = str(document.get("checkpoint_id", ""))
        last_boot_id = boot_id
        last_stage = str(document.get("stage", ""))
        last_status = str(document.get("status", ""))

    if expected_count > 0:
        if head.get("checkpoint_manifest_sha256") != last_checkpoint_hash:
            issues.append("head:checkpoint_sha256")
        if head.get("checkpoint_id") != last_checkpoint_id:
            issues.append("head:checkpoint_id")
        if head.get("boot_id") != last_boot_id:
            issues.append("head:boot_id")
        if not entry_dirs or head.get("entry_dir") != entry_dirs[-1].name:
            issues.append("head:entry_dir")
        if head.get("stage") != last_stage:
            issues.append("head:stage")
        if head.get("status") != last_status:
            issues.append("head:status")

    current_boot_covered = bool(last_boot_id) and last_boot_id == str(audit_head.get("boot_id", ""))
    if require_current_boot and not current_boot_covered:
        issues.append("head:current_boot_not_covered")
    if max_age_seconds is not None:
        if float(max_age_seconds) < 0:
            issues.append("freshness:max_age_negative")
        elif last_observed is None:
            issues.append("freshness:no_checkpoint")
        else:
            age = (moment - last_observed).total_seconds()
            if age < -60:
                issues.append("freshness:future")
            elif age > float(max_age_seconds):
                issues.append("freshness:stale")

    return {
        "ok": not issues,
        "code": "RUNTIME_LIVENESS_VALID" if not issues else "RUNTIME_LIVENESS_INVALID",
        "environment_id": env_id,
        "machine_id": machine,
        "entries": len(entry_dirs),
        "sequence": expected_count,
        "latest_boot_id": last_boot_id,
        "current_audit_boot_id": audit_head.get("boot_id"),
        "current_boot_covered": current_boot_covered,
        "latest_stage": last_stage,
        "latest_status": last_status,
        "latest_observed_at": _iso(last_observed) if last_observed is not None else None,
        "anchored_history": anchored_history,
        "issues": issues,
        "scope": str(liveness),
    }


def append_liveness_from_env(
    root: str | Path,
    *,
    stage: str,
    cycle: int,
    health: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    environment_id = os.getenv(ENVIRONMENT_ID_ENV, "").strip()
    machine_id = os.getenv(MACHINE_ID_ENV, "").strip()
    private_key = os.getenv(PRIVATE_KEY_ENV, "").strip()
    replica_root = os.getenv(REPLICA_ROOT_ENV, "").strip()
    replica_subdir = os.getenv(REPLICA_SUBDIR_ENV, DEFAULT_REPLICA_SUBDIR).strip() or DEFAULT_REPLICA_SUBDIR
    trust_store = os.getenv(TRUST_STORE_ENV, DEFAULT_TRUST_STORE).strip() or DEFAULT_TRUST_STORE
    missing = [
        name
        for name, value in (
            (ENVIRONMENT_ID_ENV, environment_id),
            (MACHINE_ID_ENV, machine_id),
            (PRIVATE_KEY_ENV, private_key),
            (REPLICA_ROOT_ENV, replica_root),
        )
        if not value
    ]
    if missing:
        raise ValueError("runtime liveness environment is incomplete: " + ",".join(missing))
    return append_liveness_checkpoint(
        root,
        environment_id=environment_id,
        machine_id=machine_id,
        stage=stage,
        cycle=cycle,
        health=health,
        private_key_path=private_key,
        replica_root=replica_root,
        replica_subdir=replica_subdir,
        trust_store_path=trust_store,
        now=now,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 33 signed runtime liveness checkpoints")
    parser.add_argument("--mode", choices=["append", "verify"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--environment-id")
    parser.add_argument("--machine-id")
    parser.add_argument("--replica-root")
    parser.add_argument("--replica-subdir", default=os.getenv(REPLICA_SUBDIR_ENV, DEFAULT_REPLICA_SUBDIR))
    parser.add_argument("--trust-store", default=os.getenv(TRUST_STORE_ENV, DEFAULT_TRUST_STORE))
    parser.add_argument("--private-key")
    parser.add_argument("--stage", default="MANUAL")
    parser.add_argument("--cycle", type=int, default=0)
    parser.add_argument("--health-file")
    parser.add_argument("--max-age-seconds", type=float)
    parser.add_argument("--require-current-boot", action="store_true")
    args = parser.parse_args()
    environment_id = args.environment_id or os.getenv(ENVIRONMENT_ID_ENV, "")
    machine_id = args.machine_id or os.getenv(MACHINE_ID_ENV, "")
    if args.mode == "append":
        if not args.health_file:
            raise SystemExit("--health-file is required for append mode")
        health = _load_json(Path(args.health_file), "health")
        result = append_liveness_checkpoint(
            args.root,
            environment_id=environment_id,
            machine_id=machine_id,
            stage=args.stage,
            cycle=args.cycle,
            health=health,
            private_key_path=args.private_key,
            replica_root=args.replica_root,
            replica_subdir=args.replica_subdir,
            trust_store_path=args.trust_store,
        )
    else:
        result = verify_liveness_ledger(
            args.root,
            environment_id=environment_id,
            machine_id=machine_id,
            replica_root=args.replica_root,
            replica_subdir=args.replica_subdir,
            trust_store_path=args.trust_store,
            max_age_seconds=args.max_age_seconds,
            require_current_boot=args.require_current_boot,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
'''


tests = r'''from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest

import src.production as production
import src.runtime_liveness as liveness
from src.audit_ledger import (
    DEFAULT_REPLICA_SUBDIR,
    LEDGER_ENTRY_FORMAT,
    LEDGER_ENTRY_VERSION,
    LEDGER_HEAD_FORMAT,
    LEDGER_HEAD_VERSION,
)
from src.key_policy import make_key_record, write_trust_store
from src.release import generate_keypair
from src.runtime_boot import BOOT_FORMAT, BOOT_VERSION


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "runtime_boot") -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "project"
    remote = tmp_path / "remote"
    (root / "release" / "keys").mkdir(parents=True)
    private = tmp_path / "runtime-private.pem"
    public = root / "release" / "keys" / "runtime-public.pem"
    generate_keypair(private, public)
    record = make_key_record(
        root,
        key_id="runtime-boot-2026-q4",
        role=role,
        public_key_path=public,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
    )
    write_trust_store(root, [record])
    scope = remote / DEFAULT_REPLICA_SUBDIR / "prod-bkk-01" / "trader-pc-01"
    (scope / "entries").mkdir(parents=True)

    def audit_report(*args, **kwargs):
        head = json.loads((scope / "head.json").read_text(encoding="utf-8"))
        entries = [item for item in (scope / "entries").iterdir() if item.is_dir()]
        return {
            "ok": True,
            "code": "AUDIT_LEDGER_VALID",
            "scope": str(scope),
            "sequence": int(head["sequence"]),
            "entries": len(entries),
            "head_boot_id": head["boot_id"],
            "issues": [],
        }

    monkeypatch.setattr(liveness, "verify_audit_ledger", audit_report)
    return root, private, remote, scope


def _write_audit_boot(
    root: Path,
    scope: Path,
    *,
    boot_id: str,
    sequence: int,
    mode: str,
    previous_boot_hash: str | None = None,
    set_local: bool = True,
) -> str:
    boot = root / "runtime" / "runtime_boot_receipt.json"
    document = {
        "version": BOOT_VERSION,
        "format": BOOT_FORMAT,
        "status": "BOOT_ATTESTED",
        "boot_id": boot_id,
        "created_at": "2026-09-20T12:00:00+00:00",
        "environment": {"id": "prod-bkk-01"},
        "machine": {"id": "trader-pc-01"},
        "release": {"source_commit": "a" * 40, "release_id": "phase33-release"},
        "chain": {"previous_boot_receipt_sha256": previous_boot_hash},
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    boot_hash = hashlib.sha256(payload).hexdigest()
    if set_local:
        boot.parent.mkdir(parents=True, exist_ok=True)
        boot.write_bytes(payload)

    entry_name = f"{sequence:08d}-{boot_id}"
    entry = scope / "entries" / entry_name
    manifest = entry / "ledger_entry.json"
    audit_doc = {
        "version": LEDGER_ENTRY_VERSION,
        "format": LEDGER_ENTRY_FORMAT,
        "sequence": sequence,
        "entry_id": boot_id,
        "environment_id": "prod-bkk-01",
        "machine_id": "trader-pc-01",
        "source_commit": "a" * 40,
        "release_id": "phase33-release",
        "chain": {"mode": mode},
        "evidence": {"boot_receipt": {"path": "evidence/runtime/runtime_boot_receipt.json", "size": len(payload), "sha256": boot_hash}},
    }
    _json(manifest, audit_doc)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    _json(
        scope / "head.json",
        {
            "version": LEDGER_HEAD_VERSION,
            "format": LEDGER_HEAD_FORMAT,
            "environment_id": "prod-bkk-01",
            "machine_id": "trader-pc-01",
            "sequence": sequence,
            "entry_dir": entry_name,
            "boot_id": boot_id,
            "boot_receipt_sha256": boot_hash,
            "entry_manifest_sha256": manifest_hash,
            "updated_at": "2026-09-20T12:00:00+00:00",
        },
    )
    return boot_hash


def _append(root: Path, private: Path, remote: Path, *, stage: str = "READY", cycle: int = 1, now: datetime | None = None) -> dict:
    return liveness.append_liveness_checkpoint(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        stage=stage,
        cycle=cycle,
        health={"status": "OK", "market_state": "OPEN", "halted": False},
        private_key_path=private,
        replica_root=remote,
        now=now or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def _verify(root: Path, remote: Path, *, now: datetime | None = None, max_age: float | None = None, current: bool = False) -> dict:
    return liveness.verify_liveness_ledger(
        root,
        environment_id="prod-bkk-01",
        machine_id="trader-pc-01",
        replica_root=remote,
        max_age_seconds=max_age,
        require_current_boot=current,
        now=now or datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


def test_first_checkpoint_genesis_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    assert result["code"] == "RUNTIME_LIVENESS_APPENDED"
    assert result["chain_mode"] == "GENESIS"
    report = _verify(root, remote, current=True)
    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["current_boot_covered"] is True


def test_second_checkpoint_preserves_checkpoint_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote, cycle=1)
    second = _append(root, private, remote, stage="POST_CYCLE", cycle=2, now=datetime(2026, 9, 20, 12, 0, 30, tzinfo=timezone.utc))
    assert second["sequence"] == 2
    assert second["chain_mode"] == "CONTINUATION"
    assert _verify(root, remote)["ok"] is True


def test_boot_transition_is_anchored_to_new_audit_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote, cycle=1)
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash)
    result = _append(root, private, remote, cycle=1, now=datetime(2026, 9, 20, 12, 1, tzinfo=timezone.utc))
    assert result["sequence"] == 2
    report = _verify(root, remote, current=True)
    assert report["ok"] is True
    assert report["latest_boot_id"] == "boot002"


def test_first_liveness_checkpoint_can_anchor_existing_audit_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS", set_local=False)
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash)
    result = _append(root, private, remote)
    assert result["chain_mode"] == "ANCHOR"
    assert _verify(root, remote)["anchored_history"] is True


def test_stale_latest_checkpoint_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _append(root, private, remote, now=stamp)
    report = _verify(root, remote, now=stamp + timedelta(minutes=3), max_age=60)
    assert report["ok"] is False
    assert "freshness:stale" in report["issues"]


def test_checkpoint_manifest_tamper_breaks_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    entry = next((Path(result["scope"]) / "entries").iterdir())
    checkpoint = entry / "checkpoint.json"
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    document["status"] = "EVIL"
    _json(checkpoint, document)
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("signature" in issue for issue in report["issues"])


def test_missing_checkpoint_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    result = _append(root, private, remote)
    entry = next((Path(result["scope"]) / "entries").iterdir())
    for item in entry.iterdir():
        item.unlink()
    entry.rmdir()
    report = _verify(root, remote)
    assert report["ok"] is False
    assert "entries:count" in report["issues"]


def test_audit_anchor_tamper_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _append(root, private, remote)
    manifest = next((scope / "entries").iterdir()) / "ledger_entry.json"
    audit_doc = json.loads(manifest.read_text(encoding="utf-8"))
    audit_doc["release_id"] = "tampered"
    _json(manifest, audit_doc)
    report = _verify(root, remote)
    assert report["ok"] is False
    assert any("audit_anchor:manifest_sha256" in issue or "release_id" in issue for issue in report["issues"])


def test_old_process_cannot_publish_after_new_boot_becomes_audit_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch)
    first_hash = _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    _write_audit_boot(root, scope, boot_id="boot002", sequence=2, mode="CONTINUATION", previous_boot_hash=first_hash, set_local=False)
    with pytest.raises(RuntimeError, match="LIVENESS_BOOT_NOT_AUDIT_HEAD"):
        _append(root, private, remote)


def test_wrong_key_role_cannot_sign_liveness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, private, remote, scope = _fixture(tmp_path, monkeypatch, role="release")
    _write_audit_boot(root, scope, boot_id="boot001", sequence=1, mode="GENESIS")
    with pytest.raises(Exception, match="SIGNING_KEY_NOT_TRUSTED"):
        _append(root, private, remote)


def test_production_liveness_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(liveness.REQUIRE_ENV, "1")
    monkeypatch.setattr(production, "append_liveness_from_env", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote unavailable")))
    with pytest.raises(production.RuntimeLivenessError, match="remote unavailable"):
        production._publish_runtime_liveness(
            stage="READY",
            cycle=1,
            symbol="EURUSD",
            report=None,
            snapshot=None,
            production_halt=None,
        )
'''


docs = r'''# Phase 33 — Runtime Heartbeat Attestation & Remote Liveness Ledger

Phase 33 extends the Phase 31/32 startup evidence into an ongoing signed runtime trail. The goal is to prove not only which approved release booted, but that a specific trading machine continued reaching production supervision checkpoints with a stated health/halt condition.

## What is signed

Each checkpoint is an Ed25519-signed `checkpoint.json` using the already managed `runtime_boot` trust role. It binds:

- exact deployment environment ID and machine ID;
- source commit and release ID inherited from the Phase 32 audit entry;
- exact Phase 31 boot ID and boot-receipt SHA-256;
- exact Phase 32 audit-entry sequence and manifest SHA-256;
- monotonic liveness sequence and previous checkpoint SHA-256;
- production cycle number and checkpoint stage;
- normalized runtime health summary (market state, account/equity summary, managed exposure, incidents, live halt state, production halt state).

A checkpoint therefore cannot be detached from its approved boot and replayed onto another machine/release without breaking the anchor checks.

## Runtime behavior

When `FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER=1`, `src.production` publishes:

- `READY` before `engine.process_latest()` on a healthy cycle. Remote append/verification failure at this point is fail-closed and the cycle does not proceed to order evaluation.
- `DEGRADED` when the production guard is WARN and live processing is skipped.
- `HALTED` when the production guard or live engine halts.
- `ERROR` before reconnect handling for an unexpected runtime exception, when the remote ledger remains available.

The supervisor enables Phase 33 by default whenever the Phase 32 remote audit ledger is enabled. A deliberate migration bypass is `FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER=0`.

## Remote layout

Phase 33 reuses `FOREX_AUDIT_LEDGER_ROOT` / `FOREX_AUDIT_LEDGER_SUBDIR` and stores liveness below the existing per-environment/per-machine Phase 32 scope:

```text
<remote>/<subdir>/<environment>/<machine>/
  head.json                  # Phase 32 boot-audit head
  entries/                   # Phase 32 boot evidence
  liveness/
    head.json                # convenience pointer, not the trust root
    entries/
      00000001-<checkpoint-id>/
        checkpoint.json
        checkpoint.signature.json
      ...
```

The verifier recomputes the contiguous checkpoint sequence, validates every detached signature and checkpoint link, then verifies each checkpoint's audit anchor against the already verified Phase 32 ledger.

## Split-brain protection

A process may publish only while its local Phase 31 boot receipt is the current Phase 32 audit head. If a newer boot becomes the audit head, an older process receives `LIVENESS_BOOT_NOT_AUDIT_HEAD` and must stop rather than continuing to publish a competing runtime history.

## Freshness / remote watchdog

A second machine can verify both integrity and recent liveness:

```bash
python -m src.runtime_liveness \
  --mode verify \
  --root . \
  --environment-id prod-bkk-01 \
  --machine-id trader-pc-01 \
  --replica-root /mnt/forex-audit \
  --max-age-seconds 120 \
  --require-current-boot
```

A stale checkpoint is evidence that the remote observer has not seen recent supervision progress; it is not proof of the root cause. Network-share failure, process death, host failure, or deliberate shutdown can all produce staleness.

## Storage boundary

Like Phase 32, append-only behavior is enforced by the application. A privileged storage administrator who can delete the complete tail and consistently rewind mutable metadata cannot be defeated by signatures on that same storage alone. Use WORM/Object Lock/immutable snapshots and/or an independent watcher that remembers the previously observed head for stronger deletion detection.

## Safety

Phase 33 never sends, retries, repairs, resizes, closes, or opens a broker order. Its only trading-path effect is fail-closed gating: a required remote liveness checkpoint must be durably appended and verified before a healthy cycle proceeds to `engine.process_latest()`.
'''


write('src/runtime_liveness.py', runtime_liveness)
write('tests/test_phase33_runtime_liveness.py', tests)
write('docs/phase33-runtime-liveness.md', docs)

replace_once(
    'src/production.py',
    'from .paper import load_portfolio_bundle\n',
    'from .paper import load_portfolio_bundle\nfrom .runtime_liveness import REQUIRE_ENV as RUNTIME_LIVENESS_REQUIRE_ENV, append_liveness_from_env\n',
)

production_helpers = r'''

class RuntimeLivenessError(RuntimeError):
    pass


def _runtime_liveness_enabled() -> bool:
    raw = os.getenv(RUNTIME_LIVENESS_REQUIRE_ENV, "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeLivenessError(f"{RUNTIME_LIVENESS_REQUIRE_ENV} must be 0/1/true/false")


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _runtime_liveness_health(
    *,
    stage: str,
    symbol: str,
    report: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    production_halt: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = report or {}
    ops = report.get("ops") if isinstance(report.get("ops"), dict) else {}
    account = ops.get("account")
    positions = list(ops.get("positions") or [])
    incident_rows = report.get("incidents") if isinstance(report.get("incidents"), list) else []
    incidents = []
    for item in incident_rows:
        if isinstance(item, dict):
            incidents.append({
                "severity": str(item.get("severity", "")),
                "code": str(item.get("code", "")),
            })
    stage_name = str(stage).upper()
    status = str(report.get("status") or "OK").upper()
    if stage_name == "DEGRADED":
        status = "DEGRADED"
    elif stage_name == "HALTED":
        status = "HALTED"
    elif stage_name == "ERROR":
        status = "ERROR"
    live = snapshot or {}
    payload = {
        "status": status,
        "symbol": symbol,
        "market_state": ops.get("market_state"),
        "market_reason": ops.get("market_reason"),
        "spread_pips": _safe_number(ops.get("spread_pips")),
        "tick_age_seconds": _safe_number(ops.get("tick_age_seconds")),
        "bar_age_seconds": _safe_number(ops.get("bar_age_seconds")),
        "account": {
            "login": int(getattr(account, "login", 0) or 0) if account is not None else 0,
            "balance": _safe_number(getattr(account, "balance", None)) if account is not None else None,
            "equity": _safe_number(getattr(account, "equity", None)) if account is not None else None,
            "margin_free": _safe_number(getattr(account, "margin_free", None)) if account is not None else None,
            "margin_level": _safe_number(getattr(account, "margin_level", None)) if account is not None else None,
        },
        "managed_positions": {
            "count": len(positions),
            "total_lots": _safe_number(sum(float(getattr(item, "volume", 0.0) or 0.0) for item in positions)),
        },
        "incidents": incidents,
        "live": {
            "halted": bool(live.get("halted", False)),
            "halt_reason": live.get("halt_reason"),
            "last_bar_time": live.get("last_bar_time"),
            "last_deal_time_msc": int(live.get("last_deal_time_msc", 0) or 0),
            "last_deal_ticket": int(live.get("last_deal_ticket", 0) or 0),
            "pending_order_intent_id": live.get("pending_order_intent_id"),
            "managed_positions": int(live.get("managed_positions", 0) or 0),
            "managed_total_lots": _safe_number(live.get("managed_total_lots")),
        },
        "production_halt": production_halt,
    }
    if extra:
        payload["extra"] = extra
    return payload


def _publish_runtime_liveness(
    *,
    stage: str,
    cycle: int,
    symbol: str,
    report: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    production_halt: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _runtime_liveness_enabled():
        return None
    health = _runtime_liveness_health(
        stage=stage,
        symbol=symbol,
        report=report,
        snapshot=snapshot,
        production_halt=production_halt,
        extra=extra,
    )
    try:
        return append_liveness_from_env(
            Path.cwd(),
            stage=stage,
            cycle=cycle,
            health=health,
        )
    except Exception as exc:
        raise RuntimeLivenessError(f"runtime liveness checkpoint failed: {exc}") from exc
'''
replace_once('src/production.py', '\ndef main() -> None:\n', production_helpers + '\n\ndef main() -> None:\n')

replace_once(
    'src/production.py',
    '''        if report["status"] == "CRITICAL":
            _alert_incidents(alerts, report, app_cfg.symbol)
            halt_store.halt("PRODUCTION_GUARD", ";".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL"))
            raise RuntimeError("production guard is CRITICAL; live engine not started")
''',
    '''        if report["status"] == "CRITICAL":
            _alert_incidents(alerts, report, app_cfg.symbol)
            halt_store.halt("PRODUCTION_GUARD", ";".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL"))
            _publish_runtime_liveness(
                stage="HALTED", cycle=0, symbol=app_cfg.symbol, report=report,
                snapshot=None, production_halt=halt_store.load(),
            )
            raise RuntimeError("production guard is CRITICAL; live engine not started")
''',
)

replace_once(
    'src/production.py',
    '''        while args.max_cycles == 0 or cycles < args.max_cycles:
            cycles += 1
            rotate_runtime_logs(app_cfg, prod_cfg)
            try:
                completed = _completed_from_broker(broker, app_cfg.symbol, app_cfg.timeframe, app_cfg.live.history_bars)
''',
    '''        while args.max_cycles == 0 or cycles < args.max_cycles:
            cycles += 1
            rotate_runtime_logs(app_cfg, prod_cfg)
            report = None
            snapshot = None
            try:
                completed = _completed_from_broker(broker, app_cfg.symbol, app_cfg.timeframe, app_cfg.live.history_bars)
''',
)

replace_once(
    'src/production.py',
    '''                if report["status"] == "CRITICAL":
                    codes = ",".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL")
                    halt_store.halt("PRODUCTION_GUARD", codes)
                    alerts.emit("CRITICAL", "PRODUCTION_HALT", codes, {"cycle": cycles})
                    break
                if report["status"] == "WARN":
                    metrics.save()
                    if args.max_cycles and cycles >= args.max_cycles:
                        break
                    time.sleep(poll_seconds)
                    continue

                snapshot = engine.process_latest(completed)
''',
    '''                if report["status"] == "CRITICAL":
                    codes = ",".join(item["code"] for item in report["incidents"] if item["severity"] == "CRITICAL")
                    halt_store.halt("PRODUCTION_GUARD", codes)
                    alerts.emit("CRITICAL", "PRODUCTION_HALT", codes, {"cycle": cycles})
                    _publish_runtime_liveness(
                        stage="HALTED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=engine.snapshot(), production_halt=halt_store.load(),
                    )
                    break
                if report["status"] == "WARN":
                    _publish_runtime_liveness(
                        stage="DEGRADED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=engine.snapshot(), production_halt=halt_store.load(),
                    )
                    metrics.save()
                    if args.max_cycles and cycles >= args.max_cycles:
                        break
                    time.sleep(poll_seconds)
                    continue

                # Phase 33 fail-closed gate: prove the remote liveness ledger is
                # writable and chained before this cycle can evaluate/send orders.
                _publish_runtime_liveness(
                    stage="READY", cycle=cycles, symbol=app_cfg.symbol, report=report,
                    snapshot=engine.snapshot(), production_halt=halt_store.load(),
                )
                snapshot = engine.process_latest(completed)
''',
)

replace_once(
    'src/production.py',
    '''                metrics.save()
                if snapshot.get("halted"):
                    alerts.emit("CRITICAL", "LIVE_ENGINE_HALTED", str(snapshot.get("halt_reason") or "production halt"))
                    break
            except Exception as exc:
                alerts.emit("CRITICAL", "LIVE_RUNTIME_ERROR", str(exc), {"cycle": cycles})
                if not _reconnect(broker, prod_cfg, alerts):
                    halt_store.halt("RECONNECT_EXHAUSTED", str(exc))
                    break
''',
    '''                metrics.save()
                if snapshot.get("halted"):
                    _publish_runtime_liveness(
                        stage="HALTED", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=snapshot, production_halt=halt_store.load(),
                    )
                    alerts.emit("CRITICAL", "LIVE_ENGINE_HALTED", str(snapshot.get("halt_reason") or "production halt"))
                    break
            except RuntimeLivenessError as exc:
                detail = str(exc)
                halt_store.halt("RUNTIME_LIVENESS_FAILED", detail)
                alerts.emit("CRITICAL", "RUNTIME_LIVENESS_FAILED", detail, {"cycle": cycles})
                break
            except Exception as exc:
                try:
                    _publish_runtime_liveness(
                        stage="ERROR", cycle=cycles, symbol=app_cfg.symbol, report=report,
                        snapshot=snapshot, production_halt=halt_store.load(),
                        extra={"exception": str(exc)},
                    )
                except RuntimeLivenessError as liveness_exc:
                    detail = str(liveness_exc)
                    halt_store.halt("RUNTIME_LIVENESS_FAILED", detail)
                    alerts.emit("CRITICAL", "RUNTIME_LIVENESS_FAILED", detail, {"cycle": cycles})
                    break
                alerts.emit("CRITICAL", "LIVE_RUNTIME_ERROR", str(exc), {"cycle": cycles})
                if not _reconnect(broker, prod_cfg, alerts):
                    halt_store.halt("RECONNECT_EXHAUSTED", str(exc))
                    break
''',
)

supervisor_old = '''$AuditLedgerSubdir = [string]$env:FOREX_AUDIT_LEDGER_SUBDIR
if ([string]::IsNullOrWhiteSpace($AuditLedgerSubdir)) {
    $AuditLedgerSubdir = "Forex-trade/audit-ledger"
}

# Phase 14 broker/local restart reconciliation is fail-closed by default. A
'''
supervisor_new = '''$AuditLedgerSubdir = [string]$env:FOREX_AUDIT_LEDGER_SUBDIR
if ([string]::IsNullOrWhiteSpace($AuditLedgerSubdir)) {
    $AuditLedgerSubdir = "Forex-trade/audit-ledger"
}

# Phase 33 extends Phase 32 with signed runtime checkpoints during every
# supervised-live cycle. It defaults on whenever the remote audit ledger is on.
$RequireRuntimeLivenessRaw = [string]$env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER
if ([string]::IsNullOrWhiteSpace($RequireRuntimeLivenessRaw)) {
    $RuntimeLivenessEnabled = $RemoteAuditEnabled
} else {
    $RuntimeLivenessEnabled = @("1", "true", "yes", "on") -contains $RequireRuntimeLivenessRaw.ToLowerInvariant()
}
if ($RuntimeLivenessEnabled -and -not $RemoteAuditEnabled) {
    throw "FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER requires the Phase 32 remote audit ledger."
}
if ($RuntimeLivenessEnabled) {
    $env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER = "1"
} else {
    $env:FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER = "0"
}

# Phase 14 broker/local restart reconciliation is fail-closed by default. A
'''
replace_once('deploy/windows/run-live-supervisor.ps1', supervisor_old, supervisor_new)

readme = ROOT / 'README.md'
text = readme.read_text(encoding='utf-8')
marker = '## Phase 33 — Runtime Heartbeat Attestation & Remote Liveness Ledger'
if marker not in text:
    text += r'''

## Phase 33 — Runtime Heartbeat Attestation & Remote Liveness Ledger

Supervised live can now publish Ed25519-signed runtime checkpoints into the Phase 32 off-device audit scope. A healthy cycle writes a `READY` checkpoint **before** `engine.process_latest()` so required remote-audit failure blocks the cycle before order evaluation. WARN/CRITICAL/runtime-error paths publish `DEGRADED`, `HALTED`, or `ERROR` evidence when possible. Each checkpoint is chained to the previous checkpoint and anchored to the exact current Phase 32 boot-audit manifest and Phase 31 boot-receipt hash. `FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER` defaults on in the Windows supervisor whenever the remote audit ledger is enabled. See `docs/phase33-runtime-liveness.md`.
'''
    readme.write_text(text, encoding='utf-8')
