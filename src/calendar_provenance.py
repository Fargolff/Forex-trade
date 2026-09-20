from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .market_calendar import load_market_calendar


BINDING_KEY = "market_calendar"
BINDING_VERSION = 1
DEFAULT_CALENDAR_PATH = "market_calendar.yaml"
DEFAULT_MANIFEST_PATH = "release/release_manifest.json"
DEFAULT_MIN_COVERAGE_DAYS = 30


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_generated_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"calendar generated_at must be an ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("calendar generated_at must include timezone information")
    return parsed.astimezone(timezone.utc)


def _parse_valid_through(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"calendar valid_through must be YYYY-MM-DD, got {value!r}") from exc


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_calendar_document(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    # Reuse the Phase 24 parser first so provenance can never bless a malformed
    # or unsupported broker calendar.
    load_market_calendar(target, required=True)
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("market calendar root must be a mapping")
    return raw


def calendar_metadata(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    raw = _load_calendar_document(target)
    if "generated_at" not in raw:
        raise ValueError("market calendar provenance requires generated_at")
    if "valid_through" not in raw:
        raise ValueError("market calendar provenance requires valid_through")

    generated_at = _parse_generated_at(raw["generated_at"])
    valid_through = _parse_valid_through(raw["valid_through"])
    return {
        "calendar_schema_version": int(raw.get("version", 1)),
        "generated_at": generated_at.isoformat(),
        "valid_through": valid_through.isoformat(),
        "sha256": sha256_file(target),
    }


def calendar_binding(path: str | Path) -> dict[str, Any]:
    metadata = calendar_metadata(path)
    return {
        "version": BINDING_VERSION,
        **metadata,
    }


def _load_manifest(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ValueError("deployment manifest is invalid")
    return raw


def bind_calendar_to_manifest(calendar_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    manifest_target = Path(manifest_path)
    manifest = _load_manifest(manifest_target)
    external = manifest.get("external_bindings") or {}
    if not isinstance(external, dict):
        raise ValueError("deployment manifest external_bindings must be an object")

    binding = calendar_binding(calendar_path)
    external = dict(external)
    external[BINDING_KEY] = binding
    manifest["external_bindings"] = external

    temp = manifest_target.with_suffix(manifest_target.suffix + ".calendar-bind-tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(manifest_target)
    return binding


def freshness_report(
    calendar_path: str | Path,
    *,
    now: datetime | None = None,
    min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS,
) -> dict[str, Any]:
    if min_coverage_days < 0:
        raise ValueError("min_coverage_days cannot be negative")
    current = _as_utc(now or datetime.now(timezone.utc))
    metadata = calendar_metadata(calendar_path)
    generated_at = _parse_generated_at(metadata["generated_at"])
    valid_through = _parse_valid_through(metadata["valid_through"])
    coverage_days = (valid_through - current.date()).days

    issues: list[str] = []
    if generated_at > current + timedelta(minutes=5):
        issues.append("CALENDAR_GENERATED_IN_FUTURE")
    if coverage_days < 0:
        issues.append("CALENDAR_EXPIRED")
    elif coverage_days < min_coverage_days:
        issues.append("CALENDAR_COVERAGE_INSUFFICIENT")

    if "CALENDAR_EXPIRED" in issues:
        status = "EXPIRED"
    elif "CALENDAR_COVERAGE_INSUFFICIENT" in issues:
        status = "EXPIRING"
    elif issues:
        status = "INVALID"
    else:
        status = "VALID"

    return {
        "ok": not issues,
        "status": status,
        "issues": issues,
        "sha256": metadata["sha256"],
        "generated_at": metadata["generated_at"],
        "valid_through": metadata["valid_through"],
        "coverage_days": coverage_days,
        "min_coverage_days": int(min_coverage_days),
    }


def verify_calendar_provenance(
    calendar_path: str | Path,
    manifest_path: str | Path,
    *,
    now: datetime | None = None,
    min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS,
    required: bool = False,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    external = manifest.get("external_bindings") or {}
    if not isinstance(external, dict):
        return {
            "ok": False,
            "code": "CALENDAR_PROVENANCE_INVALID",
            "issues": ["MANIFEST_EXTERNAL_BINDINGS_INVALID"],
        }
    expected = external.get(BINDING_KEY)
    target = Path(calendar_path)

    if not target.exists():
        if expected is not None or required:
            return {
                "ok": False,
                "code": "CALENDAR_PROVENANCE_INVALID",
                "issues": ["CALENDAR_FILE_MISSING"],
                "calendar_path": str(target),
            }
        return {
            "ok": True,
            "code": "CALENDAR_NOT_CONFIGURED",
            "issues": [],
            "calendar_path": str(target),
            "binding_present": False,
        }

    if not isinstance(expected, dict):
        return {
            "ok": False,
            "code": "CALENDAR_PROVENANCE_INVALID",
            "issues": ["CALENDAR_BINDING_MISSING"],
            "calendar_path": str(target),
        }

    issues: list[str] = []
    try:
        actual = calendar_binding(target)
        freshness = freshness_report(target, now=now, min_coverage_days=min_coverage_days)
    except Exception as exc:
        return {
            "ok": False,
            "code": "CALENDAR_PROVENANCE_INVALID",
            "issues": [f"CALENDAR_INVALID:{type(exc).__name__}:{exc}"],
            "calendar_path": str(target),
        }

    if int(expected.get("version", -1)) != BINDING_VERSION:
        issues.append("CALENDAR_BINDING_VERSION_MISMATCH")
    if str(expected.get("sha256", "")) != actual["sha256"]:
        issues.append("CALENDAR_SHA256_MISMATCH")
    if int(expected.get("calendar_schema_version", -1)) != int(actual["calendar_schema_version"]):
        issues.append("CALENDAR_SCHEMA_VERSION_MISMATCH")
    if str(expected.get("generated_at", "")) != str(actual["generated_at"]):
        issues.append("CALENDAR_GENERATED_AT_MISMATCH")
    if str(expected.get("valid_through", "")) != str(actual["valid_through"]):
        issues.append("CALENDAR_VALID_THROUGH_MISMATCH")
    issues.extend(str(issue) for issue in freshness["issues"])

    return {
        "ok": not issues,
        "code": "CALENDAR_PROVENANCE_VALID" if not issues else "CALENDAR_PROVENANCE_INVALID",
        "issues": issues,
        "calendar_path": str(target),
        "binding_present": True,
        "sha256": actual["sha256"],
        "generated_at": actual["generated_at"],
        "valid_through": actual["valid_through"],
        "coverage_days": freshness["coverage_days"],
        "min_coverage_days": freshness["min_coverage_days"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 25 market-calendar provenance and freshness gate")
    parser.add_argument("--mode", choices=["inspect", "bind", "verify"], default="inspect")
    parser.add_argument("--path", default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--min-coverage-days", type=int, default=DEFAULT_MIN_COVERAGE_DAYS)
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--at", help="ISO-8601 timestamp used for deterministic freshness checks")
    args = parser.parse_args()

    when = _parse_generated_at(args.at) if args.at else None

    if args.mode == "bind":
        result = {
            "ok": True,
            "calendar_path": args.path,
            "manifest": args.manifest,
            "binding": bind_calendar_to_manifest(args.path, args.manifest),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.mode == "verify":
        result = verify_calendar_provenance(
            args.path,
            args.manifest,
            now=when,
            min_coverage_days=args.min_coverage_days,
            required=args.required,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0 if result["ok"] else 3)

    target = Path(args.path)
    if not target.exists():
        result = {
            "ok": not args.required,
            "code": "CALENDAR_NOT_CONFIGURED" if not args.required else "CALENDAR_FILE_MISSING",
            "calendar_path": str(target),
        }
    else:
        result = freshness_report(target, now=when, min_coverage_days=args.min_coverage_days)
        result["calendar_path"] = str(target)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
