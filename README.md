# Forex Auto Trader Research Framework

Personal automated-Forex research and execution framework with reproducible research, cost-aware backtests, robust validation, portfolio controls, paper-forward testing, guarded MT5 live execution, production supervision, an independent watchdog, disaster-recovery tooling, cryptographically signed releases, deterministic signed deployment bundles and verified off-device backup retention.

## Safety defaults

- Live trading is **disabled by default**.
- Paper mode never calls `order_send`.
- Real-order modes require both `live.enabled: true` and the exact operator arming phrase.
- Guarded live supports MT5 hedging accounts only so strategy positions remain separable.
- Live execution uses broker-reported tick size/value and volume constraints.
- Per-order lots, total lots, open-position count and spread are capped.
- Bar-close signals execute on the next bar open in research/paper models.
- Live mode evaluates only the newest completed bar; missed historical bars are never replayed into broker orders.
- Stale market data, position-integrity incidents, broker disconnects, account-risk breaches and production anomalies fail closed.
- Phase 9 adds a persistent production halt that survives process/Windows restarts.
- Phase 10 watchdog/recovery tools have **no broker-order path**.
- Phase 11 can require an Ed25519-signed release before the Windows live supervisor starts or restarts.
- Phase 12 can require a separately signed deterministic release bundle pinned to an expected commit and release ID.
- Phase 13 never prunes a local backup unless a byte-identical verified replica is still reachable when pruning is attempted.
- Phase 14 blocks live restarts when broker/local position history cannot be reconciled.
- Phase 15 requires broker-native live risk/margin calculations and can pin execution to explicit MT5 account logins.
- Research `PASS`, portfolio OOS, paper results, signed artifacts, backup health or clean operational checks do not guarantee profitability.

## Architecture

```text
Strategy Factory
      ↓
Cost-aware Backtest
      ↓
Robust Validation
      ↓
Portfolio Research
      ↓
MT5 Paper Forward
      ↓
Guarded Live Engine
      ↓
Broker Reconciliation + Runtime Health
      ↓
Signed Release Bundle Verification
      ↓
Signed Release Verification
      ↓
Production Supervisor
      ↓
External Watchdog + Disaster Recovery
      ↓
Verified Backup + Off-device Replication
```

Key files:

```text
Forex-trade/
├─ config.example.yaml
├─ production.example.yaml
├─ watchdog.example.yaml
├─ backup.example.yaml
├─ requirements.txt
├─ docs/
│  ├─ phase9-production.md
│  ├─ phase10-watchdog-recovery.md
│  ├─ phase11-signed-release.md
│  ├─ phase12-release-bundle.md
│  └─ phase13-backup-retention.md
├─ deploy/windows/
│  ├─ run-live-supervisor.ps1
│  ├─ install-scheduled-task.ps1
│  ├─ run-watchdog.ps1
│  ├─ install-watchdog-task.ps1
│  ├─ run-backup-cycle.ps1
│  └─ install-backup-task.ps1
├─ src/
│  ├─ artifact.py
│  ├─ backup_policy.py
│  ├─ backtest.py
│  ├─ config.py
│  ├─ data.py
│  ├─ live.py
│  ├─ main.py
│  ├─ mt5_broker.py
│  ├─ ops.py
│  ├─ paper.py
│  ├─ portfolio.py
│  ├─ production.py
│  ├─ recovery.py
│  ├─ release.py
│  ├─ research.py
│  ├─ risk.py
│  ├─ soak.py
│  ├─ strategy.py
│  ├─ validation.py
│  └─ watchdog.py
└─ tests/
   └─ ...
```

## Phase status

### Phase 1 — Core Backtest/Risk Engine ✅

- Configurable spread, slippage and commission
- ATR stop/take-profit exits
- Risk-based sizing
- Daily-loss and max-drawdown kill switches
- Conservative stop/TP ambiguity handling
- Gap-stop handling
- Bar-close signal → next-bar-open execution
- End-of-sample realization
- Timeframe-aware Sharpe approximation

### Phase 2 — Data Engine ✅

- UTC-normalized OHLC validation
- Sorting/de-duplication
- CSV.GZ local cache
- Incremental upsert/merge
- OHLC resampling
- MT5 cache command

### Phase 3 — Strategy Factory + Batch Research ✅

13 testable hypotheses across trend, breakout, momentum and mean-reversion families. Every strategy emits:

```text
signal                 -1 / 0 / +1
stop_distance          price distance
take_profit_distance   price distance
```

### Phase 4 — Robust Validation ✅

- Chronological Train / Validation / final OOS split
- Phase 21 structurally prevents final OOS from entering portfolio candidate/parameter/weight selection
- Bounded parameter-neighborhood selection
- Rolling walk-forward evaluation
- Parameter-stability checks
- Trade-path bootstrap / Monte Carlo
- Sharpe decay monitoring
- `PASS` / `WATCH` / `REJECT` research gate

### Phase 5 — Portfolio Research ✅

- Strategy correlation analysis
- Correlation-penalized inverse-volatility allocation
- Maximum strategy-weight cap
- Risk contributions and diversification metrics
- Portfolio candidates, selected parameters and weights are frozen from pre-OOS data before final OOS is evaluated
- SHA-256 frozen-design fingerprint makes the pre-OOS decision auditable/reproducible
- Portfolio OOS metrics and block-bootstrap Monte Carlo are evaluation-only and cannot change the frozen design
- CSV exports for weights, candidates, frozen design, correlation and OOS equity

### Phase 6 — MT5 Paper Trading ✅

- Persistent atomic JSON state
- Restart-safe/idempotent completed-bar processing
- Loads Phase 5 portfolio bundle
- Spread/slippage/commission paper fills
- Gap stop/take-profit handling
- Mark-to-market equity
- Daily-loss / drawdown halt
- CSV audit trail
- `paper-demo`, `paper-mt5-once`, `paper-mt5-daemon`

### Phase 23 — Broker Swap / Financing Cost Realism ✅

- Optional account-currency cash-per-lot rollover assumptions for backtest and paper
- Separate long/short financing rates
- UTC rollover hour plus configurable Monday-Sunday multipliers
- Default Wednesday triple-swap schedule
- Backtest `total_financing` and per-trade financing audit
- Persistent/idempotent Paper `FINANCING` events and cumulative financing
- Legacy paper states start financing forward from the last processed bar; no silent retro-charge
- Live does not synthesize financing or double-charge broker-booked swap
- Live health exposes raw MT5 swap terms (`swap_long`, `swap_short`, `swap_mode`, `swap_rollover3days`)

See `docs/phase23-financing.md` before calibrating the assumptions to a broker.

### Phase 7 — Guarded Small Live Deployment ✅

- Broker-native tick/value/volume sizing
- `order_check()` before real orders
- Filling-mode fallback
- Hedging-account requirement
- Strategy tagging via magic + `fat:<strategy>`
- Per-order, total-lot, position-count and spread limits
- Daily-loss / max-drawdown halt
- Managed emergency flatten
- Persistent live state and audit log
- Explicit double live gate
- `live-preflight`, `live-mt5-once`, `live-mt5-daemon`, `live-flatten`

### Phase 8 — Production Hardening & Reconciliation ✅

- Read-only `live-health`
- Terminal connected/trade-permission checks
- Stale tick and completed-bar rejection
- Duplicate/unknown managed-position detection
- Missing SL/TP detection
- Fail-closed operational halt without guessing which ambiguous positions to flatten
- Atomic `runtime/live_heartbeat.json`
- Incident audit log
- Broker deal-history reconciliation
- Broker profit / commission / swap captured in audit events
- Persistent deal cursor
- Bounded reconnect logic

### Phase 9 — Observability & Deployment ✅

- Separate `src.production` supervisor around guarded live
- Read-only production health mode
- Environment-only webhook alerts; no webhook secret in YAML
- Local JSONL alert outbox + cooldown de-duplication
- Persistent `runtime/production_halt.json`
- Dynamic spread baseline and z-score anomaly gate
- Live slippage reconciliation and hard-cap/statistical anomaly monitoring
- Broker-clock-ahead guard
- Managed-stop account margin stress test
- Runtime log rotation
- Windows live-supervisor / Task Scheduler templates
- Production runbook and machine/secrets hardening checklist

### Phase 10 — External Watchdog & Disaster Recovery ✅

- Independent `src.watchdog` process with **no MT5/order dependency**
- Local/UNC/shared-file heartbeat source
- Optional remote heartbeat JSON source through environment variable
- Optional bearer auth through environment variable
- Watchdog exit codes: `0=OK`, `2=WARN`, `3=CRITICAL`
- Detects missing/stale heartbeat, future clock, remote HALT/CRITICAL and critical embedded incidents
- Separate watchdog alert outbox + cooldown de-duplication
- Optional external webhook delivery from environment-only secret
- SHA-256 backup manifest for selected runtime/config/portfolio state
- Verify-before-restore
- Preview restore by default
- Explicit acknowledgement required for in-place restore
- Deployment-integrity SHA-256 manifest and verification
- Deterministic soak harness for outage/degraded/clock-jump/recovery paths
- Independent Windows watchdog Task Scheduler templates
- CI runs both pytest and an operational soak scenario

See `docs/phase10-watchdog-recovery.md` for the disaster-recovery procedure.

### Phase 11 — Cryptographically Signed Release Gate ✅

- Ed25519 release key generation/signing/verification tooling
- Detached signed deployment manifest
- Public-key SHA-256 fingerprint reporting
- Signature verification before deployment hash verification
- Detects manifest tampering, wrong public key and deployment code drift
- Private signing key excluded from Git and intended to remain off the trading machine
- Generated private keys use restrictive permissions on non-Windows platforms
- Opt-in Windows supervisor gate via `FOREX_REQUIRE_SIGNED_RELEASE=1`
- Verification repeats before every supervisor start/restart
- Existing live arming and production-risk gates remain mandatory
- Dedicated Phase 11 tamper/wrong-key tests

See `docs/phase11-signed-release.md` for the signing and key-rotation runbook.

### Phase 12 — Deterministic Signed Release Bundle & Provenance ✅

- Deterministic ZIP packaging of the exact Phase 11 verified deployment tree
- Detached Ed25519 signature over the complete bundle bytes
- Bundle metadata includes source commit and release ID inside the signed ZIP
- External trusted public key remains the trust anchor; embedded key is cross-checked only
- Strict member allowlist, duplicate-member rejection and ZIP-slip/path-traversal rejection
- Anti-rollback pinning using expected source commit + expected release ID
- Bundle manifest/signature can be bound to the deployed Phase 11 manifest/signature
- Verify-before-extract preview workflow; no automatic in-place installer
- Opt-in Windows supervisor gate via `FOREX_REQUIRE_SIGNED_BUNDLE=1`
- Phase 12 supervisor gate requires Phase 11 gate to remain enabled
- Security tests cover deterministic builds, tampering, hidden files, wrong keys, rollback pins, deployed binding and unsafe archive paths

See `docs/phase12-release-bundle.md` for the artifact signing, verification and rollback runbook.

### Phase 13 — Backup Retention & Off-device Replication ✅

- Atomic backup catalog with archive SHA-256 and replica metadata
- Immediate verification after every managed backup creation
- Filesystem-based off-device replication to external drive, UNC/network share or approved sync folder
- Temporary-copy → atomic rename for replica publication
- Replica archive verification plus byte-identical SHA-256 comparison
- Conservative latest/daily/weekly/monthly retention buckets
- Local pruning blocked unless a verified byte-identical replica is reachable
- Explicit acknowledgement required before destructive retention deletion
- Non-production restore drill with post-restore size/SHA-256 verification
- Atomic restore-drill report
- Windows daily backup Task Scheduler template
- Scheduled retention deletion remains opt-in
- No cloud/provider credentials are stored in the repository

See `docs/phase13-backup-retention.md` for the backup, retention, replication and restore-drill runbook.


### Phase 14 — Broker State Disaster Recovery & Restart Reconciliation ✅

- Read-only restart gate compares broker positions/deals with local live state and audit events
- Atomic restart report/checkpoint and account/symbol/magic continuity checks
- Same-millisecond broker deal ordering uses `(time_msc, ticket)`
- Ambiguous restart state fails closed and never auto-repairs broker positions

See `docs/phase14-restart-reconciliation.md` for the restart/recovery runbook.

### Phase 15 — Broker-Accurate Position Sizing & Margin Safety ✅

- Live risk sizing uses MT5 `order_calc_profit` for the actual symbol/account currency path
- No tick-value approximation is used for new live position sizing
- MT5 `order_calc_margin` gates every new order before `order_send`
- Account-login allowlist is mandatory whenever `live.enabled=true` by default
- Projected total-margin and free-margin fractions are capped before entry
- Broker tick-size price normalization is applied to entry/SL/TP prices
- Broker stops/freeze levels are enforced conservatively before order submission
- Invalid/non-positive/crossed bid/ask ticks fail closed
- Production health emits `ACCOUNT_LOGIN_NOT_ALLOWED` as CRITICAL

See `docs/phase15-broker-safety.md` for account pinning, margin policy and broker-specific rollout steps.

## Quick start

```bash
# already at repository root
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy production.example.yaml production.yaml
copy watchdog.example.yaml watchdog.yaml
copy backup.example.yaml backup.yaml
copy reconcile.example.yaml reconcile.yaml
```

List strategies:

```bash
python -m src.main --mode list-strategies
```

Research / validation examples:

```bash
python -m src.main --mode demo-backtest --strategy ema_trend --bars 5000
python -m src.main --mode validate-mt5 --strategies all --bars 30000 --mc-runs 2000
python -m src.main --mode portfolio-mt5 --strategies all --bars 40000 --mc-runs 2000
```

## Paper trading

```bash
python -m src.main --mode paper-mt5-once
python -m src.main --mode paper-mt5-daemon
```

Paper mode uses MT5 market data only and does not expose order execution. On a fresh MT5 paper state, the first poll is a forward-only warm start: historical completed bars seed the cursor/indicator context but cannot create historical paper trades. `paper-demo` retains historical-simulation behavior. See `docs/phase22-forward-only-paper.md`.

## Guarded live and operations

Read-only checks:

```bash
python -m src.main --mode live-preflight
python -m src.main --mode live-health
python -m src.production --mode health
```

Real-order modes remain explicitly armed:

```bash
python -m src.main --mode live-mt5-once \
  --arm-live I_UNDERSTAND_LIVE_TRADING
```

Production-supervised bounded live example:

```bash
python -m src.production \
  --mode supervised-live \
  --max-cycles 3 \
  --poll-seconds 30 \
  --arm-live I_UNDERSTAND_LIVE_TRADING
```

A Phase 9 production-critical incident creates a persistent halt. Review broker state and runtime logs before clearing it.

## Phase 10 watchdog

One independent heartbeat check:

```bash
python -m src.watchdog --mode once
```

Daemon:

```bash
python -m src.watchdog --mode daemon
```

For meaningful protection against total trading-PC failure, run the watchdog on a second machine and point it to a shared/remote heartbeat source.

Environment-only remote source/alert variables:

```text
FOREX_HEARTBEAT_URL
FOREX_HEARTBEAT_BEARER
FOREX_WATCHDOG_WEBHOOK_URL
FOREX_WATCHDOG_WEBHOOK_BEARER
```

## Backup / restore / integrity

Create and verify a runtime backup:

```bash
python -m src.recovery --mode backup
python -m src.recovery --mode verify-backup --archive backups/runtime-YYYYMMDD-HHMMSS.zip
```

Restore into a preview directory first:

```bash
python -m src.recovery --mode restore-preview \
  --archive backups/runtime-YYYYMMDD-HHMMSS.zip \
  --target runtime/restore-preview
```

In-place restore is separately gated:

```bash
python -m src.recovery --mode restore-in-place \
  --archive backups/runtime-YYYYMMDD-HHMMSS.zip \
  --ack I_UNDERSTAND_RUNTIME_RESTORE
```

Deployment drift check:

```bash
python -m src.recovery --mode make-manifest
python -m src.recovery --mode verify-manifest
```

## Phase 13 managed backup lifecycle

Set an off-device root through the environment, then run a verified cycle:

```bash
python -m src.backup_policy --mode cycle --root . --config backup.yaml
```

Review retention without deleting anything:

```bash
python -m src.backup_policy --mode retention-plan --config backup.yaml
python -m src.backup_policy --mode prune --config backup.yaml
```

Run a non-production restore drill:

```bash
python -m src.backup_policy --mode restore-drill --config backup.yaml
```

A scheduled Windows backup refuses to run if `FOREX_BACKUP_REPLICA_ROOT` is missing. Destructive retention remains disabled unless the operator explicitly enables it and supplies the prune acknowledgement.

## Signed release verification

Create a deployment manifest, sign it on the trusted signing machine, then verify it on the trading machine:

```bash
python -m src.recovery --mode make-manifest --manifest release/release_manifest.json
python -m src.release --mode sign --manifest release/release_manifest.json --signature release/release_signature.json --private-key <OFFLINE_PRIVATE_KEY_PATH>
python -m src.release --mode verify --root . --manifest release/release_manifest.json --signature release/release_signature.json --public-key release/forex-release-public.pem
```

After key setup and a successful manual verification, enable the Windows supervisor gate with `FOREX_REQUIRE_SIGNED_RELEASE=1`. The private key should not be present on the trading machine.

## Phase 12 signed release bundle

Build the deterministic bundle from an already verified Phase 11 tree:

```bash
python -m src.artifact --mode build \
  --root . \
  --manifest release/release_manifest.json \
  --release-signature release/release_signature.json \
  --public-key release/forex-release-public.pem \
  --archive release/forex-release-bundle.zip \
  --source-commit <GIT_COMMIT_SHA> \
  --release-id <UNIQUE_RELEASE_ID>
```

Sign the complete ZIP on the protected/offline signing machine:

```bash
python -m src.artifact --mode sign \
  --archive release/forex-release-bundle.zip \
  --bundle-signature release/forex-release-bundle.signature.json \
  --private-key <OFFLINE_PRIVATE_KEY_PATH>
```

Verify with the externally trusted public key and anti-rollback pins:

```bash
python -m src.artifact --mode verify \
  --archive release/forex-release-bundle.zip \
  --bundle-signature release/forex-release-bundle.signature.json \
  --public-key release/forex-release-public.pem \
  --expected-commit <GIT_COMMIT_SHA> \
  --expected-release-id <UNIQUE_RELEASE_ID>
```

After manual verification, Phase 12 can be enabled with `FOREX_REQUIRE_SIGNED_BUNDLE=1`. It requires Phase 11's gate plus `FOREX_EXPECTED_RELEASE_COMMIT` and `FOREX_EXPECTED_RELEASE_ID`.

## Operational soak

```bash
python -m src.soak --cycles 2000
```

The harness verifies heartbeat-timeout detection, recovery, degraded WARN semantics, clock-jump detection and post-incident recovery. It does not replace a real multi-day broker/network soak.

## Recommended rollout

```text
Research hypothesis
      ↓
Robust validation
      ↓
Portfolio calibration / OOS
      ↓
Long MT5 paper-forward run
      ↓
Live preflight + operational health
      ↓
Production health + baseline collection
      ↓
Independent watchdog running
      ↓
Verified local backup + off-device replica
      ↓
Restore drill
      ↓
Create deployment manifest
      ↓
Sign manifest with protected/offline key
      ↓
Verify Phase 11 release
      ↓
Build deterministic release bundle with commit + release ID
      ↓
Sign complete bundle with protected/offline key
      ↓
Verify bundle + anti-rollback pins + deployed binding
      ↓
One explicitly armed tiny live cycle
      ↓
Bounded supervised daemon
      ↓
Multi-day broker-specific soak
      ↓
Only then consider longer unattended operation
```

## Remaining maturity work

- Hardware-backed/HSM release signing and protected key rotation workflow
- Hosted heartbeat transport with authenticated publishing/acknowledgement
- Historical broker swap-rate ingestion, holiday exceptions and broker-specific forecast calibration
- Broker holiday/weekend calendar semantics
- Restore-state reconciliation against every broker-side edge case
- Multi-day broker-specific soak across reconnects, weekend closes and DST/time changes

## Important

This project is a research and execution framework, not a guarantee of profit. Leveraged FX/CFD trading can lose money quickly. Spread, slippage, commission, swap, gaps, broker execution, leverage, model error, operational failure and regime change can materially alter live results versus research and paper trading.

## Phase 16 — Order Execution & Partial-Fill Reconciliation

Live order submission now uses normalized execution receipts (`FILLED`, `PARTIAL`, `PLACED`) and explicit rejection vs ambiguous-submission exceptions. A persistent `pending_order_intent` is written to `live_state.json` before every entry/close/flatten submission and is cleared only after a fully confirmed fill and durable audit event. Partial fills, accepted-but-unconfirmed orders, missing submission results, fill-volume mismatches, and unexpected execution exceptions halt new trading without automatic resend or auto-repair. A leftover intent after restart is itself a CRITICAL fail-closed condition and requires broker-state reconciliation before manual clearance.

## Phase 17 — Deterministic Pending-Intent Resolver

<!-- PHASE17_DETERMINISTIC_PENDING_INTENT_RESOLVER -->
Phase 17 turns the Phase 16 pending-order journal into a deterministic restart resolver. New intents persist a unique intent ID, submission timestamp, stable position identifier, and full `(time_msc, ticket)` pre-submit broker-deal cursor. Restart verification may reconstruct a missing local audit event and clear an execution-only halt only when MT5 deals plus current position state prove the exact full entry/exit. Legacy, partial, conflicting, or otherwise ambiguous evidence remains fail-closed with no automatic resend or repair. See `docs/phase17-pending-intent-resolver.md`.

## Phase 18 — Working-Order & Recovery-State Reconciliation

`PHASE18_WORKING_ORDER_RECOVERY`

Phase 18 tracks MT5 active/history orders by exact broker order ticket, persists receipt evidence before interpreting `PLACED`, prevents resends while an order is still working, detects orphan managed orders, and can auto-clear only a broker-proven terminal **no-fill** outcome. Partial/mismatched/missing evidence remains fail-closed. See `docs/phase18-working-order-recovery.md`.


## Phase 19 market-session and clock safety

Guarded live now distinguishes `OPEN`, `CLOSED`, and `TRANSITION` FX weekly-session states. Weekend/boundary closure suppresses stale tick/bar alerts but blocks new order creation; position integrity, pending-intent reconciliation, terminal health, and future-timestamp checks continue to run. `live-health` and heartbeat output include the market state, next transition, and broker-data clock offsets.

See `docs/phase19-market-clock.md` before changing the broker-specific UTC session boundaries.


## Phase 20 — Broker session calibration

Phase 20 persists conservative broker timing evidence in `runtime/session_calibration.json`. After enough independent weekly samples, the effective Sunday open may move later and the effective Friday close may move earlier, but calibration can never expand beyond the static Phase 19 envelope. A rolling moving-tick offset watchdog halts new trading on persistent broker/host time disagreement. See `docs/phase20-session-calibration.md`.
