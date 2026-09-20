from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import zipfile

import yaml

from .production import atomic_write_json
from .recovery import BACKUP_MANIFEST, create_backup, restore_backup, sha256_file, verify_backup


PRUNE_ACK = "I_UNDERSTAND_BACKUP_PRUNE"
CATALOG_VERSION = 1


@dataclass(frozen=True)
class BackupPolicyConfig:
    local_dir: str = "backups/runtime"
    catalog_path: str = "runtime/backup_catalog.json"
    drill_report_path: str = "runtime/backup_restore_drill.json"
    replica_root_env: str = "FOREX_BACKUP_REPLICA_ROOT"
    replica_subdir: str = "Forex-trade/runtime-backups"
    keep_latest: int = 7
    keep_daily: int = 14
    keep_weekly: int = 8
    keep_monthly: int = 12
    require_verified_replica_before_prune: bool = True


def load_backup_policy(path: str | Path = "backup.yaml") -> BackupPolicyConfig:
    target = Path(path)
    if not target.exists():
        fallback = Path("backup.example.yaml")
        if not fallback.exists():
            return BackupPolicyConfig()
        target = fallback
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("backup policy config must be a mapping")
    cfg = BackupPolicyConfig(**raw)
    for name in ("keep_latest", "keep_daily", "keep_weekly", "keep_monthly"):
        if getattr(cfg, name) < 0:
            raise ValueError(f"{name} cannot be negative")
    if not cfg.local_dir or not cfg.catalog_path or not cfg.drill_report_path:
        raise ValueError("backup paths cannot be empty")
    if not cfg.replica_root_env or not cfg.replica_subdir:
        raise ValueError("replica settings cannot be empty")
    return cfg


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_backup_manifest(archive_path: str | Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        raw = json.loads(archive.read(BACKUP_MANIFEST).decode("utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ValueError("backup manifest is invalid")
    return raw


def load_catalog(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"version": CATALOG_VERSION, "updated_at": None, "records": []}
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != CATALOG_VERSION or not isinstance(raw.get("records"), list):
        raise ValueError("backup catalog is invalid")
    return raw


def save_catalog(path: str | Path, catalog: dict[str, Any]) -> None:
    payload = dict(catalog)
    payload["version"] = CATALOG_VERSION
    payload["updated_at"] = _now().isoformat()
    atomic_write_json(path, payload)


def _upsert_record(catalog: dict[str, Any], record: dict[str, Any]) -> None:
    records = catalog.setdefault("records", [])
    digest = record["sha256"]
    for index, existing in enumerate(records):
        if existing.get("sha256") == digest:
            merged = dict(existing)
            merged.update(record)
            merged["replicas"] = record.get("replicas", existing.get("replicas", []))
            records[index] = merged
            break
    else:
        records.append(record)


def _unique_backup_path(directory: str | Path, now: datetime | None = None) -> Path:
    current = now or _now()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    base = current.strftime("runtime-%Y%m%d-%H%M%S")
    candidate = root / f"{base}.zip"
    counter = 1
    while candidate.exists():
        candidate = root / f"{base}-{counter}.zip"
        counter += 1
    return candidate


def create_managed_backup(root: str | Path, cfg: BackupPolicyConfig) -> dict[str, Any]:
    archive = _unique_backup_path(cfg.local_dir)
    manifest = create_backup(root, archive)
    verification = verify_backup(archive)
    if not verification["ok"]:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"new backup failed verification: {verification['issues']}")

    digest = sha256_file(archive)
    record = {
        "created_at": str(manifest.get("created_at") or _now().isoformat()),
        "local_path": str(archive),
        "sha256": digest,
        "size": archive.stat().st_size,
        "verified_at": _now().isoformat(),
        "replicas": [],
    }
    catalog = load_catalog(cfg.catalog_path)
    _upsert_record(catalog, record)
    save_catalog(cfg.catalog_path, catalog)
    return record


def _resolved_replica_root(cfg: BackupPolicyConfig, explicit: str | Path | None = None) -> Path:
    value = str(explicit).strip() if explicit is not None else os.getenv(cfg.replica_root_env, "").strip()
    if not value:
        raise ValueError(f"replica root is required via --replica-root or {cfg.replica_root_env}")
    return Path(value).expanduser() / cfg.replica_subdir


def replicate_archive(
    archive_path: str | Path,
    cfg: BackupPolicyConfig,
    *,
    replica_root: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(archive_path)
    local_verify = verify_backup(source)
    if not local_verify["ok"]:
        raise RuntimeError(f"local backup verification failed: {local_verify['issues']}")
    digest = sha256_file(source)

    root = _resolved_replica_root(cfg, replica_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / source.name
    temp = target.with_suffix(target.suffix + ".replica-tmp")
    if temp.exists():
        temp.unlink()
    with source.open("rb") as src, temp.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    temp.replace(target)

    remote_verify = verify_backup(target)
    replica_digest = sha256_file(target)
    if not remote_verify["ok"] or replica_digest != digest:
        raise RuntimeError("replicated backup failed verification or SHA-256 comparison")

    catalog = load_catalog(cfg.catalog_path)
    manifest = _read_backup_manifest(source)
    record = {
        "created_at": str(manifest.get("created_at") or _now().isoformat()),
        "local_path": str(source),
        "sha256": digest,
        "size": source.stat().st_size,
        "verified_at": _now().isoformat(),
        "replicas": [],
    }
    for existing in catalog.get("records", []):
        if existing.get("sha256") == digest:
            record = dict(existing)
            break
    replicas = [rep for rep in record.get("replicas", []) if rep.get("path") != str(target)]
    replica_record = {
        "path": str(target),
        "sha256": replica_digest,
        "size": target.stat().st_size,
        "verified_at": _now().isoformat(),
    }
    replicas.append(replica_record)
    record["replicas"] = replicas
    _upsert_record(catalog, record)
    save_catalog(cfg.catalog_path, catalog)
    return replica_record


def verify_catalog(cfg: BackupPolicyConfig) -> dict[str, Any]:
    catalog = load_catalog(cfg.catalog_path)
    issues: list[str] = []
    checked_local = 0
    checked_replicas = 0
    for record in catalog.get("records", []):
        digest = str(record.get("sha256", ""))
        local = Path(str(record.get("local_path", "")))
        if local.exists():
            checked_local += 1
            verification = verify_backup(local)
            if not verification["ok"]:
                issues.append(f"local_invalid:{local}")
            elif sha256_file(local) != digest:
                issues.append(f"local_sha256:{local}")
        for replica in record.get("replicas", []):
            target = Path(str(replica.get("path", "")))
            if not target.exists():
                issues.append(f"replica_missing:{target}")
                continue
            checked_replicas += 1
            verification = verify_backup(target)
            if not verification["ok"]:
                issues.append(f"replica_invalid:{target}")
            elif sha256_file(target) != digest:
                issues.append(f"replica_sha256:{target}")
    return {
        "ok": not issues,
        "records": len(catalog.get("records", [])),
        "checked_local": checked_local,
        "checked_replicas": checked_replicas,
        "issues": issues,
    }


def retention_plan(records: list[dict[str, Any]], cfg: BackupPolicyConfig) -> dict[str, list[str]]:
    usable: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        try:
            usable.append((_parse_time(str(record["created_at"])), record))
        except Exception:
            continue
    usable.sort(key=lambda item: item[0], reverse=True)

    keep: set[str] = set()
    for _, record in usable[: cfg.keep_latest]:
        keep.add(str(record.get("sha256", "")))

    def keep_buckets(limit: int, key_fn) -> None:
        seen: set[Any] = set()
        for stamp, record in usable:
            key = key_fn(stamp)
            if key in seen:
                continue
            if len(seen) >= limit:
                break
            seen.add(key)
            keep.add(str(record.get("sha256", "")))

    keep_buckets(cfg.keep_daily, lambda stamp: stamp.date().isoformat())
    keep_buckets(cfg.keep_weekly, lambda stamp: stamp.isocalendar()[:2])
    keep_buckets(cfg.keep_monthly, lambda stamp: (stamp.year, stamp.month))

    kept: list[str] = []
    prune: list[str] = []
    for _, record in usable:
        digest = str(record.get("sha256", ""))
        path = str(record.get("local_path", ""))
        (kept if digest in keep else prune).append(path)
    return {"keep": kept, "prune": prune}


def _has_verified_replica(record: dict[str, Any]) -> bool:
    digest = str(record.get("sha256", ""))
    for replica in record.get("replicas", []):
        target = Path(str(replica.get("path", "")))
        try:
            if target.exists() and sha256_file(target) == digest and verify_backup(target)["ok"]:
                return True
        except Exception:
            continue
    return False


def prune_local_backups(
    cfg: BackupPolicyConfig,
    *,
    apply: bool = False,
    ack: str | None = None,
) -> dict[str, Any]:
    if apply and ack != PRUNE_ACK:
        raise RuntimeError(f"destructive retention requires --ack {PRUNE_ACK}")

    catalog = load_catalog(cfg.catalog_path)
    plan = retention_plan(catalog.get("records", []), cfg)
    records_by_path = {str(record.get("local_path", "")): record for record in catalog.get("records", [])}
    eligible: list[str] = []
    skipped: list[dict[str, str]] = []

    for path in plan["prune"]:
        record = records_by_path.get(path)
        if record is None:
            skipped.append({"path": path, "reason": "catalog_record_missing"})
            continue
        local = Path(path)
        if not local.exists():
            skipped.append({"path": path, "reason": "local_missing"})
            continue
        if cfg.require_verified_replica_before_prune and not _has_verified_replica(record):
            skipped.append({"path": path, "reason": "no_verified_replica"})
            continue
        eligible.append(path)

    deleted: list[str] = []
    if apply:
        for path in eligible:
            Path(path).unlink()
            deleted.append(path)
        for record in catalog.get("records", []):
            if str(record.get("local_path", "")) in deleted:
                record["local_deleted_at"] = _now().isoformat()
        save_catalog(cfg.catalog_path, catalog)

    return {
        "ok": True,
        "apply": apply,
        "keep": plan["keep"],
        "eligible": eligible,
        "skipped": skipped,
        "deleted": deleted,
    }


def restore_drill(
    archive_path: str | Path,
    cfg: BackupPolicyConfig,
    *,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    archive = Path(archive_path)
    verification = verify_backup(archive)
    started_at = _now()
    issues: list[str] = []
    restored: list[str] = []

    if not verification["ok"]:
        issues.extend(str(issue) for issue in verification["issues"])
    else:
        manifest = _read_backup_manifest(archive)
        expected = {str(entry["path"]): entry for entry in manifest["entries"]}
        with tempfile.TemporaryDirectory(prefix="forex-restore-drill-") as temp_dir:
            result = restore_backup(archive, temp_dir, overwrite=False)
            restored = list(result["restored"])
            for relative, entry in expected.items():
                target = Path(temp_dir) / relative
                if not target.exists():
                    issues.append(f"missing_after_restore:{relative}")
                    continue
                if target.stat().st_size != int(entry["size"]):
                    issues.append(f"size_after_restore:{relative}")
                if sha256_file(target) != str(entry["sha256"]):
                    issues.append(f"sha256_after_restore:{relative}")

    report = {
        "ok": not issues,
        "archive": str(archive),
        "archive_sha256": sha256_file(archive) if archive.exists() else None,
        "started_at": started_at.isoformat(),
        "completed_at": _now().isoformat(),
        "restored_files": len(restored),
        "issues": issues,
    }
    atomic_write_json(report_path or cfg.drill_report_path, report)
    return report


def _latest_local_record(cfg: BackupPolicyConfig) -> dict[str, Any]:
    records = [record for record in load_catalog(cfg.catalog_path).get("records", []) if Path(str(record.get("local_path", ""))).exists()]
    if not records:
        raise RuntimeError("no managed local backup is available")
    records.sort(key=lambda record: _parse_time(str(record["created_at"])), reverse=True)
    return records[0]


def run_cycle(
    root: str | Path,
    cfg: BackupPolicyConfig,
    *,
    replica_root: str | Path | None = None,
    apply_retention: bool = False,
    ack: str | None = None,
) -> dict[str, Any]:
    created = create_managed_backup(root, cfg)
    replicated = replicate_archive(created["local_path"], cfg, replica_root=replica_root)
    retention = prune_local_backups(cfg, apply=apply_retention, ack=ack)
    return {"ok": True, "created": created, "replicated": replicated, "retention": retention}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 13 backup retention, off-device replication and restore drills")
    parser.add_argument("--mode", choices=["create", "replicate", "verify", "retention-plan", "prune", "restore-drill", "cycle"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="backup.yaml")
    parser.add_argument("--archive", default=None)
    parser.add_argument("--replica-root", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-retention", action="store_true")
    parser.add_argument("--ack", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    cfg = load_backup_policy(args.config)
    if args.mode == "create":
        result = create_managed_backup(args.root, cfg)
    elif args.mode == "replicate":
        archive = args.archive or _latest_local_record(cfg)["local_path"]
        result = replicate_archive(archive, cfg, replica_root=args.replica_root)
    elif args.mode == "verify":
        result = verify_catalog(cfg)
    elif args.mode == "retention-plan":
        result = retention_plan(load_catalog(cfg.catalog_path).get("records", []), cfg)
    elif args.mode == "prune":
        result = prune_local_backups(cfg, apply=args.apply, ack=args.ack)
    elif args.mode == "restore-drill":
        archive = args.archive or _latest_local_record(cfg)["local_path"]
        result = restore_drill(archive, cfg, report_path=args.report)
    else:
        result = run_cycle(
            args.root,
            cfg,
            replica_root=args.replica_root,
            apply_retention=args.apply_retention,
            ack=args.ack,
        )

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if isinstance(result, dict) and result.get("ok") is False:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
