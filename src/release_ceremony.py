from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import string
import tempfile
from typing import Any

from cryptography.exceptions import InvalidSignature

from .artifact import DEFAULT_BUNDLE, DEFAULT_BUNDLE_SIGNATURE, build_release_bundle, sign_release_bundle, verify_release_bundle
from .calendar_provenance import DEFAULT_CALENDAR_PATH, DEFAULT_MIN_COVERAGE_DAYS, bind_calendar_to_manifest, freshness_report, verify_calendar_provenance
from .recovery import create_deployment_manifest, sha256_file, verify_deployment_manifest
from .release import DEFAULT_MANIFEST, DEFAULT_PUBLIC_KEY, DEFAULT_SIGNATURE, PRIVATE_KEY_ENV, load_private_key, load_public_key, public_key_fingerprint, sign_manifest, verify_release
from .runtime_provenance import DEFAULT_CONFIG, DEFAULT_PRODUCTION, DEFAULT_PRODUCTION_FALLBACK, DEFAULT_RECONCILE, DEFAULT_WATCHDOG, DEFAULT_WATCHDOG_FALLBACK, bind_runtime_to_manifest, build_runtime_binding, verify_runtime_provenance

RECEIPT_FORMAT = "forex-auto-trader-release-ceremony-receipt"
RECEIPT_VERSION = 1
DEFAULT_RECEIPT = "release/release_receipt.json"
DEFAULT_RECEIPT_SIGNATURE = "release/release_receipt.signature.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: str | Path) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"release ceremony path must be project-relative: {value!r}")
    return path.as_posix()


def _under(root: Path, value: str | Path) -> Path:
    relative = _safe(value)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"release ceremony path escapes project root: {value!r}")
    return target


def _calendar(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path == root.resolve() or root.resolve() in path.parents:
        raise ValueError("release private key must remain outside the project root")
    return path


def _commit(value: str) -> str:
    text = str(value).strip()
    if not 7 <= len(text) <= 64 or any(char not in string.hexdigits for char in text):
        raise ValueError("source_commit must be a 7-64 character hexadecimal commit identifier")
    return text.lower()


def _release_id(value: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 128 or any(ord(char) < 32 for char in text):
        raise ValueError("release_id must be a non-empty printable value up to 128 characters")
    return text


def _runtime_kwargs(config_path: str, production_path: str, production_fallback: str, reconcile_path: str, watchdog_path: str, watchdog_fallback: str) -> dict[str, str]:
    return {
        "config_path": config_path,
        "production_path": production_path,
        "production_fallback": production_fallback,
        "reconcile_path": reconcile_path,
        "watchdog_path": watchdog_path,
        "watchdog_fallback": watchdog_fallback,
    }


def _fingerprint_pair(root: Path, private_key_path: str | Path | None, public_key_path: str | Path) -> tuple[Path, str]:
    public = _under(root, public_key_path)
    public_fp = public_key_fingerprint(load_public_key(public))
    if private_key_path is None:
        return public, public_fp
    private = _private_key(root, private_key_path)
    private_fp = public_key_fingerprint(load_private_key(private).public_key())
    if private_fp != public_fp:
        raise ValueError("release private key does not match the trusted public key")
    return public, public_fp


def _outputs(root: Path, manifest: str, release_signature: str, archive: str, bundle_signature: str, receipt: str, receipt_signature: str) -> dict[str, Path]:
    values = {
        "manifest": manifest,
        "release_signature": release_signature,
        "bundle": archive,
        "bundle_signature": bundle_signature,
        "receipt": receipt,
        "receipt_signature": receipt_signature,
    }
    normalized = {key: _safe(value) for key, value in values.items()}
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("release ceremony outputs must use distinct paths")
    return {key: _under(root, value) for key, value in normalized.items()}


def _descriptor(source: Path, final_path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": final_path.resolve().relative_to(root.resolve()).as_posix(),
        "size": source.stat().st_size,
        "sha256": sha256_file(source),
    }


def _publish(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".publish-tmp")
    if temp.exists():
        temp.unlink()
    shutil.copyfile(source, temp)
    temp.replace(target)


def sign_receipt(receipt_path: str | Path, private_key_path: str | Path, signature_path: str | Path) -> dict[str, Any]:
    payload = Path(receipt_path).read_bytes()
    private = load_private_key(private_key_path)
    signature = private.sign(payload)
    document = {
        "version": 1,
        "algorithm": "Ed25519",
        "document": RECEIPT_FORMAT,
        "receipt_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key_fingerprint": public_key_fingerprint(private.public_key()),
        "signed_at": _now(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    Path(signature_path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def verify_receipt_signature(receipt_path: str | Path, signature_path: str | Path, public_key_path: str | Path) -> dict[str, Any]:
    try:
        payload = Path(receipt_path).read_bytes()
        document = json.loads(Path(signature_path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("algorithm") != "Ed25519" or document.get("document") != RECEIPT_FORMAT:
            raise ValueError("unsupported release receipt signature format")
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
        return {"ok": True, "code": "SIGNATURE_VALID", "receipt_sha256": expected, "public_key_fingerprint": fingerprint, "signed_at": document.get("signed_at")}
    except Exception as exc:
        return {"ok": False, "code": f"RECEIPT_SIGNATURE_INVALID:{type(exc).__name__}:{exc}"}


def release_preflight(root: str | Path, *, source_commit: str, release_id: str, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, private_key_path: str | Path | None = None, calendar_path: str | Path = DEFAULT_CALENDAR_PATH, require_calendar: bool = False, min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS, config_path: str = DEFAULT_CONFIG, production_path: str = DEFAULT_PRODUCTION, production_fallback: str = DEFAULT_PRODUCTION_FALLBACK, reconcile_path: str = DEFAULT_RECONCILE, watchdog_path: str = DEFAULT_WATCHDOG, watchdog_fallback: str = DEFAULT_WATCHDOG_FALLBACK) -> dict[str, Any]:
    root = Path(root).resolve()
    commit = _commit(source_commit)
    rid = _release_id(release_id)
    _, fingerprint = _fingerprint_pair(root, private_key_path, public_key_path)
    runtime_kwargs = _runtime_kwargs(config_path, production_path, production_fallback, reconcile_path, watchdog_path, watchdog_fallback)
    runtime_binding = build_runtime_binding(root, **runtime_kwargs)
    calendar = _calendar(root, calendar_path)
    if require_calendar and not calendar.is_file():
        raise FileNotFoundError(f"required market calendar is missing: {calendar}")
    if calendar.is_file():
        fresh = freshness_report(calendar, min_coverage_days=min_coverage_days)
        if not fresh["ok"]:
            raise ValueError(f"market calendar freshness preflight failed: {fresh['issues']}")

    with tempfile.TemporaryDirectory(prefix="forex-release-preflight-") as temp:
        manifest = Path(temp) / "manifest.json"
        created = create_deployment_manifest(root, manifest)
        if calendar.is_file():
            bind_calendar_to_manifest(calendar, manifest)
        bind_runtime_to_manifest(root, manifest, **runtime_kwargs)
        deployment = verify_deployment_manifest(root, manifest)
        calendar_check = verify_calendar_provenance(calendar, manifest, min_coverage_days=min_coverage_days, required=require_calendar)
        runtime_check = verify_runtime_provenance(root, manifest, required=True, **runtime_kwargs)
        if not deployment["ok"]:
            raise RuntimeError(f"deployment manifest preflight failed: {deployment['issues']}")
        if not calendar_check["ok"]:
            raise RuntimeError(f"calendar provenance preflight failed: {calendar_check['issues']}")
        if not runtime_check["ok"]:
            raise RuntimeError(f"runtime provenance preflight failed: {runtime_check['issues']}")
    return {
        "ok": True,
        "code": "RELEASE_PREFLIGHT_VALID",
        "source_commit": commit,
        "release_id": rid,
        "public_key_fingerprint": fingerprint,
        "deployment_entries": len(created["entries"]),
        "runtime": runtime_binding["portfolio"],
        "calendar": calendar_check,
    }


def run_release_ceremony(root: str | Path, *, source_commit: str, release_id: str, private_key_path: str | Path, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, manifest_path: str = DEFAULT_MANIFEST, release_signature_path: str = DEFAULT_SIGNATURE, archive_path: str = DEFAULT_BUNDLE, bundle_signature_path: str = DEFAULT_BUNDLE_SIGNATURE, receipt_path: str = DEFAULT_RECEIPT, receipt_signature_path: str = DEFAULT_RECEIPT_SIGNATURE, calendar_path: str | Path = DEFAULT_CALENDAR_PATH, require_calendar: bool = False, min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS, config_path: str = DEFAULT_CONFIG, production_path: str = DEFAULT_PRODUCTION, production_fallback: str = DEFAULT_PRODUCTION_FALLBACK, reconcile_path: str = DEFAULT_RECONCILE, watchdog_path: str = DEFAULT_WATCHDOG, watchdog_fallback: str = DEFAULT_WATCHDOG_FALLBACK, overwrite: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    commit = _commit(source_commit)
    rid = _release_id(release_id)
    outputs = _outputs(root, manifest_path, release_signature_path, archive_path, bundle_signature_path, receipt_path, receipt_signature_path)
    existing = sorted(name for name, path in outputs.items() if path.exists())
    if existing and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing release artifacts: {', '.join(existing)}")
    private = _private_key(root, private_key_path)
    public, fingerprint = _fingerprint_pair(root, private, public_key_path)
    preflight = release_preflight(root, source_commit=commit, release_id=rid, public_key_path=public_key_path, private_key_path=private, calendar_path=calendar_path, require_calendar=require_calendar, min_coverage_days=min_coverage_days, config_path=config_path, production_path=production_path, production_fallback=production_fallback, reconcile_path=reconcile_path, watchdog_path=watchdog_path, watchdog_fallback=watchdog_fallback)
    runtime_kwargs = _runtime_kwargs(config_path, production_path, production_fallback, reconcile_path, watchdog_path, watchdog_fallback)
    calendar = _calendar(root, calendar_path)

    with tempfile.TemporaryDirectory(prefix="forex-release-ceremony-") as temp:
        stage = Path(temp)
        staged = {
            "manifest": stage / "manifest.json",
            "release_signature": stage / "release-signature.json",
            "bundle": stage / "bundle.zip",
            "bundle_signature": stage / "bundle-signature.json",
            "receipt": stage / "receipt.json",
            "receipt_signature": stage / "receipt-signature.json",
        }
        manifest = create_deployment_manifest(root, staged["manifest"])
        if calendar.is_file():
            bind_calendar_to_manifest(calendar, staged["manifest"])
        runtime_binding = bind_runtime_to_manifest(root, staged["manifest"], **runtime_kwargs)
        deployment = verify_deployment_manifest(root, staged["manifest"])
        calendar_check = verify_calendar_provenance(calendar, staged["manifest"], min_coverage_days=min_coverage_days, required=require_calendar)
        runtime_check = verify_runtime_provenance(root, staged["manifest"], required=True, **runtime_kwargs)
        if not deployment["ok"] or not calendar_check["ok"] or not runtime_check["ok"]:
            raise RuntimeError("release ceremony provenance verification failed before signing")

        release_sig = sign_manifest(staged["manifest"], private, staged["release_signature"])
        release_check = verify_release(root, staged["manifest"], staged["release_signature"], public)
        if not release_check["ok"]:
            raise RuntimeError(f"signed release verification failed: {release_check}")
        bundle = build_release_bundle(root, staged["manifest"], staged["release_signature"], public, staged["bundle"], source_commit=commit, release_id=rid)
        bundle_sig = sign_release_bundle(staged["bundle"], private, staged["bundle_signature"])
        bundle_check = verify_release_bundle(staged["bundle"], staged["bundle_signature"], public, expected_source_commit=commit, expected_release_id=rid, deployed_manifest_path=staged["manifest"], deployed_release_signature_path=staged["release_signature"])
        if not bundle_check["ok"]:
            raise RuntimeError(f"signed bundle verification failed: {bundle_check['issues']}")

        receipt = {
            "version": RECEIPT_VERSION,
            "format": RECEIPT_FORMAT,
            "source_commit": commit,
            "release_id": rid,
            "completed_at": _now(),
            "public_key_fingerprint": fingerprint,
            "deployment_entries": len(manifest["entries"]),
            "artifacts": {name: _descriptor(staged[name], outputs[name], root) for name in ("manifest", "release_signature", "bundle", "bundle_signature")},
            "runtime": runtime_binding["portfolio"],
            "calendar": {
                "configured": calendar.is_file(),
                "required": bool(require_calendar),
                "path": str(calendar_path),
                "sha256": calendar_check.get("sha256"),
                "valid_through": calendar_check.get("valid_through"),
                "min_coverage_days": int(min_coverage_days),
            },
            "verification": {
                "deployment": bool(deployment["ok"]),
                "release_signature": bool(release_check["ok"]),
                "calendar_provenance": bool(calendar_check["ok"]),
                "runtime_provenance": bool(runtime_check["ok"]),
                "bundle": bool(bundle_check["ok"]),
            },
            "signing": {"release_signed_at": release_sig.get("signed_at"), "bundle_signed_at": bundle_sig.get("signed_at")},
        }
        staged["receipt"].write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sign_receipt(staged["receipt"], private, staged["receipt_signature"])
        receipt_check = verify_receipt_signature(staged["receipt"], staged["receipt_signature"], public)
        if not receipt_check["ok"]:
            raise RuntimeError(f"release receipt signature verification failed: {receipt_check}")
        for name in ("manifest", "release_signature", "bundle", "bundle_signature", "receipt", "receipt_signature"):
            _publish(staged[name], outputs[name])

    final = verify_release_receipt(root, receipt_path=receipt_path, receipt_signature_path=receipt_signature_path, public_key_path=public_key_path, calendar_path=calendar_path, expected_source_commit=commit, expected_release_id=rid, config_path=config_path, production_path=production_path, production_fallback=production_fallback, reconcile_path=reconcile_path, watchdog_path=watchdog_path, watchdog_fallback=watchdog_fallback)
    if not final["ok"]:
        raise RuntimeError(f"published release receipt verification failed: {final['issues']}")
    return {"ok": True, "code": "RELEASE_CEREMONY_COMPLETE", "source_commit": commit, "release_id": rid, "receipt": _safe(receipt_path), "receipt_signature": _safe(receipt_signature_path), "public_key_fingerprint": preflight["public_key_fingerprint"], "final_verification": final}


def verify_release_receipt(root: str | Path, *, receipt_path: str | Path = DEFAULT_RECEIPT, receipt_signature_path: str | Path = DEFAULT_RECEIPT_SIGNATURE, public_key_path: str | Path = DEFAULT_PUBLIC_KEY, calendar_path: str | Path = DEFAULT_CALENDAR_PATH, expected_source_commit: str | None = None, expected_release_id: str | None = None, config_path: str = DEFAULT_CONFIG, production_path: str = DEFAULT_PRODUCTION, production_fallback: str = DEFAULT_PRODUCTION_FALLBACK, reconcile_path: str = DEFAULT_RECONCILE, watchdog_path: str = DEFAULT_WATCHDOG, watchdog_fallback: str = DEFAULT_WATCHDOG_FALLBACK) -> dict[str, Any]:
    root = Path(root).resolve()
    receipt = _under(root, receipt_path)
    signature = _under(root, receipt_signature_path)
    public = _under(root, public_key_path)
    sig = verify_receipt_signature(receipt, signature, public)
    if not sig["ok"]:
        return {"ok": False, "code": "RELEASE_RECEIPT_INVALID", "issues": [f"receipt_signature:{sig['code']}"], "signature": sig}
    document = json.loads(receipt.read_text(encoding="utf-8"))
    issues: list[str] = []
    if not isinstance(document, dict) or document.get("version") != RECEIPT_VERSION or document.get("format") != RECEIPT_FORMAT:
        issues.append("receipt:format")
    commit = _commit(str(document.get("source_commit", "")))
    rid = _release_id(str(document.get("release_id", "")))
    if expected_source_commit is not None and commit != _commit(expected_source_commit):
        issues.append("anti_rollback:source_commit")
    if expected_release_id is not None and rid != _release_id(expected_release_id):
        issues.append("anti_rollback:release_id")
    fingerprint = public_key_fingerprint(load_public_key(public))
    if document.get("public_key_fingerprint") != fingerprint:
        issues.append("receipt:public_key_fingerprint")

    paths: dict[str, Path] = {}
    artifacts = document.get("artifacts") if isinstance(document.get("artifacts"), dict) else {}
    for role in ("manifest", "release_signature", "bundle", "bundle_signature"):
        item = artifacts.get(role)
        if not isinstance(item, dict):
            issues.append(f"artifact:{role}:binding")
            continue
        try:
            path = _under(root, str(item.get("path", "")))
            paths[role] = path
            if not path.is_file():
                issues.append(f"artifact:{role}:missing")
                continue
            if path.stat().st_size != int(item.get("size", -1)):
                issues.append(f"artifact:{role}:size")
            if sha256_file(path) != str(item.get("sha256", "")):
                issues.append(f"artifact:{role}:sha256")
        except Exception as exc:
            issues.append(f"artifact:{role}:{type(exc).__name__}:{exc}")

    runtime_kwargs = _runtime_kwargs(config_path, production_path, production_fallback, reconcile_path, watchdog_path, watchdog_fallback)
    release_check = runtime_check = calendar_check = bundle_check = None
    if {"manifest", "release_signature"} <= set(paths):
        release_check = verify_release(root, paths["manifest"], paths["release_signature"], public)
        if not release_check["ok"]:
            issues.append("release:verification")
        runtime_check = verify_runtime_provenance(root, paths["manifest"], required=True, **runtime_kwargs)
        if not runtime_check["ok"]:
            issues.extend(f"runtime:{issue}" for issue in runtime_check["issues"])
        cal_doc = document.get("calendar") if isinstance(document.get("calendar"), dict) else {}
        calendar_check = verify_calendar_provenance(_calendar(root, calendar_path), paths["manifest"], min_coverage_days=int(cal_doc.get("min_coverage_days", DEFAULT_MIN_COVERAGE_DAYS)), required=bool(cal_doc.get("required", False)))
        if not calendar_check["ok"]:
            issues.extend(f"calendar:{issue}" for issue in calendar_check["issues"])
        for field in ("sha256", "valid_through"):
            if cal_doc.get(field) != calendar_check.get(field):
                issues.append(f"calendar:{field}")
        runtime_doc = document.get("runtime") if isinstance(document.get("runtime"), dict) else {}
        if runtime_check.get("ok") and runtime_doc.get("design_fingerprint") != runtime_check.get("design_fingerprint"):
            issues.append("runtime:design_fingerprint")
    if {"manifest", "release_signature", "bundle", "bundle_signature"} <= set(paths):
        bundle_check = verify_release_bundle(paths["bundle"], paths["bundle_signature"], public, expected_source_commit=commit, expected_release_id=rid, deployed_manifest_path=paths["manifest"], deployed_release_signature_path=paths["release_signature"])
        if not bundle_check["ok"]:
            issues.extend(f"bundle:{issue}" for issue in bundle_check["issues"])

    return {"ok": not issues, "code": "RELEASE_RECEIPT_VALID" if not issues else "RELEASE_RECEIPT_INVALID", "issues": issues, "source_commit": commit, "release_id": rid, "public_key_fingerprint": fingerprint, "signature": sig, "release": release_check, "runtime": runtime_check, "calendar": calendar_check, "bundle": bundle_check}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 27 deterministic release ceremony and signed receipt")
    parser.add_argument("--mode", choices=["preflight", "run", "verify-receipt"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--source-commit")
    parser.add_argument("--release-id")
    parser.add_argument("--private-key")
    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--release-signature", default=DEFAULT_SIGNATURE)
    parser.add_argument("--archive", default=DEFAULT_BUNDLE)
    parser.add_argument("--bundle-signature", default=DEFAULT_BUNDLE_SIGNATURE)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--receipt-signature", default=DEFAULT_RECEIPT_SIGNATURE)
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--require-calendar", action="store_true")
    parser.add_argument("--min-calendar-coverage-days", type=int, default=DEFAULT_MIN_COVERAGE_DAYS)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--production-config", default=DEFAULT_PRODUCTION)
    parser.add_argument("--production-fallback", default=DEFAULT_PRODUCTION_FALLBACK)
    parser.add_argument("--reconcile-config", default=DEFAULT_RECONCILE)
    parser.add_argument("--watchdog-config", default=DEFAULT_WATCHDOG)
    parser.add_argument("--watchdog-fallback", default=DEFAULT_WATCHDOG_FALLBACK)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    runtime = {
        "config_path": args.config,
        "production_path": args.production_config,
        "production_fallback": args.production_fallback,
        "reconcile_path": args.reconcile_config,
        "watchdog_path": args.watchdog_config,
        "watchdog_fallback": args.watchdog_fallback,
    }
    if args.mode == "verify-receipt":
        result = verify_release_receipt(args.root, receipt_path=args.receipt, receipt_signature_path=args.receipt_signature, public_key_path=args.public_key, calendar_path=args.calendar, expected_source_commit=args.source_commit, expected_release_id=args.release_id, **runtime)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        raise SystemExit(0 if result["ok"] else 3)
    if not args.source_commit or not args.release_id:
        raise ValueError("preflight/run require --source-commit and --release-id")
    private = args.private_key or os.getenv(PRIVATE_KEY_ENV, "").strip() or None
    if args.mode == "preflight":
        result = release_preflight(args.root, source_commit=args.source_commit, release_id=args.release_id, public_key_path=args.public_key, private_key_path=private, calendar_path=args.calendar, require_calendar=args.require_calendar, min_coverage_days=args.min_calendar_coverage_days, **runtime)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    if not private:
        raise ValueError(f"run requires --private-key or {PRIVATE_KEY_ENV}")
    result = run_release_ceremony(args.root, source_commit=args.source_commit, release_id=args.release_id, private_key_path=private, public_key_path=args.public_key, manifest_path=args.manifest, release_signature_path=args.release_signature, archive_path=args.archive, bundle_signature_path=args.bundle_signature, receipt_path=args.receipt, receipt_signature_path=args.receipt_signature, calendar_path=args.calendar, require_calendar=args.require_calendar, min_coverage_days=args.min_calendar_coverage_days, overwrite=args.overwrite, **runtime)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
