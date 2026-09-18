# Phase 20 — Broker Session Auto-Calibration & Time-Sync Watchdog

Phase 20 learns conservative weekly-session evidence from broker tick timestamps while keeping the Phase 19 static session as a hard safety envelope.

## Safety model

Calibration may only **narrow** the configured session. It can move the effective Sunday open later or the effective Friday close earlier. It can never open trading earlier or keep trading later than the static Phase 19 boundary.

A boundary is not used until `session_calibration_min_samples` independent weekly observations exist. A safety buffer is then added inside the observed market window.

The default static envelope remains:

- Sunday open: `22:00 UTC`
- Friday close: `22:00 UTC`

## Persistent time-offset watchdog

Every new broker tick observed while the static session is OPEN contributes one signed sample:

`broker tick UTC - host UTC`

A rolling median is used. If enough moving-tick samples exist and the median absolute offset exceeds the configured threshold, live operation fails closed with `PERSISTENT_BROKER_TIME_OFFSET`.

This signal deliberately does not claim to distinguish OS clock drift from sustained broker/feed latency. Either condition makes deterministic timing unsafe and requires operator investigation.

Immediate future-timestamp checks from Phase 19 remain active independently.

## State

Calibration evidence is stored atomically in:

`runtime/session_calibration.json`

The file contains only timing evidence. It contains no credentials or trading secrets.

## Configuration

```yaml
live:
  session_calibration_enabled: true
  session_calibration_state_path: runtime/session_calibration.json
  session_calibration_min_samples: 3
  session_calibration_safety_buffer_minutes: 15
  session_calibration_max_narrowing_minutes: 180
  session_calibration_retention_weeks: 12
  clock_watchdog_window_size: 12
  clock_watchdog_min_samples: 5
  max_persistent_clock_offset_seconds: 60
```

Do not reduce the static Phase 19 safety envelope merely because calibration observes a wider broker session. Wider evidence is intentionally ignored.
