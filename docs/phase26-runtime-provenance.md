# Phase 26 — Signed Runtime Config & Portfolio Provenance

Phase 26 extends the existing Ed25519 signed-release trust chain to the mutable files that decide live risk and portfolio behavior on the trading machine.

The source code can be perfectly signed while `config.yaml`, production controls, reconciliation policy, watchdog policy, or portfolio CSVs have been edited locally. Phase 26 closes that gap by binding those runtime controls into the already signed `release/release_manifest.json`.

## What is bound

The runtime binding covers:

- `config.yaml`
- the effective production config: `production.yaml`, otherwise `production.example.yaml`
- `reconcile.yaml`
- the effective watchdog config: `watchdog.yaml`, otherwise `watchdog.example.yaml`
- the live portfolio weights path selected by `config.yaml`
- the live portfolio candidates path selected by `config.yaml`
- `portfolio_frozen_design.csv` beside the weights file

Each file is recorded with project-relative path, byte size, and SHA-256.

Secrets remain environment-only and are intentionally not copied into the signed manifest.

## Portfolio semantic proof

Hashing the files is not sufficient by itself. Phase 26 also proves the three portfolio artifacts describe the same frozen pre-OOS design.

For every active strategy it checks:

- weight is finite, positive and all active weights sum to 1;
- `selected_params` in weights and candidates agree;
- active strategy sets agree with `portfolio_frozen_design.csv`;
- frozen-design weights and parameters agree with the active weights file;
- every active weights/candidates `design_fingerprint` equals the recomputed Phase 21 fingerprint.

The fingerprint is recomputed with the same canonical payload used by Phase 21:

```text
SHA256(canonical JSON of sorted strategy + weight + selected_params rows)
```

This catches a mixed bundle such as weights from one research run combined with candidates or frozen design from another run.

## Bind before signing

Generate the normal deployment manifest, add external bindings, and only then sign it:

```bash
python -m src.recovery \
  --mode make-manifest \
  --root . \
  --manifest release/release_manifest.json

# Phase 25, when a production calendar is deployed
python -m src.calendar_provenance \
  --mode bind \
  --path market_calendar.yaml \
  --manifest release/release_manifest.json

# Phase 26
python -m src.runtime_provenance \
  --mode bind \
  --root . \
  --manifest release/release_manifest.json \
  --config config.yaml \
  --reconcile-config reconcile.yaml

python -m src.release \
  --mode sign \
  --manifest release/release_manifest.json \
  --signature release/release_signature.json \
  --private-key <OFFLINE_PRIVATE_KEY_PATH>
```

If Phase 12 signed bundles are used, build and sign the bundle only after the final manifest has both required external bindings and the new release signature.

## Inspect and verify

Inspect the runtime selection without changing a manifest:

```bash
python -m src.runtime_provenance --mode inspect --root .
```

Verify deployed runtime controls against a signed-manifest binding:

```bash
python -m src.runtime_provenance \
  --mode verify \
  --root . \
  --manifest release/release_manifest.json \
  --config config.yaml \
  --reconcile-config reconcile.yaml \
  --required
```

The runtime verifier does not validate the Ed25519 signature itself. The Windows supervisor runs Phase 11 signature verification first, then Phase 25/26 external-binding verification against that authenticated manifest.

## Preferred/fallback source protection

Production and watchdog loaders can use example files as fallbacks when their preferred local files do not exist. Phase 26 signs the exact selection mode as well as the file bytes.

Example:

```text
bind time: production.yaml missing
selected:  production.example.yaml (fallback)
```

If `production.yaml` later appears, the effective runtime source has changed. Verification fails with `RUNTIME_SOURCE_CHANGED:production_config` even if the previously bound fallback file was untouched.

The same protection applies to `watchdog.yaml` / `watchdog.example.yaml`.

## Path safety

All bound paths must be project-relative. Absolute paths, Windows drive prefixes, and `..` traversal are rejected.

The live portfolio paths are read from `config.yaml`. If the config later points at another weights/candidates file, verification reports both the config hash drift and portfolio path mismatch instead of silently following the new files.

## Windows supervisor integration

`deploy/windows/run-live-supervisor.ps1` enables runtime provenance by default whenever:

```text
FOREX_REQUIRE_SIGNED_RELEASE=1
```

Explicit control:

```text
FOREX_REQUIRE_RUNTIME_PROVENANCE=1
```

Enabling Phase 26 without the Phase 11 signed-release gate is rejected because the signed manifest is the trust anchor.

Supervisor order becomes:

```text
Signed Bundle
→ Signed Release
→ Calendar Provenance / Freshness
→ Runtime Config & Portfolio Provenance
→ Restart Reconciliation
→ Supervised Live
```

The gate is repeated on every supervisor start/restart.

## Updating risk, config, or portfolio files

Do not hot-edit a bound production file and continue under the old signature. Use an auditable release flow:

1. stop/review the supervised process;
2. change or regenerate the intended runtime file;
3. validate the config and portfolio artifacts;
4. regenerate the deployment manifest;
5. re-bind the market calendar when applicable;
6. bind Phase 26 runtime controls;
7. sign the final manifest with the protected/offline Ed25519 private key;
8. deploy manifest, signature, runtime files and any bundle as one reviewed release;
9. verify all gates before explicitly armed live execution.

## Main failure classes

Examples include:

- `RUNTIME_BINDING_MISSING`
- `RUNTIME_FILE_MISSING:<role>`
- `RUNTIME_FILE_SIZE_MISMATCH:<role>`
- `RUNTIME_FILE_SHA256_MISMATCH:<role>`
- `RUNTIME_SOURCE_CHANGED:<role>`
- `RUNTIME_CONFIG_PATH_MISMATCH`
- `RECONCILE_CONFIG_PATH_MISMATCH`
- `PORTFOLIO_WEIGHTS_PATH_MISMATCH`
- `PORTFOLIO_CANDIDATES_PATH_MISMATCH`
- `PORTFOLIO_FROZEN_DESIGN_PATH_MISMATCH`
- `PORTFOLIO_DESIGN_FINGERPRINT_MISMATCH`
- `PORTFOLIO_INTEGRITY_INVALID:...`

Any Phase 26 verification failure blocks supervised live startup when the gate is required. It never edits broker positions, repairs a portfolio automatically, or resends an order.

## Migration note

After upgrading from Phase 25 to Phase 26, an old signed manifest has no `runtime_controls` binding. With signed-release production gating enabled, create and sign a new manifest before supervised live startup. Do not disable the gate merely to keep using an old manifest.

## Scope

Phase 26 proves that selected local runtime controls are the same controls approved when the release manifest was signed, and that the portfolio files remain semantically consistent with the Phase 21 frozen design. It does not prove that the strategy will be profitable, that broker fills will match research, or that the chosen risk policy is appropriate.

Live trading remains disabled by default and still requires the exact live arming phrase plus all existing account, margin, market-session, recovery and production safety gates.
