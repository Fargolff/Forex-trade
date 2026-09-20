# Phase 30 — Release Promotion & Deployment Approval Gate

Phase 30 separates **building a cryptographically valid release** from **authorizing that release for a concrete deployment environment**.

## Promotion states

1. **BUILT** — Phase 27/29 release artifacts and signed release receipt exist.
2. **VERIFIED** — the receipt, CI provenance, runtime provenance, calendar provenance and signed bundle verify successfully; `prepare` writes a promotion candidate.
3. **APPROVED** — a distinct Ed25519 key with trust-store role `deployment_approval` signs the exact VERIFIED candidate.
4. **DEPLOYABLE** — the signed approval is still valid, not revoked/expired, its receipt hashes still match, and its environment ID exactly matches the local Trading PC environment ID.

An APPROVED document by itself is not sufficient. The Trading PC re-verifies the signature, key policy, expiry and all bound release receipt data on every supervisor start/restart.

## Separation of duties

Use three independent trust roles:

- `ci_attestation`: proves tested source/CI provenance.
- `release`: builds and signs release artifacts.
- `deployment_approval`: authorizes a particular built release for a particular environment.

The deployment-approval private key must remain outside the repository and must **not** be copied to the Trading PC. The Trading PC needs only the trust store and the corresponding public key under `release/keys/`.

## Approval lifetime

Approvals default to 24 hours and may not exceed 168 hours (7 days). Short-lived approval reduces the risk that an old approval is silently reused after operational context changes.

Expiry and revocation remain different:

- expiry prevents deployment after `valid_until`;
- hard key revocation invalidates even a previously signed approval.

## Commands

Prepare a VERIFIED candidate after the release ceremony:

```powershell
python -m src.deployment_approval --mode prepare `
  --environment-id prod-bkk-01 `
  --expected-source-commit <FULL_COMMIT> `
  --expected-release-id <RELEASE_ID>
```

Approve it from an approval workstation with the approval private key kept outside the project:

```powershell
python -m src.deployment_approval --mode approve `
  --environment-id prod-bkk-01 `
  --private-key C:\secure\deployment-approval-private.pem `
  --valid-hours 24 `
  --approved-by change-ticket-1234
```

Verify on the Trading PC:

```powershell
python -m src.deployment_approval --mode verify `
  --environment-id prod-bkk-01
```

Check the promotion stage:

```powershell
python -m src.deployment_approval --mode status `
  --environment-id prod-bkk-01
```

## Supervisor gate

`deploy/windows/run-live-supervisor.ps1` enables the Phase 30 approval gate by default whenever signed-release verification is enabled. Required production setting:

```text
FOREX_DEPLOYMENT_ENVIRONMENT_ID=prod-bkk-01
```

Optional paths:

```text
FOREX_DEPLOYMENT_APPROVAL_PATH=release/deployment_approval.json
FOREX_DEPLOYMENT_APPROVAL_SIGNATURE=release/deployment_approval.signature.json
FOREX_SIGNING_KEY_TRUST_STORE=release/signing_key_trust.json
```

A deliberate migration bypass can set `FOREX_REQUIRE_DEPLOYMENT_APPROVAL=0`; this weakens the deployment trust chain and should not be used as the steady-state production configuration.

## Fail-closed conditions

Deployment is blocked if any of the following occurs:

- approval or signature missing;
- approval key is not trusted for `deployment_approval`;
- approval key is revoked;
- signature/document is modified;
- approval is expired or has an excessive validity window;
- environment ID differs;
- release receipt or receipt signature differs from the approved hashes;
- release receipt no longer verifies;
- source commit/release ID anti-rollback values differ.

Phase 30 never enables live trading, sends broker orders, retries orders, or repairs positions. The existing exact live arming phrase remains required independently.
