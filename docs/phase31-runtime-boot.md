# Phase 31 — Deployment Receipt & Runtime Boot Attestation

Phase 31 creates a fresh, signed boot receipt immediately before every supervised-live start or restart. The goal is to prove which approved release a specific Trading PC actually attempted to boot.

## Trust separation

Phase 31 adds a fourth managed signing role: `runtime_boot`.

- `ci_attestation` proves tested source provenance.
- `release` signs release artifacts.
- `deployment_approval` authorizes one release for one environment.
- `runtime_boot` is held by the Trading PC and can sign only runtime boot attestations.

The runtime boot private key must remain outside the repository. Compromise of this key does not grant permission to build a release or approve a deployment.

## Boot receipt bindings

Each receipt binds:

- fresh random `boot_id`
- UTC creation time
- exact `environment_id`
- exact operator-configured `machine_id`
- source commit and release ID
- release signing key ID
- portfolio frozen-design fingerprint
- release receipt and release receipt signature path/size/SHA-256
- deployment approval and approval signature path/size/SHA-256
- deployment approval key ID and validity deadline
- runtime boot key ID and public-key fingerprint
- SHA-256 of the previous current boot receipt

A detached Ed25519 signature protects the full boot receipt.

## Replay protection and history

Supervisor verification requires the current boot receipt to be no older than 300 seconds by default. Every restart creates a new receipt before launching supervised live.

Each successful creation is also archived under:

`runtime/boot_attestations/<boot_id>.json`

and

`runtime/boot_attestations/<boot_id>.signature.json`

The current receipt carries `previous_boot_receipt_sha256`, giving restarts a simple tamper-evident hash chain while archived receipts remain available for audit.

## Required production environment variables

When signed-release/deployment approval gates are enabled, Phase 31 defaults on as well:

- `FOREX_RUNTIME_MACHINE_ID=trader-pc-01`
- `FOREX_RUNTIME_BOOT_PRIVATE_KEY=C:\\secure\\runtime-boot-private.pem`

Existing Phase 30 `FOREX_DEPLOYMENT_ENVIRONMENT_ID` remains required.

Optional migration bypass:

`FOREX_REQUIRE_RUNTIME_BOOT_ATTESTATION=0`

This should only be used during a controlled migration.

## Supervisor order

The supervisor now runs:

1. signed bundle verification
2. signed release verification
3. market calendar provenance
4. runtime/portfolio provenance
5. deployment approval verification
6. restart reconciliation
7. create + verify fresh runtime boot attestation
8. supervised live process

The boot receipt therefore represents a point-in-time proof that all preceding gates were accepted immediately before launch.

## Fail-closed cases

Startup is blocked when any of these occur:

- runtime boot private key is missing or inside the project tree
- runtime boot key is not trusted for role `runtime_boot`
- runtime boot key is expired, not-yet-valid, or revoked
- machine/environment ID differs
- receipt is stale or from the future
- approval or release receipt changed after boot receipt creation
- any bound file path/size/SHA-256 differs
- detached boot signature is invalid
- current deployment approval is no longer DEPLOYABLE

Phase 31 does not enable live trading and does not send, retry, repair, resize, or flatten broker orders/positions.
