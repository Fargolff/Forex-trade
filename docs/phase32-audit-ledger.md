# Phase 32 — Remote Audit Ledger & Boot Receipt Replication

Phase 32 moves Phase 31 boot evidence off the Trading PC before supervised live starts. The goal is forensic survivability: a local runtime-folder loss should not erase the evidence of which approved release booted on which machine.

## Evidence copied per boot

Each immutable application-level ledger entry contains exact copies of:

- runtime boot receipt + detached signature
- deployment approval + detached signature
- release receipt + detached signature
- a new `ledger_entry.json` + detached Ed25519 signature

The ledger manifest is signed by the same managed `runtime_boot` trust role. This does not grant release or deployment-approval authority.

## Double chain

Each continuation entry must satisfy both:

1. `previous_entry_manifest_sha256` equals the preceding remote ledger manifest hash.
2. the signed boot receipt's `previous_boot_receipt_sha256` equals the preceding replicated boot-receipt hash.

The first Phase 32 entry is `GENESIS` when there is no prior Phase 31 boot, or `ANCHOR` when Phase 31 history already exists locally. `ANCHOR` is explicit: it never pretends older, non-replicated history exists remotely.

## Append-only behavior

The application never overwrites or deletes an existing entry directory. Re-appending the current boot is idempotent only when the already-stored ledger validates byte-for-byte. Boot-ID collisions, old-boot replay, forks, chain mismatches, remote tamper, missing entries, and head rollback inconsistencies fail closed.

`head.json` is mutable convenience metadata; verification does not trust it alone. It recomputes all entry hashes/signatures and checks the complete contiguous sequence.

## Production gate

When Phase 31 is enabled, Phase 32 is enabled by default. Configure:

```text
FOREX_AUDIT_LEDGER_ROOT=\\audit-server\forex-audit
FOREX_AUDIT_LEDGER_SUBDIR=Forex-trade/audit-ledger
```

The root must be outside the project tree. Prefer an actual off-device network share, separate host, object-storage gateway, or immutable/WORM destination.

A deliberate migration bypass requires:

```text
FOREX_REQUIRE_REMOTE_AUDIT_LEDGER=0
```

The production sequence becomes:

```text
signed bundle
→ signed release
→ calendar provenance
→ runtime/portfolio provenance
→ deployment approval
→ restart reconciliation
→ fresh runtime boot attestation
→ append + verify remote audit ledger
→ supervised live
```

If the remote ledger is unavailable, invalid, forked, or cannot accept a verified append, supervised live does not start.

## Important limitation

Phase 32 is append-only at the application layer. Software on the Trading PC cannot cryptographically prove that a privileged administrator of fully mutable remote storage did not delete the newest tail and also rewrite mutable metadata. For stronger deletion resistance, configure storage-side WORM/Object Lock/immutable snapshots and restrict delete/overwrite permissions independently of the Trading PC.

Phase 32 does not send broker orders, enable live trading, repair positions, or guarantee trading performance.
