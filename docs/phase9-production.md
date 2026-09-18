# Phase 9 — Production Observability & Deployment Runbook

Phase 9 adds a supervisor around the guarded Phase 7/8 live engine. It does not relax any existing live-trading gate.

## Safety model

Real orders still require both:

1. `live.enabled: true` in `config.yaml`
2. the exact operator arming phrase

The Phase 9 supervisor adds an independent persistent halt file. A production-critical incident stops supervised execution and writes `runtime/production_halt.json`. Restarting Windows or the Python process does not clear this file.

Clear it only after reviewing broker state, positions, `live_incidents.csv`, `live_events.csv`, alerts, and the reason inside the halt file:

```powershell
python -m src.production --mode clear-halt
```

## Files

```text
production.example.yaml
runtime/alerts.jsonl
runtime/alert_state.json
runtime/execution_metrics.json
runtime/production_halt.json
runtime/live_events.csv
runtime/live_incidents.csv
runtime/live_heartbeat.json
```

## Read-only production health

```powershell
python -m src.production --mode health
```

This checks the existing Phase 8 operational report plus:

- broker tick clock being unexpectedly ahead of local UTC
- dynamic spread baseline z-score
- stressed account equity/free margin at managed stop-losses plus one additional risk budget
- current production halt state

Health mode does not arm live execution.

## Alerts

No webhook URL is stored in YAML. Configure a webhook only through the environment variable named by `alert_webhook_env` (default: `FOREX_ALERT_WEBHOOK_URL`).

Example for the current Windows user:

```powershell
[Environment]::SetEnvironmentVariable(
  "FOREX_ALERT_WEBHOOK_URL",
  "https://your-alert-endpoint.example/hook",
  "User"
)
```

Every alert is also written to the local JSONL outbox. Repeated identical alerts are cooldown-deduplicated.

Do not commit webhook URLs, broker passwords, MT5 credentials, API tokens, or account secrets to this repository.

## Dynamic execution baselines

`runtime/execution_metrics.json` stores online statistics for:

- spread in pips
- adverse live slippage in pips when an `ENTRY` event can be matched with the reconciled broker order/deal

The z-score gates do not activate until the configured minimum sample count is reached. A hard observed-slippage cap remains available independently of the statistical baseline.

These metrics are monitoring signals, not evidence of strategy profitability.

## Margin stress

The supervisor estimates cash loss from every managed position's current executable price to its stop-loss using broker tick size/value and volume. It then adds one extra configured risk budget and checks stressed:

- equity > 0
- free margin > 0
- free-margin fraction
- margin level

A failure creates a production-critical halt before the next live cycle is passed to the live engine.

## Log rotation

Runtime CSV/JSONL logs are rotated before they exceed the configured size. Generations use:

```text
live_events.csv.1
live_events.csv.2
...
```

Manual rotation check:

```powershell
python -m src.production --mode rotate-logs
```

## Supervised live command

Use bounded cycles first:

```powershell
python -m src.production --mode supervised-live `
  --max-cycles 3 `
  --poll-seconds 30 `
  --arm-live I_UNDERSTAND_LIVE_TRADING
```

If Phase 9 reports `WARN`, the supervisor skips that trading cycle. If it reports `CRITICAL`, it persists a production halt and does not pass the cycle to the live engine.

## Windows supervision

Templates are under `deploy/windows/`:

- `run-live-supervisor.ps1`
- `install-scheduled-task.ps1`

The wrapper does not store the arming phrase. If unattended operation is intentionally approved, the wrapper reads it from `FOREX_LIVE_ARM_PHRASE`.

The scheduled-task installer does not change `live.enabled` and does not set either environment variable.

## Recovery sequence after an incident

1. Stop the scheduled task / supervisor process.
2. Open MT5 and verify actual positions and account equity manually.
3. Review `runtime/production_halt.json`.
4. Review `runtime/live_incidents.csv` and `runtime/live_events.csv`.
5. Run `python -m src.production --mode health` while live remains disabled if possible.
6. Fix the underlying issue; do not merely increase a threshold to make the warning disappear.
7. Clear the production halt explicitly.
8. Run one bounded supervised cycle before returning to a longer daemon.

## Machine hardening checklist

- use a dedicated Windows user for the trader
- enable full-disk encryption and Windows updates
- restrict remote desktop / remote administration access
- do not store broker credentials or webhook URLs in Git
- keep the MT5 terminal and Python environment pinned and documented
- back up research artifacts and configuration, but do not back up secrets into the repo
- keep `live.enabled: false` in templates and development machines
- review Windows Task Scheduler history after unexpected restarts
- treat a missing heartbeat, repeated reconnects, clock anomaly, or production halt as an operator-review event

Phase 9 improves observability and operational containment; it does not make leveraged FX/CFD trading safe or guarantee positive returns.
