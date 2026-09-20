# Phase 33 — Runtime Heartbeat Attestation & Remote Liveness Ledger

Phase 33 extends the Phase 31/32 startup evidence into an ongoing signed runtime trail. The goal is to prove not only which approved release booted, but that a specific trading machine continued reaching production supervision checkpoints with a stated health/halt condition.

## What is signed

Each checkpoint is an Ed25519-signed `checkpoint.json` using the already managed `runtime_boot` trust role. It binds:

- exact deployment environment ID and machine ID;
- source commit and release ID inherited from the Phase 32 audit entry;
- exact Phase 31 boot ID and boot-receipt SHA-256;
- exact Phase 32 audit-entry sequence and manifest SHA-256;
- monotonic liveness sequence and previous checkpoint SHA-256;
- production cycle number and checkpoint stage;
- normalized runtime health summary (market state, account/equity summary, managed exposure, incidents, live halt state, production halt state).

A checkpoint therefore cannot be detached from its approved boot and replayed onto another machine/release without breaking the anchor checks.

## Runtime behavior

When `FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER=1`, `src.production` publishes:

- `READY` before `engine.process_latest()` on a healthy cycle. Remote append/verification failure at this point is fail-closed and the cycle does not proceed to order evaluation.
- `DEGRADED` when the production guard is WARN and live processing is skipped.
- `HALTED` when the production guard or live engine halts.
- `ERROR` before reconnect handling for an unexpected runtime exception, when the remote ledger remains available.

The supervisor enables Phase 33 by default whenever the Phase 32 remote audit ledger is enabled. A deliberate migration bypass is `FOREX_REQUIRE_RUNTIME_LIVENESS_LEDGER=0`.

## Remote layout

Phase 33 reuses `FOREX_AUDIT_LEDGER_ROOT` / `FOREX_AUDIT_LEDGER_SUBDIR` and stores liveness below the existing per-environment/per-machine Phase 32 scope:

```text
<remote>/<subdir>/<environment>/<machine>/
  head.json                  # Phase 32 boot-audit head
  entries/                   # Phase 32 boot evidence
  liveness/
    head.json                # convenience pointer, not the trust root
    entries/
      00000001-<checkpoint-id>/
        checkpoint.json
        checkpoint.signature.json
      ...
```

The verifier recomputes the contiguous checkpoint sequence, validates every detached signature and checkpoint link, then verifies each checkpoint's audit anchor against the already verified Phase 32 ledger.

## Split-brain protection

A process may publish only while its local Phase 31 boot receipt is the current Phase 32 audit head. If a newer boot becomes the audit head, an older process receives `LIVENESS_BOOT_NOT_AUDIT_HEAD` and must stop rather than continuing to publish a competing runtime history.

## Freshness / remote watchdog

A second machine can verify both integrity and recent liveness:

```bash
python -m src.runtime_liveness \
  --mode verify \
  --root . \
  --environment-id prod-bkk-01 \
  --machine-id trader-pc-01 \
  --replica-root /mnt/forex-audit \
  --max-age-seconds 120 \
  --require-current-boot
```

A stale checkpoint is evidence that the remote observer has not seen recent supervision progress; it is not proof of the root cause. Network-share failure, process death, host failure, or deliberate shutdown can all produce staleness.

## Storage boundary

Like Phase 32, append-only behavior is enforced by the application. A privileged storage administrator who can delete the complete tail and consistently rewind mutable metadata cannot be defeated by signatures on that same storage alone. Use WORM/Object Lock/immutable snapshots and/or an independent watcher that remembers the previously observed head for stronger deletion detection.

## Safety

Phase 33 never sends, retries, repairs, resizes, closes, or opens a broker order. Its only trading-path effect is fail-closed gating: a required remote liveness checkpoint must be durably appended and verified before a healthy cycle proceeds to `engine.process_latest()`.
