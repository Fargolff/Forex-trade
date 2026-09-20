# Phase 29 — Signing-Key Rotation, Expiry & Revocation

Phase 29 turns signing keys into managed identities instead of permanent single-file trust anchors.

## Trust store

The default policy file is `release/signing_key_trust.json`. It is versioned JSON with one record per public key:

- `key_id`: stable operator-visible ID, for example `release-2026-q4`
- `role`: `release` or `ci_attestation`
- `public_key`: project-relative PEM path, normally under `release/keys/`
- `fingerprint`: SHA-256 fingerprint of the Ed25519 SubjectPublicKeyInfo DER
- `valid_from`: timezone-aware ISO-8601 timestamp
- `valid_until`: optional exclusive expiry timestamp
- `revoked`: hard-revocation switch
- `revoked_at` / `revocation_reason`: optional incident metadata

The policy and trusted public PEMs are included in deployment/source-tree provenance. Editing them after CI attestation therefore invalidates the attested source tree.

## Rotation model

Planned rotation uses overlapping validity windows. During the overlap, both old and new keys may be trusted for signing. The signer is selected by the private key fingerprint and the matching `key_id` is embedded into new signature documents.

After the old key's `valid_until`, it cannot create a new signature. Existing signatures made before expiry remain verifiable because verification evaluates the signature's recorded `signed_at` against that key's historical validity window.

## Revocation model

`revoked: true` is deliberately stronger than expiry. A revoked key is rejected for verification even if the signature was created before the revocation. Use hard revocation for suspected compromise. Use `valid_until` for ordinary planned retirement.

## Signature version 2

Phase 29 signatures add `key_id` and use signature document version 2 for:

- release manifest signatures
- deterministic release-bundle signatures
- release-receipt signatures
- CI attestation signatures

Legacy version-1 primitives remain verifiable when used directly with an explicitly supplied public key, preserving Phase 11/12 compatibility. The Phase 29 release ceremony is stricter: it requires the trust store and version-2 key identity.

## Required operator setup

1. Keep all private keys outside the repository.
2. Put only public keys under `release/keys/`.
3. Create `release/signing_key_trust.json` from `signing_key_trust.example.json`.
4. Compute every fingerprint from the actual PEM; never hand-copy a guessed fingerprint.
5. Give CI only the current CI-attestation private key through the protected secret. Never give CI the release private key.
6. For planned rotation, add the new public key and policy record before the new key's `valid_from`.
7. Keep the retiring public key and record so historical releases remain verifiable.
8. For compromise, set `revoked: true`, document the reason, re-run CI, and rotate immediately.

## Fail-closed cases

Release/attestation verification fails when:

- the trust store is missing or malformed
- `key_id` is unknown or duplicated
- a PEM fingerprint differs from the policy
- a signing key is expired, not yet valid, or revoked
- a signature claims a different fingerprint for its `key_id`
- a signature timestamp is outside the key's validity window
- the trust store or trusted public-key set changes after CI attestation

These controls protect release identity and provenance. They do not guarantee trading profitability, order fills, broker availability, or strategy performance.
