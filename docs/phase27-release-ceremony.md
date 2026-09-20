# Phase 27 — Deterministic Release Ceremony & Signed Receipt

Phase 27 turns the multi-command signing workflow from Phases 11, 12, 25 and 26 into one ordered, fail-closed release ceremony.

The goal is not to make signing keys easier to access. The private Ed25519 key should remain on the protected/offline signing machine. The goal is to make the *procedure* hard to perform out of order or incompletely.

## Ceremony order

`python -m src.release_ceremony --mode run ...` performs this sequence:

1. validate source commit and release ID;
2. verify the trusted public key and, when supplied, prove the private key matches it;
3. validate runtime config / portfolio integrity;
4. validate market-calendar structure and freshness;
5. build a fresh deployment manifest in staging;
6. bind Phase 25 calendar provenance when a calendar is present;
7. bind Phase 26 runtime/config/portfolio provenance;
8. verify deployment + calendar + runtime provenance before signing;
9. sign the manifest with Ed25519;
10. verify the signed release immediately;
11. build the deterministic Phase 12 bundle pinned to source commit + release ID;
12. sign and verify the bundle;
13. create a release receipt containing artifact hashes, release identity, public-key fingerprint, portfolio design fingerprint and calendar provenance;
14. sign and verify the receipt;
15. publish artifacts from staging; the signed receipt is the final commit marker;
16. run a final verification against the published files.

Any failed check aborts the ceremony. No broker order, live enablement, position repair or automatic deployment is performed.

## Preflight first

The preflight mode performs all validation that can be performed without publishing release artifacts:

```bash
python -m src.release_ceremony \
  --mode preflight \
  --source-commit <40_HEX_COMMIT> \
  --release-id <UNIQUE_RELEASE_ID> \
  --public-key release/forex-release-public.pem \
  --calendar market_calendar.yaml \
  --require-calendar
```

`preflight` is read-only with respect to final release outputs. It uses a temporary manifest to prove that the deployment, calendar binding and runtime binding are internally consistent.

If `--private-key` (or `FOREX_RELEASE_PRIVATE_KEY`) is supplied to preflight, the key is loaded only to prove that its public half matches the trusted public key. It does not sign or publish anything.

## One-command ceremony

On the protected signing machine:

```bash
python -m src.release_ceremony \
  --mode run \
  --source-commit <40_HEX_COMMIT> \
  --release-id <UNIQUE_RELEASE_ID> \
  --private-key /secure/offline/forex-release-private.pem \
  --public-key release/forex-release-public.pem \
  --calendar market_calendar.yaml \
  --require-calendar
```

The private key path may also be supplied through `FOREX_RELEASE_PRIVATE_KEY`.

Phase 27 refuses to use a private key located inside the project root. This prevents an operator from accidentally leaving the signing key in the release/deployment tree.

By default the ceremony refuses to overwrite any existing final artifact. `--overwrite` is explicit and should only be used when intentionally regenerating the same release outputs after review.

## Published artifacts

Default outputs are:

```text
release/release_manifest.json
release/release_signature.json
release/forex-release-bundle.zip
release/forex-release-bundle.signature.json
release/release_receipt.json
release/release_receipt.signature.json
```

The receipt records SHA-256 + byte size + final project-relative path for the manifest, manifest signature, bundle and bundle signature. It also records:

- `source_commit`
- `release_id`
- trusted public-key fingerprint
- deployment entry count
- Phase 21/26 portfolio design fingerprint
- active strategy set
- calendar SHA / validity horizon when configured
- the outcome of the deployment, release-signature, calendar, runtime and bundle gates

The receipt itself has a detached Ed25519 signature. A copied/edited receipt therefore cannot silently redefine which artifacts belong to a release.

## Receipt as commit marker

All work is performed in staging first. Final artifacts are only copied after all pre-sign and post-sign checks succeed.

The receipt and its signature are published last. Operationally, a release is considered complete only when the receipt signature verifies and `verify-receipt` returns `RELEASE_RECEIPT_VALID`.

This is intentionally stronger than checking for the existence of a ZIP file: a crash or interrupted copy may leave partial files, but it cannot create a valid signed receipt for them.

## Verify the complete release later

On the signing machine or deployment/trading machine:

```bash
python -m src.release_ceremony \
  --mode verify-receipt \
  --source-commit <EXPECTED_COMMIT> \
  --release-id <EXPECTED_RELEASE_ID> \
  --public-key release/forex-release-public.pem \
  --calendar market_calendar.yaml
```

Verification checks:

- receipt Ed25519 signature;
- expected source-commit and release-ID anti-rollback pins;
- artifact size + SHA-256 from the receipt;
- Phase 11 signed release + deployment hashes;
- Phase 26 runtime/config/portfolio provenance;
- Phase 25 calendar provenance/freshness;
- Phase 12 bundle signature + embedded release + deployed manifest/signature binding.

Changing `config.yaml`, portfolio CSVs, the calendar, manifest, signatures or bundle after the ceremony makes verification fail closed.

## Output path rules

Manifest, signatures, bundle and receipt outputs must be distinct project-relative paths. Absolute output paths, drive-qualified paths and `..` traversal are rejected.

The calendar may remain external deployment data and can be supplied by absolute path. The private key may also be outside the project root and should normally live on protected/offline storage.

## Failure examples

Typical failures include:

```text
release private key does not match the trusted public key
release private key must remain outside the project root
refusing to overwrite existing release artifacts
market calendar freshness preflight failed
runtime provenance preflight failed
receipt_signature:RECEIPT_HASH_MISMATCH
artifact:bundle:sha256
anti_rollback:source_commit
anti_rollback:release_id
runtime:RUNTIME_FILE_SHA256_MISMATCH:config
calendar:CALENDAR_SHA256_MISMATCH
```

## Important limitation

The ceremony pins the `source_commit` supplied by the operator into the signed bundle and signed receipt. It does not independently contact GitHub or prove that the local working tree corresponds to that commit. The deployment manifest still cryptographically identifies the exact release files, and CI should be green for the commit being released. Operators should supply the exact reviewed commit SHA.

Phase 27 is a release-integrity control, not a profitability control. It does not enable live trading, store the live arm phrase, alter broker positions or guarantee trading results.
