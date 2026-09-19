# Phase 24 — Broker Holiday & Early-Close Calendar Safety

Phase 24 adds a broker-specific UTC market calendar on top of the Phase 19 weekly-session guard and Phase 20 conservative session calibration.

The calendar is **restriction-only**. It may close trading for a full day, delay a day's open, close a day early, or add a temporary closure interval. It can never expand trading beyond the weekly/calibrated session envelope.

## Default file and environment overrides

The live market-clock guard looks for:

```text
market_calendar.yaml
```

If the file does not exist, the Phase 19/20 weekly behavior is unchanged.

Use another path with:

```text
FOREX_MARKET_CALENDAR_PATH=D:\Forex\market_calendar.yaml
```

To fail closed when the calendar file is missing, set:

```text
FOREX_REQUIRE_MARKET_CALENDAR=1
```

A malformed calendar always raises an error rather than being silently ignored.

## File format

Start from `market_calendar.example.yaml`.

```yaml
version: 1

days:
  "2026-12-24":
    close_utc: "18:00"
    reason: "Christmas Eve early close"

  "2026-12-25":
    closed: true
    reason: "Christmas Day"

  "2026-12-28":
    open_utc: "23:00"
    reason: "Delayed broker reopen"

closures:
  - start_utc: "2026-12-31T20:00:00Z"
    end_utc: "2027-01-01T23:00:00Z"
    reason: "New Year broker closure"
```

### `days`

Each key is a UTC date.

A full-day closure uses:

```yaml
closed: true
```

A restricted session may provide `open_utc`, `close_utc`, or both. Missing `open_utc` means `00:00`. Missing `close_utc` means `24:00`.

Examples:

```yaml
# Early close
"2026-12-24":
  close_utc: "18:00"

# Delayed open
"2026-12-28":
  open_utc: "23:00"
```

The allowed window must satisfy `open_utc < close_utc`. Overnight per-day windows are intentionally unsupported; use a temporary closure interval when the broker schedule crosses dates in a non-standard way.

### `closures`

Temporary closures are half-open UTC intervals:

```text
start_utc <= now < end_utc
```

Timestamps must include a timezone. UTC (`Z` / `+00:00`) is recommended.

## Restriction-only precedence

The final live market state is the intersection of:

```text
Phase 19 static weekly session
        ∩
Phase 20 calibrated narrower session
        ∩
Phase 24 calendar restriction
```

Therefore a calendar entry cannot make Saturday tradable, cannot open Sunday earlier than the weekly policy, and cannot keep Friday open after the weekly close.

Calendar restrictions may only make the final state more conservative:

- `OPEN` → `TRANSITION`
- `OPEN` → `CLOSED`
- `TRANSITION` → `CLOSED`

They never turn a weekly `CLOSED` state into `OPEN`.

## Transition grace

The existing `market_transition_grace_seconds` also applies to calendar open/close boundaries. This blocks new orders near early-close and delayed-open boundaries instead of switching from fully tradable to closed at an exact second.

Full-day closures remain `CLOSED` for the whole UTC date.

## Stale-data behavior

When the calendar makes the market `CLOSED` or `TRANSITION`, stale tick/bar alerts are suppressed exactly like expected weekend closure. Other production checks remain active, including:

- terminal/account checks
- position integrity
- pending-intent reconciliation
- future-timestamp checks
- persistent broker/host clock-offset watchdog

A holiday calendar does not weaken those controls.

## Validation CLI

Validate and summarize a calendar:

```bash
python -m src.market_calendar --path market_calendar.yaml
```

Inspect the calendar-only state at a UTC timestamp:

```bash
python -m src.market_calendar \
  --path market_calendar.yaml \
  --at 2026-12-24T17:45:00Z
```

This CLI validates the calendar structure only. The final live state still intersects the calendar with the Phase 19/20 weekly session.

## Paper-forward behavior

MT5 paper-forward mode does not manufacture bars while the broker is closed, so it naturally remains idle during a holiday when no completed bars arrive. Phase 24 primarily hardens the live liveness/trading gate; it does not replay synthetic holiday bars or create paper orders by itself.

## Deployment and integrity

The calendar changes whether the system is allowed to open new risk, so treat it as deployment-critical configuration:

1. review it against the broker's published holiday schedule;
2. keep all times in UTC;
3. test upcoming exceptions with the CLI;
4. include the production calendar in the signed deployment manifest/bundle if your release process manages config files;
5. include it in the verified backup set;
6. consider `FOREX_REQUIRE_MARKET_CALENDAR=1` on the production machine once the file is deliberately deployed.

Do not guess recurring future holidays. Broker holiday hours differ by instrument/account and may change each year.

## Failure policy

The system fails closed rather than guessing when:

- the required calendar is missing;
- YAML is malformed;
- the schema/version is unsupported;
- a day window has `open_utc >= close_utc`;
- a closure timestamp lacks timezone information;
- a closure has `end_utc <= start_utc`.

Live trading remains disabled by default and still requires all existing arming, account, risk, margin, reconciliation, release, and operational gates.
