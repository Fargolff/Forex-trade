from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

BINDING_KEY = "runtime_controls"
BINDING_VERSION = 1
DEFAULT_MANIFEST = "release/release_manifest.json"
DEFAULT_CONFIG = "config.yaml"
DEFAULT_PRODUCTION = "production.yaml"
DEFAULT_PRODUCTION_FALLBACK = "production.example.yaml"
DEFAULT_RECONCILE = "reconcile.yaml"
DEFAULT_WATCHDOG = "watchdog.yaml"
DEFAULT_WATCHDOG_FALLBACK = "watchdog.example.yaml"
DEFAULT_WEIGHTS = "results/portfolio/portfolio_weights.csv"
DEFAULT_CANDIDATES = "results/portfolio/portfolio_candidates.csv"
DEFAULT_FROZEN = "portfolio_frozen_design.csv"


def _safe(value: str | Path) -> str:
    text = str(value).strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"runtime provenance path must be project-relative: {value!r}")
    return path.as_posix()


def _under(root: Path, value: str | Path) -> Path:
    relative = _safe(value)
    target = (root.resolve() / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"runtime provenance path escapes project root: {value!r}")
    return target


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError("deployment manifest is invalid")
    return value


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _params(raw: Any, strategy: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except Exception as exc:
        raise ValueError(f"{source} selected_params invalid for {strategy}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{source} selected_params for {strategy} must be an object")
    return value


def compute_design_fingerprint(rows: list[dict[str, Any]]) -> str:
    payload: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        strategy = str(row["strategy"]).strip()
        weight = float(row["weight"])
        params = row["selected_params"]
        if not strategy or strategy in seen:
            raise ValueError(f"invalid/duplicate portfolio strategy: {strategy!r}")
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"portfolio weight for {strategy} must be finite and positive")
        if not isinstance(params, dict):
            raise ValueError(f"selected_params for {strategy} must be an object")
        seen.add(strategy)
        payload.append({"strategy": strategy, "weight": weight, "selected_params": params})
    if not payload:
        raise ValueError("portfolio has no active strategies")
    payload.sort(key=lambda item: item["strategy"])
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def portfolio_integrity_report(
    weights_path: str | Path,
    candidates_path: str | Path,
    frozen_design_path: str | Path,
) -> dict[str, Any]:
    weights = _csv(Path(weights_path))
    candidates = _csv(Path(candidates_path))
    frozen = _csv(Path(frozen_design_path))
    if not weights or not {"strategy", "weight", "selected_params", "design_fingerprint"} <= set(weights[0]):
        raise ValueError("portfolio weights require strategy, weight, selected_params and design_fingerprint")
    if not candidates or not {"strategy", "selected_params", "design_fingerprint"} <= set(candidates[0]):
        raise ValueError("portfolio candidates require strategy, selected_params and design_fingerprint")
    if not frozen or not {"strategy", "weight", "selected_params"} <= set(frozen[0]):
        raise ValueError("portfolio frozen design requires strategy, weight and selected_params")

    active: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in weights:
        strategy = str(row["strategy"]).strip()
        if strategy in seen:
            raise ValueError("portfolio weights contain duplicate strategies")
        seen.add(strategy)
        weight = float(row["weight"])
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("portfolio weights must be finite and non-negative")
        if weight > 0:
            active.append({
                "strategy": strategy,
                "weight": weight,
                "selected_params": _params(row["selected_params"], strategy, "weights"),
                "declared": str(row["design_fingerprint"]).strip().lower(),
            })
    if not math.isclose(sum(row["weight"] for row in active), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError("active portfolio weights must sum to 1.0")

    design = [{key: row[key] for key in ("strategy", "weight", "selected_params")} for row in active]
    fingerprint = compute_design_fingerprint(design)
    if {row["declared"] for row in active} != {fingerprint}:
        raise ValueError("portfolio weights design_fingerprint does not match active design")

    candidate_map: dict[str, dict[str, str]] = {}
    for row in candidates:
        strategy = str(row["strategy"]).strip()
        if strategy in candidate_map:
            raise ValueError("portfolio candidates contain duplicate strategies")
        candidate_map[strategy] = row
    active_map = {row["strategy"]: row for row in active}
    for strategy, expected in active_map.items():
        if strategy not in candidate_map:
            raise ValueError(f"active strategy missing from portfolio candidates: {strategy}")
        row = candidate_map[strategy]
        if _params(row["selected_params"], strategy, "candidates") != expected["selected_params"]:
            raise ValueError(f"candidate parameters disagree with weights for {strategy}")
        if str(row["design_fingerprint"]).strip().lower() != fingerprint:
            raise ValueError(f"candidate design_fingerprint disagrees for {strategy}")

    frozen_map: dict[str, dict[str, str]] = {}
    for row in frozen:
        strategy = str(row["strategy"]).strip()
        if strategy in frozen_map:
            raise ValueError("portfolio frozen design contains duplicate strategies")
        frozen_map[strategy] = row
    if set(frozen_map) != set(active_map):
        raise ValueError("portfolio frozen design strategy set differs from active weights")
    frozen_design: list[dict[str, Any]] = []
    for strategy in sorted(active_map):
        row = frozen_map[strategy]
        weight = float(row["weight"])
        params = _params(row["selected_params"], strategy, "frozen_design")
        if not math.isclose(weight, active_map[strategy]["weight"], rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"frozen-design weight disagrees for {strategy}")
        if params != active_map[strategy]["selected_params"]:
            raise ValueError(f"frozen-design parameters disagree for {strategy}")
        frozen_design.append({"strategy": strategy, "weight": weight, "selected_params": params})
    if compute_design_fingerprint(frozen_design) != fingerprint:
        raise ValueError("portfolio frozen design fingerprint disagrees with active weights")
    return {
        "ok": True,
        "design_fingerprint": fingerprint,
        "active_strategies": len(active),
        "strategies": sorted(active_map),
    }


def _config_paths(root: Path, config_path: str) -> tuple[str, str, str]:
    raw = yaml.safe_load(_under(root, config_path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml root must be a mapping")
    live = raw.get("live") or {}
    if not isinstance(live, dict):
        raise ValueError("config live section must be a mapping")
    weights = _safe(live.get("weights_path", DEFAULT_WEIGHTS))
    candidates = _safe(live.get("candidates_path", DEFAULT_CANDIDATES))
    frozen = _safe((PurePosixPath(weights).parent / DEFAULT_FROZEN).as_posix())
    return weights, candidates, frozen


def _binding(root: Path, path: str | Path) -> dict[str, Any]:
    relative = _safe(path)
    target = _under(root, relative)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    return {"path": relative, "size": target.stat().st_size, "sha256": _sha(target)}


def _effective(root: Path, preferred: str | Path, fallback: str | Path) -> tuple[str, str]:
    preferred, fallback = _safe(preferred), _safe(fallback)
    if _under(root, preferred).is_file():
        return preferred, "preferred"
    if _under(root, fallback).is_file():
        return fallback, "fallback"
    raise FileNotFoundError(f"neither {preferred} nor {fallback} exists")


def build_runtime_binding(
    root: str | Path,
    *,
    config_path: str | Path = DEFAULT_CONFIG,
    production_path: str | Path = DEFAULT_PRODUCTION,
    production_fallback: str | Path = DEFAULT_PRODUCTION_FALLBACK,
    reconcile_path: str | Path = DEFAULT_RECONCILE,
    watchdog_path: str | Path = DEFAULT_WATCHDOG,
    watchdog_fallback: str | Path = DEFAULT_WATCHDOG_FALLBACK,
) -> dict[str, Any]:
    root = Path(root).resolve()
    config_path, reconcile_path = _safe(config_path), _safe(reconcile_path)
    weights, candidates, frozen = _config_paths(root, config_path)
    prod, prod_mode = _effective(root, production_path, production_fallback)
    watch, watch_mode = _effective(root, watchdog_path, watchdog_fallback)
    files = {
        "config": _binding(root, config_path),
        "production_config": _binding(root, prod),
        "reconcile_config": _binding(root, reconcile_path),
        "watchdog_config": _binding(root, watch),
        "portfolio_weights": _binding(root, weights),
        "portfolio_candidates": _binding(root, candidates),
        "portfolio_frozen_design": _binding(root, frozen),
    }
    portfolio = portfolio_integrity_report(_under(root, weights), _under(root, candidates), _under(root, frozen))
    return {
        "version": BINDING_VERSION,
        "files": files,
        "source_selection": {
            "production_config": {
                "preferred": _safe(production_path), "fallback": _safe(production_fallback),
                "selected": prod, "mode": prod_mode,
            },
            "watchdog_config": {
                "preferred": _safe(watchdog_path), "fallback": _safe(watchdog_fallback),
                "selected": watch, "mode": watch_mode,
            },
        },
        "portfolio": portfolio,
    }


def bind_runtime_to_manifest(root: str | Path, manifest_path: str | Path, **kwargs: Any) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    manifest = _json(manifest_path)
    external = manifest.get("external_bindings") or {}
    if not isinstance(external, dict):
        raise ValueError("deployment manifest external_bindings must be an object")
    binding = build_runtime_binding(root, **kwargs)
    manifest["external_bindings"] = {**external, BINDING_KEY: binding}
    temp = manifest_path.with_suffix(manifest_path.suffix + ".runtime-bind-tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(manifest_path)
    return binding


def _file_issues(root: Path, role: str, expected: Any) -> list[str]:
    if not isinstance(expected, dict):
        return [f"RUNTIME_FILE_BINDING_INVALID:{role}"]
    try:
        target = _under(root, expected.get("path", ""))
    except Exception:
        return [f"RUNTIME_FILE_PATH_INVALID:{role}"]
    if not target.is_file():
        return [f"RUNTIME_FILE_MISSING:{role}"]
    issues: list[str] = []
    if target.stat().st_size != int(expected.get("size", -1)):
        issues.append(f"RUNTIME_FILE_SIZE_MISMATCH:{role}")
    if _sha(target) != str(expected.get("sha256", "")).lower():
        issues.append(f"RUNTIME_FILE_SHA256_MISMATCH:{role}")
    return issues


def verify_runtime_provenance(
    root: str | Path,
    manifest_path: str | Path,
    *,
    config_path: str | Path = DEFAULT_CONFIG,
    production_path: str | Path = DEFAULT_PRODUCTION,
    production_fallback: str | Path = DEFAULT_PRODUCTION_FALLBACK,
    reconcile_path: str | Path = DEFAULT_RECONCILE,
    watchdog_path: str | Path = DEFAULT_WATCHDOG,
    watchdog_fallback: str | Path = DEFAULT_WATCHDOG_FALLBACK,
    required: bool = False,
) -> dict[str, Any]:
    root = Path(root).resolve()
    external = _json(manifest_path).get("external_bindings") or {}
    if not isinstance(external, dict):
        return {"ok": False, "code": "RUNTIME_PROVENANCE_INVALID", "issues": ["MANIFEST_EXTERNAL_BINDINGS_INVALID"]}
    expected = external.get(BINDING_KEY)
    if expected is None:
        return {
            "ok": not required,
            "code": "RUNTIME_PROVENANCE_NOT_CONFIGURED" if not required else "RUNTIME_PROVENANCE_INVALID",
            "issues": [] if not required else ["RUNTIME_BINDING_MISSING"],
        }
    if not isinstance(expected, dict):
        return {"ok": False, "code": "RUNTIME_PROVENANCE_INVALID", "issues": ["RUNTIME_BINDING_INVALID"]}

    issues: list[str] = []
    if int(expected.get("version", -1)) != BINDING_VERSION:
        issues.append("RUNTIME_BINDING_VERSION_MISMATCH")
    files = expected.get("files")
    if not isinstance(files, dict):
        return {"ok": False, "code": "RUNTIME_PROVENANCE_INVALID", "issues": ["RUNTIME_FILES_BINDING_INVALID"]}
    roles = (
        "config", "production_config", "reconcile_config", "watchdog_config",
        "portfolio_weights", "portfolio_candidates", "portfolio_frozen_design",
    )
    for role in roles:
        issues.extend(
            [f"RUNTIME_FILE_BINDING_MISSING:{role}"]
            if role not in files else _file_issues(root, role, files[role])
        )

    try:
        if _safe(files["config"]["path"]) != _safe(config_path):
            issues.append("RUNTIME_CONFIG_PATH_MISMATCH")
        if _safe(files["reconcile_config"]["path"]) != _safe(reconcile_path):
            issues.append("RECONCILE_CONFIG_PATH_MISMATCH")
    except Exception:
        issues.append("RUNTIME_BOUND_PATH_INVALID")

    selection = expected.get("source_selection")
    if not isinstance(selection, dict):
        issues.append("RUNTIME_SOURCE_SELECTION_INVALID")
    else:
        for role, preferred, fallback in (
            ("production_config", production_path, production_fallback),
            ("watchdog_config", watchdog_path, watchdog_fallback),
        ):
            try:
                rule = selection[role]
                selected, mode = _effective(root, preferred, fallback)
                if selected != _safe(rule.get("selected", "")) or mode != str(rule.get("mode", "")):
                    issues.append(f"RUNTIME_SOURCE_CHANGED:{role}")
                if _safe(preferred) != _safe(rule.get("preferred", "")) or _safe(fallback) != _safe(rule.get("fallback", "")):
                    issues.append(f"RUNTIME_SOURCE_POLICY_MISMATCH:{role}")
            except Exception as exc:
                issues.append(f"RUNTIME_SOURCE_INVALID:{role}:{type(exc).__name__}")

    try:
        weights, candidates, frozen = _config_paths(root, _safe(config_path))
        bound_weights = _safe(files["portfolio_weights"]["path"])
        bound_candidates = _safe(files["portfolio_candidates"]["path"])
        bound_frozen = _safe(files["portfolio_frozen_design"]["path"])
        if bound_weights != weights:
            issues.append("PORTFOLIO_WEIGHTS_PATH_MISMATCH")
        if bound_candidates != candidates:
            issues.append("PORTFOLIO_CANDIDATES_PATH_MISMATCH")
        if bound_frozen != frozen:
            issues.append("PORTFOLIO_FROZEN_DESIGN_PATH_MISMATCH")
        actual = portfolio_integrity_report(_under(root, bound_weights), _under(root, bound_candidates), _under(root, bound_frozen))
        signed = expected.get("portfolio")
        if not isinstance(signed, dict):
            issues.append("PORTFOLIO_BINDING_INVALID")
        else:
            if str(signed.get("design_fingerprint", "")).lower() != actual["design_fingerprint"]:
                issues.append("PORTFOLIO_DESIGN_FINGERPRINT_MISMATCH")
            if int(signed.get("active_strategies", -1)) != actual["active_strategies"]:
                issues.append("PORTFOLIO_ACTIVE_STRATEGY_COUNT_MISMATCH")
            if sorted(map(str, signed.get("strategies", []))) != actual["strategies"]:
                issues.append("PORTFOLIO_STRATEGY_SET_MISMATCH")
    except Exception as exc:
        issues.append(f"PORTFOLIO_INTEGRITY_INVALID:{type(exc).__name__}:{exc}")

    return {
        "ok": not issues,
        "code": "RUNTIME_PROVENANCE_VALID" if not issues else "RUNTIME_PROVENANCE_INVALID",
        "issues": issues,
        "binding_present": True,
        "design_fingerprint": expected.get("portfolio", {}).get("design_fingerprint") if isinstance(expected.get("portfolio"), dict) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 26 signed runtime-config and portfolio provenance gate")
    parser.add_argument("--mode", choices=["inspect", "bind", "verify"], default="inspect")
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--production-config", default=DEFAULT_PRODUCTION)
    parser.add_argument("--production-fallback", default=DEFAULT_PRODUCTION_FALLBACK)
    parser.add_argument("--reconcile-config", default=DEFAULT_RECONCILE)
    parser.add_argument("--watchdog-config", default=DEFAULT_WATCHDOG)
    parser.add_argument("--watchdog-fallback", default=DEFAULT_WATCHDOG_FALLBACK)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    kwargs = {
        "config_path": args.config,
        "production_path": args.production_config,
        "production_fallback": args.production_fallback,
        "reconcile_path": args.reconcile_config,
        "watchdog_path": args.watchdog_config,
        "watchdog_fallback": args.watchdog_fallback,
    }
    if args.mode == "bind":
        result = {"ok": True, "binding": bind_runtime_to_manifest(args.root, args.manifest, **kwargs)}
    elif args.mode == "verify":
        result = verify_runtime_provenance(args.root, args.manifest, required=args.required, **kwargs)
    else:
        result = {"ok": True, "binding": build_runtime_binding(args.root, **kwargs)}
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.mode == "verify" and not result["ok"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
