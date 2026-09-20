from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from .recovery import verify_deployment_manifest


PRIVATE_KEY_ENV = "FOREX_RELEASE_PRIVATE_KEY"
PUBLIC_KEY_ENV = "FOREX_RELEASE_PUBLIC_KEY"
DEFAULT_MANIFEST = "release/release_manifest.json"
DEFAULT_SIGNATURE = "release/release_signature.json"
DEFAULT_PUBLIC_KEY = "release/forex-release-public.pem"


def _read_bytes(path: str | Path) -> bytes:
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(str(target))
    return target.read_bytes()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def public_key_fingerprint(public_key: Ed25519PublicKey) -> str:
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _sha256(der)


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    raw = _read_bytes(path)
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("release private key must be Ed25519")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    raw = _read_bytes(path)
    key = serialization.load_pem_public_key(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("release public key must be Ed25519")
    return key


def generate_keypair(private_key_path: str | Path, public_key_path: str | Path) -> dict[str, str]:
    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    if private_path.exists() or public_path.exists():
        raise FileExistsError("refusing to overwrite an existing release key")

    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    if os.name != "nt":
        private_path.chmod(0o600)

    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return {
        "private_key": str(private_path),
        "public_key": str(public_path),
        "public_key_fingerprint": public_key_fingerprint(public_key),
    }


def sign_manifest(
    manifest_path: str | Path,
    private_key_path: str | Path,
    signature_path: str | Path,
    *,
    key_id: str | None = None,
) -> dict[str, Any]:
    manifest_bytes = _read_bytes(manifest_path)
    private_key = load_private_key(private_key_path)
    public_key = private_key.public_key()
    signature = private_key.sign(manifest_bytes)
    payload = {
        "version": 2 if key_id else 1,
        "algorithm": "Ed25519",
        "manifest_sha256": _sha256(manifest_bytes),
        "public_key_fingerprint": public_key_fingerprint(public_key),
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    if key_id:
        payload["key_id"] = str(key_id)
    target = Path(signature_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_signature(
    manifest_path: str | Path,
    signature_path: str | Path,
    public_key_path: str | Path,
) -> dict[str, Any]:
    manifest_bytes = _read_bytes(manifest_path)
    public_key = load_public_key(public_key_path)
    signature_doc = json.loads(Path(signature_path).read_text(encoding="utf-8"))
    if not isinstance(signature_doc, dict):
        raise ValueError("release signature document must be a JSON object")
    version = signature_doc.get("version")
    if version not in (1, 2) or signature_doc.get("algorithm") != "Ed25519":
        raise ValueError("unsupported release signature format")
    if version == 2 and not str(signature_doc.get("key_id", "")).strip():
        return {"ok": False, "code": "KEY_ID_MISSING"}

    expected_hash = _sha256(manifest_bytes)
    if str(signature_doc.get("manifest_sha256", "")) != expected_hash:
        return {
            "ok": False,
            "code": "MANIFEST_HASH_MISMATCH",
            "expected_manifest_sha256": expected_hash,
        }

    actual_fingerprint = public_key_fingerprint(public_key)
    if str(signature_doc.get("public_key_fingerprint", "")) != actual_fingerprint:
        return {
            "ok": False,
            "code": "PUBLIC_KEY_FINGERPRINT_MISMATCH",
            "public_key_fingerprint": actual_fingerprint,
        }

    try:
        signature = base64.b64decode(str(signature_doc.get("signature_b64", "")), validate=True)
    except Exception:
        return {"ok": False, "code": "SIGNATURE_ENCODING_INVALID"}

    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature:
        return {"ok": False, "code": "SIGNATURE_INVALID"}

    return {
        "ok": True,
        "code": "SIGNATURE_VALID",
        "manifest_sha256": expected_hash,
        "public_key_fingerprint": actual_fingerprint,
        "signed_at": signature_doc.get("signed_at"),
        "key_id": signature_doc.get("key_id"),
    }


def verify_release(
    root: str | Path,
    manifest_path: str | Path,
    signature_path: str | Path,
    public_key_path: str | Path,
) -> dict[str, Any]:
    signature = verify_signature(manifest_path, signature_path, public_key_path)
    if not signature["ok"]:
        return {"ok": False, "signature": signature, "deployment": None}

    deployment = verify_deployment_manifest(root, manifest_path)
    return {
        "ok": bool(deployment["ok"]),
        "signature": signature,
        "deployment": deployment,
    }


def _key_arg(explicit: str | None, env_name: str) -> str:
    value = (explicit or os.getenv(env_name, "")).strip()
    if not value:
        raise ValueError(f"key path is required via argument or {env_name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 11 cryptographic release signing and verification")
    parser.add_argument(
        "--mode",
        choices=["generate-keypair", "sign", "verify", "fingerprint"],
        required=True,
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--signature", default=DEFAULT_SIGNATURE)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--public-key", default=None)
    args = parser.parse_args()

    if args.mode == "generate-keypair":
        private_path = _key_arg(args.private_key, PRIVATE_KEY_ENV)
        public_path = _key_arg(args.public_key, PUBLIC_KEY_ENV)
        result = generate_keypair(private_path, public_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.mode == "sign":
        private_path = _key_arg(args.private_key, PRIVATE_KEY_ENV)
        result = sign_manifest(args.manifest, private_path, args.signature)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    public_path = _key_arg(args.public_key, PUBLIC_KEY_ENV)
    if args.mode == "fingerprint":
        key = load_public_key(public_path)
        print(public_key_fingerprint(key))
        return

    result = verify_release(args.root, args.manifest, args.signature, public_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
