from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backup_policy import (
    PRUNE_ACK,
    BackupPolicyConfig,
    create_managed_backup,
    load_catalog,
    prune_local_backups,
    replicate_archive,
    restore_drill,
    retention_plan,
    verify_catalog,
)


def _cfg(tmp_path: Path, **overrides):
    values = {
        "local_dir": str(tmp_path / "backups"),
        "catalog_path": str(tmp_path / "runtime" / "backup_catalog.json"),
        "drill_report_path": str(tmp_path / "runtime" / "restore_drill.json"),
        "replica_root_env": "FOREX_TEST_REPLICA_ROOT",
        "replica_subdir": "replicas",
        "keep_latest": 7,
        "keep_daily": 14,
        "keep_weekly": 8,
        "keep_monthly": 12,
        "require_verified_replica_before_prune": True,
    }
    values.update(overrides)
    return BackupPolicyConfig(**values)


def _runtime_fixture(tmp_path: Path):
    root = tmp_path / "project"
    runtime = root / "runtime"
    results = root / "results" / "portfolio"
    runtime.mkdir(parents=True)
    results.mkdir(parents=True)
    (root / "config.yaml").write_text("live:\n  enabled: false\n", encoding="utf-8")
    (runtime / "live_state.json").write_text('{"halted": false}\n', encoding="utf-8")
    (results / "portfolio_weights.csv").write_text("strategy,weight\nema,1.0\n", encoding="utf-8")
    return root


def test_managed_backup_verifies_and_catalogs(tmp_path):
    root = _runtime_fixture(tmp_path)
    cfg = _cfg(tmp_path)

    record = create_managed_backup(root, cfg)
    catalog = load_catalog(cfg.catalog_path)
    verified = verify_catalog(cfg)

    assert Path(record["local_path"]).exists()
    assert len(catalog["records"]) == 1
    assert catalog["records"][0]["sha256"] == record["sha256"]
    assert verified["ok"] is True
    assert verified["checked_local"] == 1


def test_replication_is_byte_identical_and_cataloged(tmp_path):
    root = _runtime_fixture(tmp_path)
    cfg = _cfg(tmp_path)
    record = create_managed_backup(root, cfg)
    replica_root = tmp_path / "off-device"

    replica = replicate_archive(record["local_path"], cfg, replica_root=replica_root)
    catalog = load_catalog(cfg.catalog_path)

    assert Path(replica["path"]).exists()
    assert replica["sha256"] == record["sha256"]
    assert catalog["records"][0]["replicas"][0]["sha256"] == record["sha256"]
    assert verify_catalog(cfg)["ok"] is True


def test_retention_plan_keeps_latest_daily_weekly_monthly_buckets(tmp_path):
    cfg = _cfg(tmp_path, keep_latest=1, keep_daily=2, keep_weekly=2, keep_monthly=2)
    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    records = []
    for index, days_ago in enumerate([0, 1, 2, 8, 16, 40, 80]):
        stamp = now - timedelta(days=days_ago)
        records.append(
            {
                "created_at": stamp.isoformat(),
                "local_path": f"backup-{index}.zip",
                "sha256": f"sha-{index}",
                "replicas": [],
            }
        )

    plan = retention_plan(records, cfg)

    assert "backup-0.zip" in plan["keep"]
    assert "backup-1.zip" in plan["keep"]
    assert len(plan["prune"]) >= 1
    assert set(plan["keep"]).isdisjoint(plan["prune"])


def test_prune_refuses_without_verified_replica_then_deletes_after_replication(tmp_path):
    root = _runtime_fixture(tmp_path)
    cfg = _cfg(tmp_path, keep_latest=0, keep_daily=0, keep_weekly=0, keep_monthly=0)
    record = create_managed_backup(root, cfg)
    local = Path(record["local_path"])

    dry = prune_local_backups(cfg, apply=False)
    assert dry["eligible"] == []
    assert dry["skipped"][0]["reason"] == "no_verified_replica"
    assert local.exists()

    replicate_archive(local, cfg, replica_root=tmp_path / "off-device")
    applied = prune_local_backups(cfg, apply=True, ack=PRUNE_ACK)

    assert str(local) in applied["deleted"]
    assert not local.exists()


def test_corrupt_replica_blocks_local_prune(tmp_path):
    root = _runtime_fixture(tmp_path)
    cfg = _cfg(tmp_path, keep_latest=0, keep_daily=0, keep_weekly=0, keep_monthly=0)
    record = create_managed_backup(root, cfg)
    replica = replicate_archive(record["local_path"], cfg, replica_root=tmp_path / "off-device")
    Path(replica["path"]).write_bytes(b"corrupt")

    result = prune_local_backups(cfg, apply=False)

    assert result["eligible"] == []
    assert result["skipped"][0]["reason"] == "no_verified_replica"
    assert Path(record["local_path"]).exists()


def test_restore_drill_verifies_restored_bytes_and_writes_report(tmp_path):
    root = _runtime_fixture(tmp_path)
    cfg = _cfg(tmp_path)
    record = create_managed_backup(root, cfg)

    report = restore_drill(record["local_path"], cfg)

    assert report["ok"] is True
    assert report["restored_files"] >= 2
    assert Path(cfg.drill_report_path).exists()
