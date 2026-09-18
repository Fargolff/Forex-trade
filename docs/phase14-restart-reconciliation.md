# Phase 14 — Broker State Disaster Recovery & Restart Reconciliation

Phase 14 adds a fail-closed reconciliation gate that runs before the Windows live supervisor starts or restarts the production process.

The gate is intentionally **read-only**. It never opens, closes, resizes or repairs broker positions automatically. If broker state cannot be explained from trusted broker deal history plus local state/audit evidence, supervised live does not start.

## What Phase 14 compares

```text
Current MT5 account + managed positions
              ↓
Recent broker deal history
              ↓
Reconstructed position lifecycle
              ↓
Local live_state.json
              ↓
Local ENTRY audit events (including rotated logs)
              ↓
Prior restart checkpoint
              ↓
OK or CRITICAL
```

## Files

```text
reconcile.example.yaml
src/restart_reconcile.py
runtime/restart_reconcile.json
runtime/restart_checkpoint.json
```

Copy the example configuration only if overrides are needed:

```powershell
Copy-Item reconcile.example.yaml reconcile.yaml
```

`reconcile.yaml` is ignored by Git.

## Default behavior

The default broker-history lookback is 2160 hours (90 days). A currently open managed position whose opening deal is older than the configured lookback causes a CRITICAL result rather than being guessed.

The restart checkpoint stores a broker-deal cursor as:

```json
{
  "time_msc": 1700000000123,
  "ticket": 9101
}
```

Using `(time_msc, ticket)` avoids ambiguity when multiple deals have the same millisecond timestamp.

## Main fail-closed incidents

The gate can reject startup for conditions including:

- `LOCAL_STATE_CORRUPT`
- `LOCAL_STATE_MISSING_WITH_OPEN_POSITIONS`
- `LOCAL_LIVE_STATE_HALTED`
- `RECONCILE_CHECKPOINT_CORRUPT`
- `ACCOUNT_IDENTITY_CHANGED`
- `SYMBOL_CHANGED`
- `MAGIC_CHANGED`
- `POSITION_COMMENT_UNMANAGED`
- `POSITION_STRATEGY_NOT_IN_BUNDLE`
- `POSITION_DEAL_HISTORY_MISSING`
- `UNSUPPORTED_DEAL_ENTRY_MODE`
- `POSITION_STRATEGY_MISMATCH`
- `POSITION_SIDE_MISMATCH`
- `POSITION_VOLUME_MISMATCH`
- `BROKER_POSITION_WITHOUT_LOCAL_ENTRY`
- `LOCAL_ENTRY_MISMATCH`
- `POSITION_DISAPPEARED_WITHOUT_EXIT_DEAL`
- `DEAL_HISTORY_OPEN_WITHOUT_POSITION`

A CRITICAL result does **not** flatten positions. The operator must inspect MT5, broker deal history, local logs and the last known backup/checkpoint before deciding what to do.

## Manual health check

Read-only reconciliation without writing a new checkpoint:

```powershell
python -m src.restart_reconcile `
  --mode health `
  --config config.yaml `
  --reconcile-config reconcile.yaml `
  --no-checkpoint
```

The detailed result is also written atomically to:

```text
runtime/restart_reconcile.json
```

Exit code `0` means the broker/local state is internally consistent under the configured policy. Exit code `3` means the gate is CRITICAL.

## Verified restart

The normal verification mode writes a fresh checkpoint only when reconciliation passes:

```powershell
python -m src.restart_reconcile `
  --mode verify `
  --config config.yaml `
  --reconcile-config reconcile.yaml
```

A failed reconciliation never replaces the prior checkpoint.

## Crash-after-order protection

One important failure mode is:

```text
broker accepts order
      ↓
position exists at MT5
      ↓
Python/Windows crashes before local ENTRY event is written
```

When `require_local_entry: true`, Phase 14 detects this as `BROKER_POSITION_WITHOUT_LOCAL_ENTRY` and refuses to restart live trading automatically. This prevents the system from silently assuming local bookkeeping is complete after an uncertain order submission.

## Position disappearance protection

The checkpoint records the positions that existed at the last successful restart reconciliation.

If a previously checkpointed position is gone on the next restart, Phase 14 requires a later broker exit deal to explain the disappearance. Otherwise startup fails with `POSITION_DISAPPEARED_WITHOUT_EXIT_DEAL`.

This allows normal SL/TP/manual close activity to be explained from broker history while still rejecting unexplained local/broker divergence.

## Windows supervisor integration

`deploy/windows/run-live-supervisor.ps1` now runs the Phase 14 reconciliation gate before every production process start/restart.

The gate is enabled by default. A temporary migration bypass requires an explicit environment setting:

```powershell
setx FOREX_REQUIRE_RESTART_RECONCILE "0"
```

Re-enable it by removing the override or setting:

```powershell
setx FOREX_REQUIRE_RESTART_RECONCILE "1"
```

Optional configuration override:

```powershell
setx FOREX_RECONCILE_CONFIG "reconcile.yaml"
```

Recommended production operation is to leave Phase 14 enabled.

## Recovery workflow after CRITICAL

```text
Do not restart live repeatedly
      ↓
Open MT5 and inspect managed positions
      ↓
Inspect runtime/restart_reconcile.json
      ↓
Inspect live_state.json and live_events.csv(.1-.N)
      ↓
Inspect broker deal history
      ↓
Confirm account login / symbol / magic / strategy mapping
      ↓
Restore state only if a verified backup is actually required
      ↓
Run Phase 14 health manually
      ↓
Run Phase 14 verify to create a new checkpoint
      ↓
Only then restart the live supervisor
```

## Limitations

- The default implementation uses the broker position ticket as the position identifier when the broker adapter does not expose a separate stable position identifier.
- A managed position older than the configured deal-history lookback fails closed and needs operator review.
- Phase 14 validates consistency; it does not decide whether an existing position is economically desirable.
- It does not automatically repair local state or broker state.
- It does not guarantee profitability or eliminate broker/API execution risk.
