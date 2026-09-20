from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import string
import tempfile
from typing import Any
import zipfile

from cryptography.exceptions import InvalidSignature

from .recovery import sha256_bytes, sha256_file
from .release import load_private_key, load_public_key, public_key_fingerprint, verify_release


BUNDLE_FORMAT = "forex-auto-trader-signed-release-bundle"
BUNDLE_VERSION = 1
BUNDLE_MANIFEST = "release/release_manifest.json"
BUNDLE_RELEASE_SIGNATURE = "release/release_signature.json"
BUNDLE_PUBLIC_KEY = "release/forex-release-public.pem"
BUNDLE_INFO = "release/bundle_info.json"
DEFAULT_BUNDLE = "release/forex-release-bundle.zip"
DEFAULT_BUNDLE_SIGNATURE = "release/forex-release-bundle.signature.json"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _safe_member(value: str) -> str:
    if "\\" in value:
        raise ValueError(f"unsafe bundle member: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe bundle member: {value!r}")
    return path.as_posix()


def _validate_source_commit(value: str) -> str:
    commit = value.strip()
    if not 7 <= len(commit) <= 64 or any(char not in string.hexdigits for char in commit):
        raise ValueError("source_commit must be a 7-64 character hexadecimal commit identifier")
    return commit.lower()


def _validate_release_id(value: str) -> str:
    release_id = value.strip()
    if not release_id or len(release_id) > 128 or any(ord(char) < 32 for char in release_id):
        raise ValueError("release_id must be a non-empty printable value up to 128 characters")
    return release_id


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(_safe_member(name), date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o644 & 0xFFFF) << 16
    return info


def _manifest_document(manifest_path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise ValueError("deployment manifest is invalid")
    return document


def _manifest_members(manifest: dict[str, Any]) -> list[str]:
    members: list[str] = []
    for entry in manifest["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("deployment manifest entry is invalid")
        members.append(_safe_member(str(entry.get("path", ""))))
    if len(members) != len(set(members)):
        raise ValueError("deployment manifest contains duplicate paths")
    return sorted(members)


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    archive.writestr(_zip_info(name), payload)


def build_release_bundle(
    root: str | Path,
    manifest_path: str | Path,
    release_signature_path: str | Path,
    public_key_path: str | Path,
    output_path: str | Path,
    *,
    source_commit: str,
    release_id: str,
) -> dict[str, Any]:
    source_commit = _validate_source_commit(source_commit)
    release_id = _validate_release_id(release_id)
    root_path = Path(root).resolve()
    verified = verify_release(root_path, manifest_path, release_signature_path, public_key_path)
    if not verified["ok"]:
        raise RuntimeError(f"signed release verification failed before bundling: {verified}")

    manifest = _manifest_document(manifest_path)
    deployment_members = _manifest_members(manifest)
    manifest_bytes = Path(manifest_path).read_bytes()
    release_signature_bytes = Path(release_signature_path).read_bytes()
    public_key_bytes = Path(public_key_path).read_bytes()
    fingerprint = public_key_fingerprint(load_public_key(public_key_path))

    bundle_info = {
        "version": BUNDLE_VERSION,
        "format": BUNDLE_FORMAT,
        "source_commit": source_commit,
        "release_id": release_id,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "release_signature_sha256": sha256_bytes(release_signature_bytes),
        "public_key_fingerprint": fingerprint,
        "deployment_entries": len(deployment_members),
    }
    bundle_info_bytes = (json.dumps(bundle_info, indent=2, sort_keys=True) + "\n").encode("utf-8")

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    if temp.exists():
        temp.unlink()

    with zipfile.ZipFile(temp, "w") as archive:
        for relative in deployment_members:
            source = (root_path / relative).resolve()
            if root_path != source and root_path not in source.parents:
                raise ValueError(f"deployment path escapes root: {relative}")
            _write_member(archive, relative, source.read_bytes())
        _write_member(archive, BUNDLE_MANIFEST, manifest_bytes)
        _write_member(archive, BUNDLE_RELEASE_SIGNATURE, release_signature_bytes)
        _write_member(archive, BUNDLE_PUBLIC_KEY, public_key_bytes)
        _write_member(archive, BUNDLE_INFO, bundle_info_bytes)

    temp.replace(target)
    return {
        "ok": True,
        "archive": str(target),
        "sha256": sha256_file(target),
        "source_commit": source_commit,
        "release_id": release_id,
        "entries": len(deployment_members),
        "public_key_fingerprint": fingerprint,
    }


def sign_release_bundle(
    archive_path: str | Path,
    private_key_path: str | Path,
    signature_path: str | Path,
    *,
    key_id: str | None = None,
) -> dict[str, Any]:
    archive_bytes = Path(archive_path).read_bytes()
    private_key = load_private_key(private_key_path)
    public_key = private_key.public_key()
    signature = private_key.sign(archive_bytes)
    payload = {
        "version": 2 if key_id else 1,
        "algorithm": "Ed25519",
        "bundle_sha256": sha256_bytes(archive_bytes),
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


def verify_bundle_signature(
    archive_path: str | Path,
    signature_path: str | Path,
    trusted_public_key_path: str | Path,
) -> dict[str, Any]:
    archive_bytes = Path(archive_path).read_bytes()
    public_key = load_public_key(trusted_public_key_path)
    document = json.loads(Path(signature_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("bundle signature document must be a JSON object")
    version = document.get("version")
    if version not in (1, 2) or document.get("algorithm") != "Ed25519":
        raise ValueError("unsupported bundle signature format")
    if version == 2 and not str(document.get("key_id", "")).strip():
        return {"ok": False, "code": "KEY_ID_MISSING"}

    expected_hash = sha256_bytes(archive_bytes)
    if str(document.get("bundle_sha256", "")) != expected_hash:
        return {"ok": False, "code": "BUNDLE_HASH_MISMATCH", "expected_bundle_sha256": expected_hash}

    fingerprint = public_key_fingerprint(public_key)
    if str(document.get("public_key_fingerprint", "")) != fingerprint:
        return {"ok": False, "code": "PUBLIC_KEY_FINGERPRINT_MISMATCH", "public_key_fingerprint": fingerprint}

    try:
        signature = base64.b64decode(str(document.get("signature_b64", "")), validate=True)
    except Exception:
        return {"ok": False, "code": "SIGNATURE_ENCODING_INVALID"}
    try:
        public_key.verify(signature, archive_bytes)
    except InvalidSignature:
        return {"ok": False, "code": "SIGNATURE_INVALID"}

    return {
        "ok": True,
        "code": "SIGNATURE_VALID",
        "bundle_sha256": expected_hash,
        "public_key_fingerprint": fingerprint,
        "signed_at": document.get("signed_at"),
        "key_id": document.get("key_id"),
    }


def _copy_archive_to_temp(archive: zipfile.ZipFile, target_root: Path) -> list[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("release bundle contains duplicate members")
    copied: list[str] = []
    for raw_name in names:
        name = _safe_member(raw_name)
        info = archive.getinfo(raw_name)
        if info.is_dir():
            raise ValueError(f"directory members are not allowed: {name}")
        destination = (target_root / name).resolve()
        if target_root != destination and target_root not in destination.parents:
            raise ValueError(f"bundle member escapes root: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(raw_name))
        copied.append(name)
    return copied


def verify_release_bundle(
    archive_path: str | Path,
    bundle_signature_path: str | Path,
    trusted_public_key_path: str | Path,
    *,
    expected_source_commit: str | None = None,
    expected_release_id: str | None = None,
    deployed_manifest_path: str | Path | None = None,
    deployed_release_signature_path: str | Path | None = None,
) -> dict[str, Any]:
    archive_file = Path(archive_path)
    issues: list[str] = []
    try:
        detached = verify_bundle_signature(archive_file, bundle_signature_path, trusted_public_key_path)
        if not detached["ok"]:
            return {
                "ok": False,
                "issues": [f"bundle_signature:{detached['code']}"],
                "archive": str(archive_file),
                "bundle_signature": detached,
            }

        trusted_fingerprint = public_key_fingerprint(load_public_key(trusted_public_key_path))
        with tempfile.TemporaryDirectory(prefix="forex-release-") as temp_dir:
            temp_root = Path(temp_dir).resolve()
            with zipfile.ZipFile(archive_file, "r") as archive:
                names = _copy_archive_to_temp(archive, temp_root)

            required = {BUNDLE_MANIFEST, BUNDLE_RELEASE_SIGNATURE, BUNDLE_PUBLIC_KEY, BUNDLE_INFO}
            missing_metadata = sorted(required - set(names))
            issues.extend(f"missing:{name}" for name in missing_metadata)
            if missing_metadata:
                return {
                    "ok": False,
                    "issues": issues,
                    "archive": str(archive_file),
                    "bundle_signature": detached,
                }

            manifest_path = temp_root / BUNDLE_MANIFEST
            release_signature_path = temp_root / BUNDLE_RELEASE_SIGNATURE
            embedded_public_key_path = temp_root / BUNDLE_PUBLIC_KEY
            info_path = temp_root / BUNDLE_INFO

            manifest = _manifest_document(manifest_path)
            deployment_members = _manifest_members(manifest)
            expected_members = set(deployment_members) | required
            actual_members = set(names)
            for name in sorted(expected_members - actual_members):
                issues.append(f"missing:{name}")
            for name in sorted(actual_members - expected_members):
                issues.append(f"unexpected:{name}")

            info = json.loads(info_path.read_text(encoding="utf-8"))
            if not isinstance(info, dict):
                issues.append("bundle_info:invalid")
                info = {}
            if info.get("version") != BUNDLE_VERSION or info.get("format") != BUNDLE_FORMAT:
                issues.append("bundle_info:format")
            if str(info.get("manifest_sha256", "")) != sha256_file(manifest_path):
                issues.append("bundle_info:manifest_sha256")
            if str(info.get("release_signature_sha256", "")) != sha256_file(release_signature_path):
                issues.append("bundle_info:release_signature_sha256")
            if str(info.get("public_key_fingerprint", "")) != trusted_fingerprint:
                issues.append("bundle_info:public_key_fingerprint")
            if int(info.get("deployment_entries", -1)) != len(deployment_members):
                issues.append("bundle_info:deployment_entries")

            source_commit = _validate_source_commit(str(info.get("source_commit", "")))
            release_id = _validate_release_id(str(info.get("release_id", "")))
            if expected_source_commit is not None and source_commit != _validate_source_commit(expected_source_commit):
                issues.append("anti_rollback:source_commit")
            if expected_release_id is not None and release_id != _validate_release_id(expected_release_id):
                issues.append("anti_rollback:release_id")

            embedded_fingerprint = public_key_fingerprint(load_public_key(embedded_public_key_path))
            if embedded_fingerprint != trusted_fingerprint:
                issues.append("embedded_public_key:fingerprint")

            signed_release = verify_release(temp_root, manifest_path, release_signature_path, trusted_public_key_path)
            if not signed_release["ok"]:
                signature = signed_release.get("signature") or {}
                code = signature.get("code")
                if code and not signature.get("ok", False):
                    issues.append(f"release_signature:{code}")
                deployment = signed_release.get("deployment") or {}
                for issue in deployment.get("issues", []):
                    issues.append(f"deployment:{issue}")

            if deployed_manifest_path is not None:
                deployed_manifest = Path(deployed_manifest_path)
                if not deployed_manifest.exists() or sha256_file(deployed_manifest) != sha256_file(manifest_path):
                    issues.append("deployed_binding:manifest")
            if deployed_release_signature_path is not None:
                deployed_signature = Path(deployed_release_signature_path)
                if not deployed_signature.exists() or sha256_file(deployed_signature) != sha256_file(release_signature_path):
                    issues.append("deployed_binding:release_signature")

            return {
                "ok": not issues,
                "issues": issues,
                "archive": str(archive_file),
                "archive_sha256": sha256_file(archive_file),
                "source_commit": source_commit,
                "release_id": release_id,
                "public_key_fingerprint": trusted_fingerprint,
                "checked": len(deployment_members),
                "bundle_signature": detached,
            }
    except Exception as exc:
        return {
            "ok": False,
            "issues": [f"bundle:{type(exc).__name__}:{exc}"],
            "archive": str(archive_file),
        }


def extract_bundle_preview(
    archive_path: str | Path,
    bundle_signature_path: str | Path,
    target_root: str | Path,
    trusted_public_key_path: str | Path,
    *,
    expected_source_commit: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    verified = verify_release_bundle(
        archive_path,
        bundle_signature_path,
        trusted_public_key_path,
        expected_source_commit=expected_source_commit,
        expected_release_id=expected_release_id,
    )
    if not verified["ok"]:
        raise RuntimeError(f"release bundle verification failed: {verified['issues']}")

    target = Path(target_root).resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"preview target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        for raw_name in archive.namelist():
            name = _safe_member(raw_name)
            destination = (target / name).resolve()
            if target != destination and target not in destination.parents:
                raise ValueError(f"bundle member escapes target: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix(destination.suffix + ".extract-tmp")
            temp.write_bytes(archive.read(raw_name))
            temp.replace(destination)
            extracted.append(name)
    return {"ok": True, "target": str(target), "extracted": sorted(extracted), "verified": verified}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 12 deterministic signed release bundle tooling")
    parser.add_argument("--mode", choices=["build", "sign", "verify", "extract-preview"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default=BUNDLE_MANIFEST)
    parser.add_argument("--release-signature", default=BUNDLE_RELEASE_SIGNATURE)
    parser.add_argument("--public-key", default=BUNDLE_PUBLIC_KEY)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--archive", default=DEFAULT_BUNDLE)
    parser.add_argument("--bundle-signature", default=DEFAULT_BUNDLE_SIGNATURE)
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--expected-commit", default=None)
    parser.add_argument("--expected-release-id", default=None)
    parser.add_argument("--deployed-manifest", default=None)
    parser.add_argument("--deployed-release-signature", default=None)
    parser.add_argument("--target", default="runtime/release-preview")
    args = parser.parse_args()

    if args.mode == "build":
        if not args.source_commit or not args.release_id:
            raise ValueError("build requires --source-commit and --release-id")
        result = build_release_bundle(
            args.root,
            args.manifest,
            args.release_signature,
            args.public_key,
            args.archive,
            source_commit=args.source_commit,
            release_id=args.release_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.mode == "sign":
        if not args.private_key:
            raise ValueError("sign requires --private-key")
        result = sign_release_bundle(args.archive, args.private_key, args.bundle_signature)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.mode == "verify":
        result = verify_release_bundle(
            args.archive,
            args.bundle_signature,
            args.public_key,
            expected_source_commit=args.expected_commit,
            expected_release_id=args.expected_release_id,
            deployed_manifest_path=args.deployed_manifest,
            deployed_release_signature_path=args.deployed_release_signature,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0 if result["ok"] else 3)

    result = extract_bundle_preview(
        args.archive,
        args.bundle_signature,
        args.target,
        args.public_key,
        expected_source_commit=args.expected_commit,
        expected_release_id=args.expected_release_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
