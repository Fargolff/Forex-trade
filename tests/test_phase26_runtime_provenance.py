from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.runtime_provenance import (
    bind_runtime_to_manifest,
    compute_design_fingerprint,
    portfolio_integrity_report,
    verify_runtime_provenance,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fixture_tree(tmp_path: Path, *, use_fallbacks: bool = False) -> tuple[Path, str]:
    root = tmp_path
    (root / "release").mkdir()
    (root / "release" / "release_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [],
                "external_bindings": {
                    "market_calendar": {
                        "version": 1,
                        "sha256": "calendar-binding-is-preserved",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "config.yaml").write_text(
        "\n".join(
            [
                "mode: backtest",
                "live:",
                "  enabled: false",
                "  weights_path: results/portfolio/portfolio_weights.csv",
                "  candidates_path: results/portfolio/portfolio_candidates.csv",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "reconcile.yaml").write_text("{}\n", encoding="utf-8")

    if use_fallbacks:
        (root / "production.example.yaml").write_text("{}\n", encoding="utf-8")
        (root / "watchdog.example.yaml").write_text("{}\n", encoding="utf-8")
    else:
        (root / "production.yaml").write_text("{}\n", encoding="utf-8")
        (root / "watchdog.yaml").write_text("{}\n", encoding="utf-8")
        (root / "production.example.yaml").write_text("alert_timeout_seconds: 7\n", encoding="utf-8")
        (root / "watchdog.example.yaml").write_text("poll_seconds: 90\n", encoding="utf-8")

    portfolio_rows = [
        {
            "strategy": "ema_trend",
            "weight": 0.6,
            "selected_params": {"fast": 12, "slow": 30},
        },
        {
            "strategy": "rsi_reversion",
            "weight": 0.4,
            "selected_params": {"period": 14, "oversold": 30, "overbought": 70},
        },
    ]
    fingerprint = compute_design_fingerprint(portfolio_rows)

    weights = [
        {
            "strategy": row["strategy"],
            "weight": row["weight"],
            "selected_params": json.dumps(row["selected_params"], sort_keys=True),
            "design_fingerprint": fingerprint,
        }
        for row in portfolio_rows
    ]
    candidates = [
        {
            "strategy": row["strategy"],
            "selected_params": json.dumps(row["selected_params"], sort_keys=True),
            "design_fingerprint": fingerprint,
            "pre_oos_verdict": "PASS",
        }
        for row in portfolio_rows
    ]
    frozen = [
        {
            "strategy": row["strategy"],
            "weight": row["weight"],
            "selected_params": json.dumps(row["selected_params"], sort_keys=True),
        }
        for row in portfolio_rows
    ]

    portfolio_root = root / "results" / "portfolio"
    _write_csv(
        portfolio_root / "portfolio_weights.csv",
        ["strategy", "weight", "selected_params", "design_fingerprint"],
        weights,
    )
    _write_csv(
        portfolio_root / "portfolio_candidates.csv",
        ["strategy", "selected_params", "design_fingerprint", "pre_oos_verdict"],
        candidates,
    )
    _write_csv(
        portfolio_root / "portfolio_frozen_design.csv",
        ["strategy", "weight", "selected_params"],
        frozen,
    )
    return root, fingerprint


def test_bind_and_verify_runtime_controls_preserves_other_bindings(tmp_path: Path) -> None:
    root, fingerprint = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"

    binding = bind_runtime_to_manifest(root, manifest)
    assert binding["portfolio"]["design_fingerprint"] == fingerprint
    assert binding["portfolio"]["active_strategies"] == 2

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert "market_calendar" in document["external_bindings"]
    assert "runtime_controls" in document["external_bindings"]

    verified = verify_runtime_provenance(root, manifest, required=True)
    assert verified["ok"] is True
    assert verified["code"] == "RUNTIME_PROVENANCE_VALID"
    assert verified["design_fingerprint"] == fingerprint


def test_config_tamper_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"
    bind_runtime_to_manifest(root, manifest)

    with (root / "config.yaml").open("a", encoding="utf-8") as handle:
        handle.write("initial_equity: 999999\n")

    report = verify_runtime_provenance(root, manifest, required=True)
    assert report["ok"] is False
    assert "RUNTIME_FILE_SHA256_MISMATCH:config" in report["issues"]


def test_portfolio_tamper_is_detected_by_hash_and_integrity(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"
    bind_runtime_to_manifest(root, manifest)

    weights = root / "results" / "portfolio" / "portfolio_weights.csv"
    text = weights.read_text(encoding="utf-8").replace("0.6", "0.7", 1)
    weights.write_text(text, encoding="utf-8")

    report = verify_runtime_provenance(root, manifest, required=True)
    assert report["ok"] is False
    assert "RUNTIME_FILE_SHA256_MISMATCH:portfolio_weights" in report["issues"]
    assert any(issue.startswith("PORTFOLIO_INTEGRITY_INVALID:") for issue in report["issues"])


def test_bind_rejects_cross_file_fingerprint_inconsistency(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    candidates = root / "results" / "portfolio" / "portfolio_candidates.csv"
    rows = list(csv.DictReader(candidates.open("r", newline="", encoding="utf-8")))
    rows[0]["selected_params"] = json.dumps({"fast": 13, "slow": 30}, sort_keys=True)
    _write_csv(
        candidates,
        ["strategy", "selected_params", "design_fingerprint", "pre_oos_verdict"],
        rows,
    )

    with pytest.raises(ValueError, match="candidate parameters disagree"):
        portfolio_integrity_report(
            root / "results" / "portfolio" / "portfolio_weights.csv",
            candidates,
            root / "results" / "portfolio" / "portfolio_frozen_design.csv",
        )


def test_missing_required_binding_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"

    required = verify_runtime_provenance(root, manifest, required=True)
    assert required["ok"] is False
    assert required["issues"] == ["RUNTIME_BINDING_MISSING"]

    optional = verify_runtime_provenance(root, manifest, required=False)
    assert optional["ok"] is True
    assert optional["code"] == "RUNTIME_PROVENANCE_NOT_CONFIGURED"


def test_fallback_source_shadowing_is_rejected(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path, use_fallbacks=True)
    manifest = root / "release" / "release_manifest.json"
    binding = bind_runtime_to_manifest(root, manifest)
    assert binding["source_selection"]["production_config"]["mode"] == "fallback"
    assert binding["source_selection"]["watchdog_config"]["mode"] == "fallback"

    (root / "production.yaml").write_text("{}\n", encoding="utf-8")
    report = verify_runtime_provenance(root, manifest, required=True)
    assert report["ok"] is False
    assert "RUNTIME_SOURCE_CHANGED:production_config" in report["issues"]


def test_config_portfolio_path_must_match_signed_binding(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"
    bind_runtime_to_manifest(root, manifest)

    config = root / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "results/portfolio/portfolio_weights.csv",
            "results/portfolio/other_weights.csv",
        ),
        encoding="utf-8",
    )
    (root / "results" / "portfolio" / "other_weights.csv").write_text(
        (root / "results" / "portfolio" / "portfolio_weights.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    report = verify_runtime_provenance(root, manifest, required=True)
    assert report["ok"] is False
    assert "RUNTIME_FILE_SHA256_MISMATCH:config" in report["issues"]
    assert "PORTFOLIO_WEIGHTS_PATH_MISMATCH" in report["issues"]


def test_project_relative_path_policy_rejects_escape(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    config = root / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "results/portfolio/portfolio_weights.csv",
            "../outside.csv",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project-relative"):
        bind_runtime_to_manifest(root, root / "release" / "release_manifest.json")


def test_missing_bound_portfolio_file_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture_tree(tmp_path)
    manifest = root / "release" / "release_manifest.json"
    bind_runtime_to_manifest(root, manifest)

    (root / "results" / "portfolio" / "portfolio_frozen_design.csv").unlink()
    report = verify_runtime_provenance(root, manifest, required=True)
    assert report["ok"] is False
    assert "RUNTIME_FILE_MISSING:portfolio_frozen_design" in report["issues"]
