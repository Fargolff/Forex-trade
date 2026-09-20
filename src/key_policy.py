from __future__ import annotations

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
ALLOWED_ROLES = {"release", "ci_attestation", "deployment_approval"}
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
