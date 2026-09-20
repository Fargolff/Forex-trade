# Phase 19 — Market Session-Aware Liveness & Clock Safety

Phase 19 prevents normal FX weekend closure from being misclassified as a broken market-data feed while preserving fail-closed behavior when the market is expected to be open.

## Weekly session policy

The guarded-live runtime now has a configurable UTC weekly session:

- Sunday open, default `22:00` UTC
- Friday close, default `22:00` UTC
- transition grace, default 3600 seconds

The transition grace is symmetric around the configured weekly open/close boundary. This intentionally blocks new entries near the boundary, which both absorbs common one-hour DST/broker-session shifts and reduces weekend-gap execution risk.

Possible market states are:

- `OPEN` — freshness checks are enforced and new entries may be evaluated.
- `CLOSED` — stale tick/bar incidents are suppressed and no new order is submitted.
- `TRANSITION` — stale tick/bar incidents are suppressed and no new order is submitted.

Position-integrity, terminal-health and pending-intent checks continue to run in every state.

## Clock/timestamp safety

MT5 Python does not expose a reliable independent server-clock primitive for this runtime. Phase 19 therefore uses broker tick timestamps and the completed-bar timestamp as broker-data clock evidence.

A tick timestamp ahead of host UTC by more than `max_future_tick_seconds` raises `BROKER_TICK_IN_FUTURE` as CRITICAL. A completed-bar timestamp ahead of host UTC by more than `max_future_bar_seconds` raises `COMPLETED_BAR_IN_FUTURE` as CRITICAL. These checks remain active even when the weekly market is closed.

A host clock that is too far ahead of a valid broker feed still manifests as stale market data while the market is OPEN; Phase 19 does not guess whether that condition is local-clock drift or feed latency.

## Configuration

```yaml
live:
  market_session_enabled: true
  market_sunday_open_utc: "22:00"
  market_friday_close_utc: "22:00"
  market_transition_grace_seconds: 3600
  max_future_tick_seconds: 5
  max_future_bar_seconds: 300
```

Broker session calendars differ. Adjust the weekly UTC boundary only after checking the actual symbol session at the broker. The transition window is a safety buffer, not a guarantee that a venue follows the default schedule.

## Operational behavior

`live-health` and the runtime heartbeat now expose market state, reason, next weekly transition, and signed broker-data clock offsets. A positive clock offset means the broker timestamp is ahead of host UTC.

Phase 19 does not add automatic time synchronization, order resend, or position repair. Fix the OS clock or broker/session configuration explicitly if a clock-integrity incident occurs.
