from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CI_MODULE = r'''from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import string
from typing import Any

from cryptography.exceptions import InvalidSignature

from .recovery import DEPLOYMENT_PATTERNS, sha256_file
from .release import generate_keypair, load_private_key, load_public_key, public_key_fingerprint

ATTESTATION_FORMAT = "forex-auto-trader-ci-attestation"
ATTESTATION_VERSION = 1
DEFAULT_ATTESTATION = "release/ci_attestation.json"
DEFAULT_SIGNATURE = "release/ci_attestation.signature.json"
DEFAULT_PUBLIC_KEY = "release/forex-ci-attestation-public.pem"
PRIVATE_KEY_ENV = "FOREX_CI_ATTESTATION_PRIVATE_KEY"
DEFAULT_REPOSITORY = "Fargolff/Forex-trade"
DEFAULT_WORKFLOW = "Forex Auto Trader Tests"
DEFAULT_WORKFLOW_PATH = ".github/workflows/tests.yml"
DEFAULT_MAIN_REF = "refs/heads/main"
DEFAULT_SOAK_CYCLES = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(value: str) -> str:
    text = str(value).strip().lower()
    if len(text) not in (40, 64) or any(char not in string.hexdigits for char in text):
        raise ValueError("CI source_commit must be a full 40- or 64-character hexadecimal commit identifier")
    return text


def _path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _outside_private_key(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError("CI attestation private key must remain outside the project root")
    return path


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def source_tree_report(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    found: dict[str, Path] = {}
    for pattern in DEPLOYMENT_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                found[path.relative_to(root).as_posix()] = path
    entries = [
        {"path": relative, "size": found[relative].stat().st_size, "sha256": sha256_file(found[relative])}
        for relative in sorted(found)
    ]
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "entries_count": len(entries),
        "entries": entries,
    }


def workflow_report(root: str | Path, workflow_path: str | Path = DEFAULT_WORKFLOW_PATH) -> dict[str, Any]:
    root = Path(root).resolve()
    target = _path(root, workflow_path)
    if not target.is_file():
        raise FileNotFoundError(f"CI workflow file is missing: {target}")
    return {
        "path": target.relative_to(root).as_posix(),
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
    }


def create_ci_attestation(
    root: str | Path,
    output_path: str | Path = DEFAULT_ATTESTATION,
    *,
    source_commit: str,
    repository: str = DEFAULT_REPOSITORY,
    workflow: str = DEFAULT_WORKFLOW,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
    run_id: str | int,
    run_attempt: str | int = 1,
    run_url: str,
    event_name: str = "push",
    ref: str = DEFAULT_MAIN_REF,
    pytest_status: str = "passed",
    soak_status: str = "passed",
    soak_cycles: int = DEFAULT_SOAK_CYCLES,
) -> dict[str, Any]:
    root = Path(root).resolve()
    commit = _commit(source_commit)
    if not str(repository).strip() or not str(workflow).strip():
        raise ValueError("repository and workflow are required")
    if int(run_attempt) < 1 or int(soak_cycles) < 0:
        raise ValueError("run_attempt must be >=1 and soak_cycles cannot be negative")
    document = {
        "version": ATTESTATION_VERSION,
        "format": ATTESTATION_FORMAT,
        "created_at": _now(),
        "repository": str(repository).strip(),
        "source_commit": commit,
        "source_tree": source_tree_report(root),
        "workflow": {
            "name": str(workflow).strip(),
            **workflow_report(root, workflow_path),
            "run_id": str(run_id),
            "run_attempt": int(run_attempt),
            "run_url": str(run_url).strip(),
            "event_name": str(event_name).strip(),
            "ref": str(ref).strip(),
        },
        "checks": {
            "pytest": {"status": str(pytest_status).strip().lower(), "command": "python -m pytest -q"},
            "operational_soak": {
                "status": str(soak_status).strip().lower(),
                "command": f"python -m src.soak --cycles {int(soak_cycles)}",
                "cycles": int(soak_cycles),
            },
        },
    }
    _atomic_json(_path(root, output_path), document)
    return document


def sign_ci_attestation(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    private_key_path: str | Path | None = None,
    signature_path: str | Path = DEFAULT_SIGNATURE,
) -> dict[str, Any]:
    root = Path(root).resolve()
    key_value = private_key_path or os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(f"CI attestation private key path is required via argument or {PRIVATE_KEY_ENV}")
    private_path = _outside_private_key(root, key_value)
    attestation = _path(root, attestation_path)
    payload = attestation.read_bytes()
    private = load_private_key(private_path)
    signature = private.sign(payload)
    document = {
        "version": 1,
        "algorithm": "Ed25519",
        "document": ATTESTATION_FORMAT,
        "attestation_sha256": hashlib.sha256(payload).hexdigest(),
        "public_key_fingerprint": public_key_fingerprint(private.public_key()),
        "signed_at": _now(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    _atomic_json(_path(root, signature_path), document)
    return document


def verify_ci_signature(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    signature_path: str | Path = DEFAULT_SIGNATURE,
    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,
) -> dict[str, Any]:
    try:
        root = Path(root).resolve()
        attestation = _path(root, attestation_path)
        signature_file = _path(root, signature_path)
        public_file = _path(root, public_key_path)
        payload = attestation.read_bytes()
        document = json.loads(signature_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("algorithm") != "Ed25519" or document.get("document") != ATTESTATION_FORMAT:
            raise ValueError("unsupported CI attestation signature format")
        expected_hash = hashlib.sha256(payload).hexdigest()
        if document.get("attestation_sha256") != expected_hash:
            return {"ok": False, "code": "CI_ATTESTATION_HASH_MISMATCH"}
        public = load_public_key(public_file)
        fingerprint = public_key_fingerprint(public)
        if document.get("public_key_fingerprint") != fingerprint:
            return {"ok": False, "code": "CI_PUBLIC_KEY_FINGERPRINT_MISMATCH"}
        try:
            signature = base64.b64decode(str(document.get("signature_b64", "")), validate=True)
        except Exception:
            return {"ok": False, "code": "CI_SIGNATURE_ENCODING_INVALID"}
        try:
            public.verify(signature, payload)
        except InvalidSignature:
            return {"ok": False, "code": "CI_SIGNATURE_INVALID"}
        return {
            "ok": True,
            "code": "CI_SIGNATURE_VALID",
            "attestation_sha256": expected_hash,
            "public_key_fingerprint": fingerprint,
            "signed_at": document.get("signed_at"),
        }
    except Exception as exc:
        return {"ok": False, "code": f"CI_SIGNATURE_ERROR:{type(exc).__name__}:{exc}"}


def verify_ci_attestation(
    root: str | Path,
    attestation_path: str | Path = DEFAULT_ATTESTATION,
    signature_path: str | Path = DEFAULT_SIGNATURE,
    public_key_path: str | Path = DEFAULT_PUBLIC_KEY,
    *,
    expected_source_commit: str | None = None,
    expected_repository: str = DEFAULT_REPOSITORY,
    expected_workflow: str = DEFAULT_WORKFLOW,
    required_soak_cycles: int = DEFAULT_SOAK_CYCLES,
    require_main_ref: bool = True,
) -> dict[str, Any]:
    root = Path(root).resolve()
    signature = verify_ci_signature(root, attestation_path, signature_path, public_key_path)
    if not signature["ok"]:
        return {"ok": False, "code": "CI_ATTESTATION_INVALID", "issues": [f"signature:{signature['code']}"], "signature": signature}
    try:
        document = json.loads(_path(root, attestation_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "code": "CI_ATTESTATION_INVALID", "issues": [f"document:{type(exc).__name__}:{exc}"], "signature": signature}

    issues: list[str] = []
    if not isinstance(document, dict) or document.get("version") != ATTESTATION_VERSION or document.get("format") != ATTESTATION_FORMAT:
        issues.append("CI_ATTESTATION_FORMAT_INVALID")
    try:
        commit = _commit(str(document.get("source_commit", "")))
    except Exception as exc:
        commit = ""
        issues.append(f"CI_SOURCE_COMMIT_INVALID:{type(exc).__name__}")
    if expected_source_commit is not None and commit != _commit(expected_source_commit):
        issues.append("CI_SOURCE_COMMIT_MISMATCH")
    if str(document.get("repository", "")) != str(expected_repository):
        issues.append("CI_REPOSITORY_MISMATCH")

    workflow = document.get("workflow") if isinstance(document.get("workflow"), dict) else {}
    if str(workflow.get("name", "")) != str(expected_workflow):
        issues.append("CI_WORKFLOW_MISMATCH")
    if str(workflow.get("event_name", "")) not in {"push", "workflow_dispatch"}:
        issues.append("CI_EVENT_NOT_RELEASE_ELIGIBLE")
    if require_main_ref and str(workflow.get("ref", "")) != DEFAULT_MAIN_REF:
        issues.append("CI_REF_NOT_MAIN")
    try:
        if int(workflow.get("run_attempt", 0)) < 1:
            issues.append("CI_RUN_ATTEMPT_INVALID")
    except Exception:
        issues.append("CI_RUN_ATTEMPT_INVALID")
    if not str(workflow.get("run_id", "")).strip() or not str(workflow.get("run_url", "")).strip():
        issues.append("CI_RUN_IDENTITY_MISSING")

    checks = document.get("checks") if isinstance(document.get("checks"), dict) else {}
    pytest_check = checks.get("pytest") if isinstance(checks.get("pytest"), dict) else {}
    soak_check = checks.get("operational_soak") if isinstance(checks.get("operational_soak"), dict) else {}
    if str(pytest_check.get("status", "")).lower() != "passed":
        issues.append("CI_PYTEST_NOT_PASSED")
    if str(soak_check.get("status", "")).lower() != "passed":
        issues.append("CI_SOAK_NOT_PASSED")
    try:
        if int(soak_check.get("cycles", -1)) < int(required_soak_cycles):
            issues.append("CI_SOAK_CYCLES_INSUFFICIENT")
    except Exception:
        issues.append("CI_SOAK_CYCLES_INVALID")

    actual_tree = source_tree_report(root)
    signed_tree = document.get("source_tree") if isinstance(document.get("source_tree"), dict) else {}
    if str(signed_tree.get("sha256", "")) != actual_tree["sha256"]:
        issues.append("CI_SOURCE_TREE_MISMATCH")
    if int(signed_tree.get("entries_count", -1)) != actual_tree["entries_count"]:
        issues.append("CI_SOURCE_TREE_ENTRY_COUNT_MISMATCH")
    if signed_tree.get("entries") != actual_tree["entries"]:
        issues.append("CI_SOURCE_TREE_ENTRIES_MISMATCH")

    try:
        current_workflow = workflow_report(root, str(workflow.get("path", DEFAULT_WORKFLOW_PATH)))
        if str(workflow.get("sha256", "")) != current_workflow["sha256"]:
            issues.append("CI_WORKFLOW_FILE_MISMATCH")
        if int(workflow.get("size", -1)) != current_workflow["size"]:
            issues.append("CI_WORKFLOW_FILE_SIZE_MISMATCH")
    except Exception as exc:
        issues.append(f"CI_WORKFLOW_FILE_INVALID:{type(exc).__name__}:{exc}")

    return {
        "ok": not issues,
        "code": "CI_ATTESTATION_VALID" if not issues else "CI_ATTESTATION_INVALID",
        "issues": issues,
        "source_commit": commit,
        "repository": document.get("repository"),
        "workflow": workflow.get("name"),
        "run_id": workflow.get("run_id"),
        "run_attempt": workflow.get("run_attempt"),
        "run_url": workflow.get("run_url"),
        "event_name": workflow.get("event_name"),
        "ref": workflow.get("ref"),
        "source_tree_sha256": actual_tree["sha256"],
        "public_key_fingerprint": signature.get("public_key_fingerprint"),
        "signature": signature,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 28 CI attestation and source-tree provenance")
    parser.add_argument("--mode", choices=["generate-keypair", "create", "sign", "verify"], required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--attestation", default=DEFAULT_ATTESTATION)
    parser.add_argument("--signature", default=DEFAULT_SIGNATURE)
    parser.add_argument("--public-key", default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--private-key")
    parser.add_argument("--source-commit")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--expected-repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--expected-workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--workflow-path", default=DEFAULT_WORKFLOW_PATH)
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt", type=int, default=1)
    parser.add_argument("--run-url")
    parser.add_argument("--event-name", default="push")
    parser.add_argument("--ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--pytest-status", default="passed")
    parser.add_argument("--soak-status", default="passed")
    parser.add_argument("--soak-cycles", type=int, default=DEFAULT_SOAK_CYCLES)
    args = parser.parse_args()
    root = Path(args.root).resolve()

    if args.mode == "generate-keypair":
        if not args.private_key:
            raise ValueError("generate-keypair requires --private-key")
        result = generate_keypair(args.private_key, _path(root, args.public_key))
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "create":
        if not args.source_commit or not args.run_id or not args.run_url:
            raise ValueError("create requires --source-commit, --run-id and --run-url")
        result = create_ci_attestation(
            root,
            args.attestation,
            source_commit=args.source_commit,
            repository=args.repository,
            workflow=args.workflow,
            workflow_path=args.workflow_path,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            run_url=args.run_url,
            event_name=args.event_name,
            ref=args.ref,
            pytest_status=args.pytest_status,
            soak_status=args.soak_status,
            soak_cycles=args.soak_cycles,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.mode == "sign":
        result = sign_ci_attestation(root, args.attestation, args.private_key, args.signature)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    result = verify_ci_attestation(
        root,
        args.attestation,
        args.signature,
        args.public_key,
        expected_source_commit=args.expected_source_commit,
        expected_repository=args.expected_repository,
        expected_workflow=args.expected_workflow,
        required_soak_cycles=args.soak_cycles,
        require_main_ref=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
'''

PHASE28_TESTS = r'''from __future__ import annotations

import json
from pathlib import Path

from src.ci_attestation import (
    DEFAULT_ATTESTATION,
    DEFAULT_PUBLIC_KEY,
    DEFAULT_REPOSITORY,
    DEFAULT_SIGNATURE,
    DEFAULT_WORKFLOW,
    create_ci_attestation,
    sign_ci_attestation,
    verify_ci_attestation,
)
from src.release import generate_keypair


def _fixture(tmp_path: Path, *, commit: str = "1" * 40, ref: str = "refs/heads/main", pytest_status: str = "passed") -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "deploy" / "windows").mkdir(parents=True)
    (root / "deploy" / "windows" / "run.ps1").write_text("Write-Host ok\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "tests.yml").write_text("name: Forex Auto Trader Tests\n", encoding="utf-8")
    (root / "requirements.txt").write_text("pytest>=8\n", encoding="utf-8")
    (root / "config.example.yaml").write_text("mode: backtest\n", encoding="utf-8")
    (root / "production.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "watchdog.example.yaml").write_text("{}\n", encoding="utf-8")
    (root / "release").mkdir()

    keys = tmp_path / "ci-keys"
    keys.mkdir()
    private = keys / "ci-private.pem"
    public = root / DEFAULT_PUBLIC_KEY
    generate_keypair(private, public)
    create_ci_attestation(
        root,
        DEFAULT_ATTESTATION,
        source_commit=commit,
        repository=DEFAULT_REPOSITORY,
        workflow=DEFAULT_WORKFLOW,
        run_id="12345",
        run_attempt=1,
        run_url="https://github.com/Fargolff/Forex-trade/actions/runs/12345",
        event_name="push",
        ref=ref,
        pytest_status=pytest_status,
        soak_status="passed",
        soak_cycles=300,
    )
    sign_ci_attestation(root, DEFAULT_ATTESTATION, private, DEFAULT_SIGNATURE)
    return root, private


def test_signed_ci_attestation_verifies_source_commit_and_tree(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is True
    assert report["source_commit"] == "1" * 40
    assert report["run_id"] == "12345"


def test_source_commit_mismatch_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    report = verify_ci_attestation(root, expected_source_commit="2" * 40)
    assert report["ok"] is False
    assert "CI_SOURCE_COMMIT_MISMATCH" in report["issues"]


def test_source_tree_tamper_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    (root / "src" / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_SOURCE_TREE_MISMATCH" in report["issues"]


def test_workflow_tamper_fails_closed(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    (root / ".github" / "workflows" / "tests.yml").write_text("name: tampered\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_WORKFLOW_FILE_MISMATCH" in report["issues"]


def test_non_main_ref_is_not_release_eligible(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, ref="refs/heads/feature")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_REF_NOT_MAIN" in report["issues"]


def test_failed_pytest_attestation_is_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, pytest_status="failed")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert "CI_PYTEST_NOT_PASSED" in report["issues"]


def test_wrong_ci_public_key_is_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_private = other / "private.pem"
    other_public = other / "public.pem"
    generate_keypair(other_private, other_public)
    report = verify_ci_attestation(root, public_key_path=other_public, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert report["issues"][0].startswith("signature:")


def test_attestation_document_tamper_breaks_signature(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    path = root / DEFAULT_ATTESTATION
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["workflow"]["run_id"] = "99999"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_ci_attestation(root, expected_source_commit="1" * 40)
    assert report["ok"] is False
    assert report["issues"][0].startswith("signature:")
'''

DOC = r'''# Phase 28 — CI Attestation & Source-Commit Provenance

Phase 28 closes the remaining Phase 27 gap: an operator can no longer type an arbitrary `--source-commit` and have that value become trusted release provenance.

A production release now requires a separately signed CI attestation that proves all of the following at the same time:

- the exact full source commit SHA;
- the canonical deployment source-tree fingerprint used by the release tooling;
- the exact permanent CI workflow file hash;
- `pytest` passed;
- the operational soak passed with at least 300 cycles;
- the CI run was a release-eligible `push` or manual run on `refs/heads/main`;
- repository and workflow identity match the expected project.

The CI attestation key is deliberately **not** the Phase 11/27 release key. The CI private key may live in a protected GitHub Actions secret, while the release private key remains offline/protected. Compromise of one trust domain does not automatically provide the other signing authority.

## Trust chain

```text
main commit
   ↓
pytest + operational soak
   ↓
CI source-tree + workflow fingerprint
   ↓
Ed25519 CI attestation (CI key)
   ↓
Phase 28 verification on signing machine
   ↓
Phase 27 manifest/bundle/receipt (offline release key)
```

The release receipt pins the CI attestation and its detached signature by path, size and SHA-256, and records the CI signer fingerprint, workflow run ID/URL and source-tree fingerprint. `verify-receipt` re-verifies the CI evidence against the currently deployed source tree.

## One-time CI key setup

Generate a dedicated CI keypair. Do **not** reuse the release key:

```bash
python -m src.ci_attestation \
  --mode generate-keypair \
  --private-key /secure/forex-ci-attestation-private.pem \
  --public-key release/forex-ci-attestation-public.pem
```

Commit only `release/forex-ci-attestation-public.pem`.

Store the private key as a base64-encoded GitHub Actions secret named:

```text
FOREX_CI_ATTESTATION_PRIVATE_KEY_B64
```

The permanent test workflow only consumes this secret on `main`; pull-request runs never receive it. If the secret is not configured, ordinary PR/main tests still run, but no releasable CI-attestation artifact is produced.

## CI artifact

After a successful eligible main run, GitHub Actions uploads an artifact named roughly:

```text
forex-ci-attestation-<commit>
```

It contains:

```text
release/ci_attestation.json
release/ci_attestation.signature.json
```

Place those two files under `release/` on the protected signing checkout before Phase 27/28 preflight or ceremony.

## Release behavior

`src.release_ceremony` now fails closed before release signing when CI evidence is missing, unsigned, signed by the wrong CI key, points at another commit, another repository/workflow, a non-main ref, failed tests/soak, or a different deployment source tree/workflow file.

The attested source-tree hash uses the same deployment patterns as `src.recovery`, so the source bytes being released must be the source bytes that CI tested. Mutable runtime configuration and the frozen portfolio remain covered separately by Phase 26.

## Threat-model note

A CI-signing-key compromise could forge CI evidence, but it still cannot create a valid Phase 27 release receipt without the separate protected release key. Conversely, possession of the release key is insufficient to claim that an arbitrary source commit passed CI because Phase 28 independently requires the CI signature and source-tree match.

This is provenance hardening, not a profitability or execution guarantee.
'''

WORKFLOW = r'''name: Forex Auto Trader Tests

on:
  pull_request:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read

jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install test dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install "pandas>=2.2" "numpy>=2.0" "PyYAML>=6.0" "cryptography>=43.0" "pytest>=8.0"

      - name: Run tests
        env:
          PYTHONPATH: .
        run: python -m pytest -q

      - name: Run operational soak
        env:
          PYTHONPATH: .
        run: python -m src.soak --cycles 300

      - name: Create signed CI attestation
        id: ci_attestation
        if: ${{ github.event_name != 'pull_request' }}
        env:
          CI_PRIVATE_KEY_B64: ${{ secrets.FOREX_CI_ATTESTATION_PRIVATE_KEY_B64 }}
          SOURCE_COMMIT: ${{ github.sha }}
        run: |
          if [ "$GITHUB_REF" != "refs/heads/main" ]; then
            echo "CI attestation skipped: release attestations are main-only."
            echo "created=false" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          if [ -z "$CI_PRIVATE_KEY_B64" ]; then
            echo "CI attestation skipped: FOREX_CI_ATTESTATION_PRIVATE_KEY_B64 is not configured."
            echo "created=false" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          if [ ! -f release/forex-ci-attestation-public.pem ]; then
            echo "::error::CI attestation secret is configured but release/forex-ci-attestation-public.pem is missing."
            exit 1
          fi
          printf '%s' "$CI_PRIVATE_KEY_B64" | base64 --decode > "$RUNNER_TEMP/forex-ci-attestation-private.pem"
          chmod 600 "$RUNNER_TEMP/forex-ci-attestation-private.pem"
          python -m src.ci_attestation \
            --mode create \
            --root . \
            --source-commit "$SOURCE_COMMIT" \
            --repository "$GITHUB_REPOSITORY" \
            --workflow "$GITHUB_WORKFLOW" \
            --run-id "$GITHUB_RUN_ID" \
            --run-attempt "$GITHUB_RUN_ATTEMPT" \
            --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
            --event-name "$GITHUB_EVENT_NAME" \
            --ref "$GITHUB_REF" \
            --soak-cycles 300
          python -m src.ci_attestation \
            --mode sign \
            --root . \
            --private-key "$RUNNER_TEMP/forex-ci-attestation-private.pem"
          python -m src.ci_attestation \
            --mode verify \
            --root . \
            --expected-source-commit "$SOURCE_COMMIT" \
            --expected-repository "$GITHUB_REPOSITORY" \
            --expected-workflow "$GITHUB_WORKFLOW" \
            --soak-cycles 300
          rm -f "$RUNNER_TEMP/forex-ci-attestation-private.pem"
          echo "created=true" >> "$GITHUB_OUTPUT"

      - name: Upload signed CI attestation
        if: ${{ steps.ci_attestation.outputs.created == 'true' }}
        uses: actions/upload-artifact@v4
        with:
          name: forex-ci-attestation-${{ github.sha }}
          path: |
            release/ci_attestation.json
            release/ci_attestation.signature.json
          retention-days: 30
'''


def must_replace(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Phase 28 anchor not found: {label}")
    return text.replace(old, new, 1)


def patch_release_ceremony() -> None:
    path = ROOT / "src" / "release_ceremony.py"
    text = path.read_text(encoding="utf-8")
    text = must_replace(
        text,
        "from .calendar_provenance import DEFAULT_CALENDAR_PATH, DEFAULT_MIN_COVERAGE_DAYS, bind_calendar_to_manifest, freshness_report, verify_calendar_provenance\nfrom .recovery import create_deployment_manifest, sha256_file, verify_deployment_manifest",
        "from .calendar_provenance import DEFAULT_CALENDAR_PATH, DEFAULT_MIN_COVERAGE_DAYS, bind_calendar_to_manifest, freshness_report, verify_calendar_provenance\nfrom .ci_attestation import DEFAULT_ATTESTATION as DEFAULT_CI_ATTESTATION, DEFAULT_PUBLIC_KEY as DEFAULT_CI_PUBLIC_KEY, DEFAULT_REPOSITORY as DEFAULT_CI_REPOSITORY, DEFAULT_SIGNATURE as DEFAULT_CI_SIGNATURE, DEFAULT_WORKFLOW as DEFAULT_CI_WORKFLOW, verify_ci_attestation\nfrom .recovery import create_deployment_manifest, sha256_file, verify_deployment_manifest",
        "ci import",
    )
    text = must_replace(
        text,
        "    rid = _release_id(release_id)\n    _, fingerprint = _fingerprint_pair(root, private_key_path, public_key_path)",
        "    rid = _release_id(release_id)\n    ci_check = verify_ci_attestation(root, DEFAULT_CI_ATTESTATION, DEFAULT_CI_SIGNATURE, DEFAULT_CI_PUBLIC_KEY, expected_source_commit=commit, expected_repository=DEFAULT_CI_REPOSITORY, expected_workflow=DEFAULT_CI_WORKFLOW, require_main_ref=True)\n    if not ci_check[\"ok\"]:\n        raise RuntimeError(f\"CI attestation preflight failed: {ci_check['issues']}\")\n    _, fingerprint = _fingerprint_pair(root, private_key_path, public_key_path)",
        "preflight ci gate",
    )
    text = must_replace(
        text,
        '        "public_key_fingerprint": fingerprint,\n        "deployment_entries": len(created["entries"]),',
        '        "public_key_fingerprint": fingerprint,\n        "ci_attestation": ci_check,\n        "deployment_entries": len(created["entries"]),',
        "preflight return",
    )
    text = must_replace(
        text,
        "    preflight = release_preflight(root, source_commit=commit, release_id=rid, public_key_path=public_key_path, private_key_path=private, calendar_path=calendar_path, require_calendar=require_calendar, min_coverage_days=min_coverage_days, config_path=config_path, production_path=production_path, production_fallback=production_fallback, reconcile_path=reconcile_path, watchdog_path=watchdog_path, watchdog_fallback=watchdog_fallback)\n    runtime_kwargs = _runtime_kwargs",
        "    preflight = release_preflight(root, source_commit=commit, release_id=rid, public_key_path=public_key_path, private_key_path=private, calendar_path=calendar_path, require_calendar=require_calendar, min_coverage_days=min_coverage_days, config_path=config_path, production_path=production_path, production_fallback=production_fallback, reconcile_path=reconcile_path, watchdog_path=watchdog_path, watchdog_fallback=watchdog_fallback)\n    ci_check = preflight[\"ci_attestation\"]\n    runtime_kwargs = _runtime_kwargs",
        "run ci result",
    )
    text = must_replace(
        text,
        '            "artifacts": {name: _descriptor(staged[name], outputs[name], root) for name in ("manifest", "release_signature", "bundle", "bundle_signature")},\n            "runtime": runtime_binding["portfolio"],',
        '            "artifacts": {name: _descriptor(staged[name], outputs[name], root) for name in ("manifest", "release_signature", "bundle", "bundle_signature")},\n            "ci_attestation": {\n                "attestation": _descriptor(_under(root, DEFAULT_CI_ATTESTATION), _under(root, DEFAULT_CI_ATTESTATION), root),\n                "signature": _descriptor(_under(root, DEFAULT_CI_SIGNATURE), _under(root, DEFAULT_CI_SIGNATURE), root),\n                "public_key_fingerprint": ci_check.get("public_key_fingerprint"),\n                "repository": ci_check.get("repository"),\n                "workflow": ci_check.get("workflow"),\n                "run_id": ci_check.get("run_id"),\n                "run_attempt": ci_check.get("run_attempt"),\n                "run_url": ci_check.get("run_url"),\n                "source_tree_sha256": ci_check.get("source_tree_sha256"),\n            },\n            "runtime": runtime_binding["portfolio"],',
        "receipt ci binding",
    )
    text = must_replace(
        text,
        '                "deployment": bool(deployment["ok"]),\n                "release_signature": bool(release_check["ok"]),',
        '                "deployment": bool(deployment["ok"]),\n                "ci_attestation": bool(ci_check["ok"]),\n                "release_signature": bool(release_check["ok"]),',
        "receipt ci verification",
    )
    text = must_replace(
        text,
        '    if document.get("public_key_fingerprint") != fingerprint:\n        issues.append("receipt:public_key_fingerprint")\n\n    paths: dict[str, Path] = {}',
        '''    if document.get("public_key_fingerprint") != fingerprint:\n        issues.append("receipt:public_key_fingerprint")\n\n    ci_check = None\n    ci_doc = document.get("ci_attestation") if isinstance(document.get("ci_attestation"), dict) else {}\n    ci_paths: dict[str, Path] = {}\n    for role in ("attestation", "signature"):\n        item = ci_doc.get(role)\n        if not isinstance(item, dict):\n            issues.append(f"ci_attestation:{role}:binding")\n            continue\n        try:\n            path = _under(root, str(item.get("path", "")))\n            ci_paths[role] = path\n            if not path.is_file():\n                issues.append(f"ci_attestation:{role}:missing")\n                continue\n            if path.stat().st_size != int(item.get("size", -1)):\n                issues.append(f"ci_attestation:{role}:size")\n            if sha256_file(path) != str(item.get("sha256", "")):\n                issues.append(f"ci_attestation:{role}:sha256")\n        except Exception as exc:\n            issues.append(f"ci_attestation:{role}:{type(exc).__name__}:{exc}")\n    if {"attestation", "signature"} <= set(ci_paths):\n        ci_check = verify_ci_attestation(root, ci_paths["attestation"], ci_paths["signature"], DEFAULT_CI_PUBLIC_KEY, expected_source_commit=commit, expected_repository=DEFAULT_CI_REPOSITORY, expected_workflow=DEFAULT_CI_WORKFLOW, require_main_ref=True)\n        if not ci_check["ok"]:\n            issues.extend(f"ci_attestation:{issue}" for issue in ci_check["issues"])\n        for field in ("public_key_fingerprint", "repository", "workflow", "run_id", "run_attempt", "run_url", "source_tree_sha256"):\n            if ci_doc.get(field) != ci_check.get(field):\n                issues.append(f"ci_attestation:{field}")\n\n    paths: dict[str, Path] = {}''',
        "receipt ci verify",
    )
    text = must_replace(
        text,
        '    return {"ok": not issues, "code": "RELEASE_RECEIPT_VALID" if not issues else "RELEASE_RECEIPT_INVALID", "issues": issues, "source_commit": commit, "release_id": rid, "public_key_fingerprint": fingerprint, "signature": sig, "release": release_check, "runtime": runtime_check, "calendar": calendar_check, "bundle": bundle_check}',
        '    return {"ok": not issues, "code": "RELEASE_RECEIPT_VALID" if not issues else "RELEASE_RECEIPT_INVALID", "issues": issues, "source_commit": commit, "release_id": rid, "public_key_fingerprint": fingerprint, "signature": sig, "ci_attestation": ci_check, "release": release_check, "runtime": runtime_check, "calendar": calendar_check, "bundle": bundle_check}',
        "receipt return",
    )
    text = text.replace('Phase 27 deterministic release ceremony and signed receipt', 'Phase 28 CI-attested deterministic release ceremony and signed receipt', 1)
    path.write_text(text, encoding="utf-8")


def patch_phase27_tests() -> None:
    path = ROOT / "tests" / "test_phase27_release_ceremony.py"
    text = path.read_text(encoding="utf-8")
    text = must_replace(
        text,
        "from src.release import generate_keypair\nfrom src.release_ceremony",
        "from src.ci_attestation import DEFAULT_ATTESTATION as DEFAULT_CI_ATTESTATION, DEFAULT_SIGNATURE as DEFAULT_CI_SIGNATURE, create_ci_attestation, sign_ci_attestation\nfrom src.release import generate_keypair\nfrom src.release_ceremony",
        "phase27 ci imports",
    )
    helper_anchor = "\ndef _fixture(tmp_path: Path) -> tuple[Path, Path, str]:\n"
    helper = '''\ndef _attest(root: Path, commit: str) -> None:\n    create_ci_attestation(\n        root,\n        DEFAULT_CI_ATTESTATION,\n        source_commit=commit,\n        run_id="phase28-test-run",\n        run_attempt=1,\n        run_url="https://github.com/Fargolff/Forex-trade/actions/runs/phase28-test-run",\n        event_name="push",\n        ref="refs/heads/main",\n        soak_cycles=300,\n    )\n    sign_ci_attestation(root, DEFAULT_CI_ATTESTATION, root.parent / "ci-keys" / "ci-private.pem", DEFAULT_CI_SIGNATURE)\n\n\ndef _fixture(tmp_path: Path) -> tuple[Path, Path, str]:\n'''
    text = must_replace(text, helper_anchor, helper, "phase27 attest helper")
    text = must_replace(
        text,
        '    (root / "deploy" / "windows" / "run.ps1").write_text("Write-Host ok\\n", encoding="utf-8")\n    (root / "requirements.txt")',
        '    (root / "deploy" / "windows" / "run.ps1").write_text("Write-Host ok\\n", encoding="utf-8")\n    (root / ".github" / "workflows").mkdir(parents=True)\n    (root / ".github" / "workflows" / "tests.yml").write_text("name: Forex Auto Trader Tests\\n", encoding="utf-8")\n    (root / "requirements.txt")',
        "phase27 workflow fixture",
    )
    text = must_replace(
        text,
        '    generate_keypair(private, public)\n    return root, private, fingerprint',
        '    generate_keypair(private, public)\n    ci_keys = tmp_path / "ci-keys"\n    ci_keys.mkdir()\n    ci_private = ci_keys / "ci-private.pem"\n    ci_public = root / "release" / "forex-ci-attestation-public.pem"\n    generate_keypair(ci_private, ci_public)\n    _attest(root, "a" * 40)\n    return root, private, fingerprint',
        "phase27 ci keys",
    )
    text = must_replace(
        text,
        'def _run(root: Path, private: Path, *, overwrite: bool = False) -> dict:\n    return run_release_ceremony(',
        'def _run(root: Path, private: Path, *, overwrite: bool = False) -> dict:\n    _attest(root, "a" * 40)\n    return run_release_ceremony(',
        "phase27 run reattest",
    )
    text = must_replace(
        text,
        '    result = release_preflight(\n        root,\n        source_commit="b" * 40,',
        '    _attest(root, "b" * 40)\n    result = release_preflight(\n        root,\n        source_commit="b" * 40,',
        "phase27 preflight commit",
    )
    append = '''\n\ndef test_missing_ci_attestation_fails_before_release_signing(tmp_path: Path) -> None:\n    root, private, _ = _fixture(tmp_path)\n    (root / DEFAULT_CI_ATTESTATION).unlink()\n    with pytest.raises(RuntimeError, match="CI attestation preflight failed"):\n        run_release_ceremony(\n            root,\n            source_commit="a" * 40,\n            release_id="missing-ci-attestation",\n            private_key_path=private,\n            require_calendar=True,\n        )\n    assert not (root / "release" / "release_manifest.json").exists()\n'''
    if "test_missing_ci_attestation_fails_before_release_signing" not in text:
        text += append
    path.write_text(text, encoding="utf-8")


def main() -> None:
    (ROOT / "src" / "ci_attestation.py").write_text(CI_MODULE, encoding="utf-8")
    (ROOT / "tests" / "test_phase28_ci_attestation.py").write_text(PHASE28_TESTS, encoding="utf-8")
    (ROOT / "docs" / "phase28-ci-attestation.md").write_text(DOC, encoding="utf-8")
    (ROOT / ".github" / "workflows" / "tests.yml").write_text(WORKFLOW, encoding="utf-8")
    patch_release_ceremony()
    patch_phase27_tests()

    gitignore = ROOT / ".gitignore"
    text = gitignore.read_text(encoding="utf-8")
    for line in ("release/ci_attestation.json", "release/ci_attestation.signature.json"):
        if line not in text:
            text = text.replace("# OS/editor\n", f"{line}\n\n# OS/editor\n")
    gitignore.write_text(text, encoding="utf-8")

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    marker = "## Phase 28 — CI Attestation & Source-Commit Provenance"
    if marker not in text:
        text += "\n\n" + marker + "\n\nProduction release signing now requires a separately signed CI attestation for the exact main-branch source commit and canonical deployment source tree. The CI attestation key is separate from the offline release key; pull-request jobs never receive the CI signing secret. See `docs/phase28-ci-attestation.md`.\n"
    readme.write_text(text, encoding="utf-8")

    print("Phase 28 source migration applied")


if __name__ == "__main__":
    main()
