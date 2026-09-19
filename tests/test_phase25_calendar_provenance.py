from datetime import datetime, timezone
import json

from src.calendar_provenance import (
    bind_calendar_to_manifest,
    calendar_binding,
    freshness_report,
    verify_calendar_provenance,
)


def _write_calendar(path, *, generated_at="2026-09-20T00:00:00Z", valid_through="2026-12-31"):
    path.write_text(
        "\n".join(
            [
                "version: 1",
                f'generated_at: "{generated_at}"',
                f'valid_through: "{valid_through}"',
                "days:",
                '  "2026-12-25":',
                "    closed: true",
                '    reason: "Christmas Day"',
                "closures: []",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_manifest(path, *, external_bindings=None):
    payload = {
        "version": 1,
        "created_at": "2026-09-20T00:00:00+00:00",
        "entries": [],
    }
    if external_bindings is not None:
        payload["external_bindings"] = external_bindings
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now(value="2026-09-20T00:00:00+00:00"):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def test_bind_and_verify_calendar_provenance(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    manifest = tmp_path / "release_manifest.json"
    _write_calendar(calendar)
    _write_manifest(manifest)

    binding = bind_calendar_to_manifest(calendar, manifest)
    assert binding["version"] == 1
    assert binding["calendar_schema_version"] == 1
    assert len(binding["sha256"]) == 64
    assert binding["valid_through"] == "2026-12-31"

    result = verify_calendar_provenance(
        calendar,
        manifest,
        now=_now(),
        min_coverage_days=30,
        required=True,
    )
    assert result["ok"] is True
    assert result["code"] == "CALENDAR_PROVENANCE_VALID"
    assert result["coverage_days"] >= 30


def test_tampered_calendar_fails_signed_hash_binding(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    manifest = tmp_path / "release_manifest.json"
    _write_calendar(calendar)
    _write_manifest(manifest)
    bind_calendar_to_manifest(calendar, manifest)

    calendar.write_text(calendar.read_text(encoding="utf-8") + "# local hot edit\n", encoding="utf-8")
    result = verify_calendar_provenance(calendar, manifest, now=_now(), min_coverage_days=30)
    assert result["ok"] is False
    assert "CALENDAR_SHA256_MISMATCH" in result["issues"]


def test_freshness_reports_expiring_and_expired(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    _write_calendar(calendar, valid_through="2026-09-25")

    expiring = freshness_report(calendar, now=_now(), min_coverage_days=10)
    assert expiring["ok"] is False
    assert expiring["status"] == "EXPIRING"
    assert "CALENDAR_COVERAGE_INSUFFICIENT" in expiring["issues"]

    expired = freshness_report(
        calendar,
        now=_now("2026-09-26T00:00:00+00:00"),
        min_coverage_days=0,
    )
    assert expired["ok"] is False
    assert expired["status"] == "EXPIRED"
    assert "CALENDAR_EXPIRED" in expired["issues"]


def test_optional_missing_calendar_without_binding_is_neutral(tmp_path):
    manifest = tmp_path / "release_manifest.json"
    _write_manifest(manifest)
    result = verify_calendar_provenance(
        tmp_path / "market_calendar.yaml",
        manifest,
        now=_now(),
        min_coverage_days=30,
        required=False,
    )
    assert result["ok"] is True
    assert result["code"] == "CALENDAR_NOT_CONFIGURED"


def test_required_or_bound_missing_calendar_fails_closed(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    manifest = tmp_path / "release_manifest.json"
    _write_manifest(manifest)

    required = verify_calendar_provenance(calendar, manifest, now=_now(), required=True)
    assert required["ok"] is False
    assert required["issues"] == ["CALENDAR_FILE_MISSING"]

    _write_calendar(calendar)
    binding = calendar_binding(calendar)
    _write_manifest(manifest, external_bindings={"market_calendar": binding})
    calendar.unlink()
    bound = verify_calendar_provenance(calendar, manifest, now=_now(), required=False)
    assert bound["ok"] is False
    assert bound["issues"] == ["CALENDAR_FILE_MISSING"]


def test_existing_calendar_without_manifest_binding_fails_closed(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    manifest = tmp_path / "release_manifest.json"
    _write_calendar(calendar)
    _write_manifest(manifest)

    result = verify_calendar_provenance(calendar, manifest, now=_now(), min_coverage_days=30)
    assert result["ok"] is False
    assert result["issues"] == ["CALENDAR_BINDING_MISSING"]


def test_bind_preserves_other_external_bindings(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    manifest = tmp_path / "release_manifest.json"
    _write_calendar(calendar)
    _write_manifest(manifest, external_bindings={"other": {"sha256": "abc"}})

    bind_calendar_to_manifest(calendar, manifest)
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["external_bindings"]["other"] == {"sha256": "abc"}
    assert saved["external_bindings"]["market_calendar"]["sha256"] == calendar_binding(calendar)["sha256"]


def test_generated_at_in_future_fails_freshness(tmp_path):
    calendar = tmp_path / "market_calendar.yaml"
    _write_calendar(calendar, generated_at="2026-09-20T01:00:01Z")
    result = freshness_report(calendar, now=_now(), min_coverage_days=30)
    assert result["ok"] is False
    assert "CALENDAR_GENERATED_IN_FUTURE" in result["issues"]
