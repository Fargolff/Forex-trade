from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
import zipfile

from .production import atomic_write_json


RESTORE_ACK = "I_UNDERSTAND_RUNTIME_RESTORE"
BACKUP_MANIFEST = "backup_manifest.json"

DEFAULT_BACKUP_PATHS = (
    "config.yaml",
    "production.yaml",
    "watchdog.yaml",
    "runtime/live_state.json",
    "runtime/live_heartbeat.json",
    "runtime/live_incidents.csv",
    "runtime/live_events.csv",
    "runtime/production_halt.json",
    "runtime/execution_metrics.json",
    "runtime/alert_state.json",
    "results/portfolio/portfolio_weights.csv",
    "results/portfolio/portfolio_candidates.csv",
)

DEPLOYMENT_PATTERNS = (
    "src/**/*.py",
    "requirements.txt",
    "config.example.yaml",
    "production.example.yaml",
    "watchdog.example.yaml",
    "deploy/windows/*.ps1",
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def _resolve_under(root: Path, relative: str) -> Path:
    safe = _safe_relative(relative)
    target = (root / safe).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path escapes root: {relative}")
    return target


def create_backup(
    root: str | Path,
    output: str | Path,
    paths: Iterable[str] = DEFAULT_BACKUP_PATHS,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in paths:
            safe = _safe_relative(str(relative))
            source = _resolve_under(root_path, safe)
            if not source.exists() or not source.is_file():
                continue
            payload = source.read_bytes()
            entries.append(
                {
                    "path": safe,
                    "size": len(payload),
                    "sha256": sha256_bytes(payload),
                }
            )
            archive.writestr(safe, payload)

        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "root_label": root_path.name,
            "entries": entries,
            "note": "Secrets stored only in environment variables are intentionally not included.",
        }
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    return manifest


def _load_backup_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = json.loads(archive.read(BACKUP_MANIFEST).decode("utf-8"))
    except KeyError as exc:
        raise ValueError("backup manifest is missing") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ValueError("backup manifest is invalid")
    return raw


def verify_backup(path: str | Path) -> dict[str, Any]:
    archive_path = Path(path)
    issues: list[str] = []
    checked = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = _load_backup_manifest(archive)
        members = set(archive.namelist())
        for entry in manifest["entries"]:
            relative = _safe_relative(str(entry.get("path", "")))
            if relative not in members:
                issues.append(f"missing:{relative}")
                continue
            payload = archive.read(relative)
            checked += 1
            expected_hash = str(entry.get("sha256", ""))
            expected_size = int(entry.get("size", -1))
            if len(payload) != expected_size:
                issues.append(f"size:{relative}")
            if sha256_bytes(payload) != expected_hash:
                issues.append(f"sha256:{relative}")
    return {"ok": not issues, "checked": checked, "issues": issues, "archive": str(archive_path)}


def restore_backup(
    archive_path: str | Path,
    target_root: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    verified = verify_backup(archive_path)
    if not verified["ok"]:
        raise RuntimeError(f"backup verification failed: {verified['issues']}")

    target = Path(target_root).resolve()
    target.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = _load_backup_manifest(archive)
        for entry in manifest["entries"]:
            relative = _safe_relative(str(entry["path"]))
            destination = _resolve_under(target, relative)
            if destination.exists() and not overwrite:
                raise FileExistsError(f"restore target already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = archive.read(relative)
            temp = destination.with_suffix(destination.suffix + ".restore-tmp")
            temp.write_bytes(payload)
            temp.replace(destination)
            restored.append(relative)
    return {"ok": True, "target": str(target), "restored": restored}


def _deployment_files(root: Path, patterns: Iterable[str] = DEPLOYMENT_PATTERNS) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                found[relative] = path
    return [found[key] for key in sorted(found)]


def create_deployment_manifest(
    root: str | Path,
    output: str | Path,
    patterns: Iterable[str] = DEPLOYMENT_PATTERNS,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    files = _deployment_files(root_path, patterns)
    entries = [
        {
            "path": path.relative_to(root_path).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    manifest = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    atomic_write_json(output, manifest)
    return manifest


def verify_deployment_manifest(root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        raise ValueError("deployment manifest is invalid")

    issues: list[str] = []
    checked = 0
    for entry in manifest["entries"]:
        relative = _safe_relative(str(entry.get("path", "")))
        target = _resolve_under(root_path, relative)
        if not target.exists() or not target.is_file():
            issues.append(f"missing:{relative}")
            continue
        checked += 1
        if target.stat().st_size != int(entry.get("size", -1)):
            issues.append(f"size:{relative}")
            continue
        if sha256_file(target) != str(entry.get("sha256", "")):
            issues.append(f"sha256:{relative}")
    return {"ok": not issues, "checked": checked, "issues": issues}


def _default_backup_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"backups/runtime-{stamp}.zip"


def main() -> None:
    parser = argparse.ArgumentParser(description="Forex trader disaster-recovery and deployment-integrity tools")
    parser.add_argument(
        "--mode",
        choices=["backup", "verify-backup", "restore-preview", "restore-in-place", "make-manifest", "verify-manifest"],
        required=True,
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--archive", default=None)
    parser.add_argument("--target", default="runtime/restore-preview")
    parser.add_argument("--manifest", default="runtime/deployment_manifest.json")
    parser.add_argument("--ack", default=None)
    args = parser.parse_args()

    if args.mode == "backup":
        archive = args.archive or _default_backup_name()
        result = create_backup(args.root, archive)
        print(json.dumps({"archive": archive, **result}, indent=2, sort_keys=True))
        return
    if args.mode == "verify-backup":
        if not args.archive:
            raise ValueError("--archive is required")
        result = verify_backup(args.archive)
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0 if result["ok"] else 3)
    if args.mode == "restore-preview":
        if not args.archive:
            raise ValueError("--archive is required")
        result = restore_backup(args.archive, args.target, overwrite=False)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "restore-in-place":
        if not args.archive:
            raise ValueError("--archive is required")
        if args.ack != RESTORE_ACK:
            raise RuntimeError(f"in-place restore requires --ack {RESTORE_ACK}")
        result = restore_backup(args.archive, args.root, overwrite=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "make-manifest":
        result = create_deployment_manifest(args.root, args.manifest)
        print(json.dumps({"manifest": args.manifest, "entries": len(result["entries"])}, indent=2, sort_keys=True))
        return
    if args.mode == "verify-manifest":
        result = verify_deployment_manifest(args.root, args.manifest)
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
