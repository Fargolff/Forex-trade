# Phase 34 — Independent Remote Watcher & Alert Escalation

Phase 34 adds a **read-only observer** for the signed Phase 32/33 remote audit scope. It is designed to run on a machine that is operationally separate from the Trading PC and does not need MT5, broker credentials, a release private key, a deployment-approval private key, or the `runtime_boot` private key.

The watcher verifies the existing signed evidence rather than asking the Trading PC whether it is healthy.

## Trust boundary

The observer needs only:

- this source tree (or another trusted copy of the verifier code),
- `release/signing_key_trust.json` plus the referenced public keys,
- read access to the Phase 32/33 replica root,
- optional alert-webhook credentials local to the observer.

`observer_id` must differ from the monitored Trading PC `machine_id`. This is a logical safety check, not cryptographic proof that the processes run on physically different hosts.

The watcher never sends, retries, cancels, repairs, resizes, or flattens broker orders/positions and cannot arm live trading.

## What every check verifies

`src.remote_watcher` calls the full Phase 33 verifier with `require_current_boot=True`. Therefore the check includes:

1. Phase 32 remote audit-ledger integrity,
2. Phase 33 checkpoint signatures,
3. checkpoint sequence and previous-hash continuity,
4. boot-receipt/audit-entry anchors,
5. current remote audit boot coverage,
6. head consistency.

After that cryptographic verification, the observer applies independent operational rules.

### CRITICAL conditions

- signed ledger verification failure,
- missing liveness after the watcher had previously observed the machine,
- remote scope becoming unavailable,
- Phase 33 `HALTED` / `ERROR` / critical status,
- heartbeat older than `critical_after_seconds`,
- materially future-dated heartbeat,
- remote sequence moving backwards relative to the observer's remembered head,
- the same previously observed sequence presenting a different checkpoint hash,
- corrupt/mismatched local watcher state.

### WARNING conditions

- heartbeat older than `warning_after_seconds`,
- Phase 33 `DEGRADED` / warning status.

A recovery notification is emitted once when the current check becomes healthy after an active incident.

## Why the observer keeps local state

Phase 33 can prove the current remote chain is internally consistent. It cannot, by itself, prove that a privileged storage administrator did not delete a previously visible tail and replace the remote storage with an older valid snapshot.

Phase 34 therefore remembers the highest observed liveness sequence and its checkpoint SHA-256 **outside the monitored replica root**. A later lower sequence raises `WATCHER_LEDGER_REWIND`; the same sequence with a different hash raises `WATCHER_LEDGER_MUTATED`.

The state file is intentionally fail-closed: malformed state is **not** silently reset because resetting it would erase the anti-rewind memory.

For stronger durability, protect the watcher host/state with normal host ACLs, backup/snapshot policy, and independent monitoring.

## Configuration

Copy the example:

```text
remote_watcher.example.yaml -> remote_watcher.yaml
```

Set at minimum:

```yaml
observer_id: watcher-pc-01
environment_id: prod-bkk-01
machine_id: trader-pc-01
replica_root: "Z:/forex-audit"
```

The observer state and alert outbox must not be located inside `replica_root`.

Recommended starting freshness thresholds are:

```yaml
warning_after_seconds: 120
critical_after_seconds: 300
```

Choose thresholds that are comfortably above the production supervisor poll interval and expected remote-storage latency.

## One-shot check

Run on the observer machine:

```bash
python -m src.remote_watcher --root . --config remote_watcher.yaml
```

Exit codes:

- `0` — healthy or recovered,
- `1` — warning,
- `2` — critical.

The Windows runner can be scheduled once per minute using the included task helper.

## Alert delivery

Every emitted incident/recovery is written to the local JSONL outbox first. Configure an optional observer-only webhook with:

```text
FOREX_WATCHER_ALERT_WEBHOOK_URL=https://...
```

The webhook secret should be configured on the observer, not copied from or to the Trading PC. A webhook failure does not discard the local outbox event; the JSON result reports `webhook_delivered: false` so the observer/service manager can also alarm on delivery failure.

Repeated identical incidents are suppressed until their configured repeat interval. A new incident or a severity escalation alerts immediately.

## Windows scheduling

Run once from an elevated or appropriately privileged shell on the observer:

```powershell
.\deploy\windows\install-remote-watcher-task.ps1 `
  -ProjectRoot C:\Forex-trade `
  -ConfigPath remote_watcher.yaml
```

The task invokes `deploy/windows/run-remote-watcher.ps1` every minute. For unattended operation, configure Task Scheduler with a dedicated service account and ensure that account can read the remote replica and trust-store public keys.

## Important limitations

Phase 34 is an **observer and alerting control**, not a remote trading control plane. It deliberately does not issue a shutdown/flatten command to the Trading PC. Automatic remote kill actions would create another privileged execution channel and require a separate authentication, authorization, replay-protection, and failure-mode design.

A local watcher state file also cannot protect against compromise of the watcher host itself. Storage-side WORM/Object Lock/immutable snapshots remain recommended for the remote audit ledger.
