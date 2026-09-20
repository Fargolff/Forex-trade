# Phase 28 — CI Attestation & Source-Commit Provenance

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
