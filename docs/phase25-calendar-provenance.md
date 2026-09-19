# Phase 25 — Market Calendar Provenance & Freshness Gate

Phase 25 makes the Phase 24 broker holiday calendar deployment-verifiable instead of trusting any local `market_calendar.yaml` that happens to exist on the trading machine.

The calendar remains external deployment data. It is **not** required to be committed to the repository. Instead, its SHA-256 and freshness metadata are bound into the already signed Phase 11 release manifest.

## Calendar provenance metadata

Production calendars should add two top-level fields:

```yaml
version: 1
generated_at: "2026-09-20T00:00:00Z"
valid_through: "2027-01-31"
```

`generated_at` is when the operator/calendar source prepared the reviewed schedule. It must include timezone information.

`valid_through` means the broker holiday/session schedule has been reviewed through that UTC date, including ordinary dates with no exception. It is **not** simply the date of the last listed holiday.

## Signed external binding

After creating the normal deployment manifest, bind the calendar **before signing the manifest**:

```bash
python -m src.recovery \
  --mode make-manifest \
  --root . \
  --manifest release/release_manifest.json

python -m src.calendar_provenance \
  --mode bind \
  --path market_calendar.yaml \
  --manifest release/release_manifest.json

python -m src.release \
  --mode sign \
  --manifest release/release_manifest.json \
  --signature release/release_signature.json \
  --private-key /secure/offline/forex-release-private.pem
```

The bind step writes a signed-manifest section like:

```json
{
  "external_bindings": {
    "market_calendar": {
      "version": 1,
      "calendar_schema_version": 1,
      "sha256": "...",
      "generated_at": "2026-09-20T00:00:00+00:00",
      "valid_through": "2027-01-31"
    }
  }
}
```

The private key remains offline. The trading PC only needs the signed manifest, signature, trusted public key, and matching external calendar file.

## Why the calendar stays outside the release bundle

Broker holiday schedules can change independently of application source code. Phase 25 therefore treats the calendar as a signed external binding rather than a source-controlled program file.

This has two effects:

1. the calendar may live at an operator-controlled deployment path;
2. any content change requires a new signed release manifest/signature before supervised live can start.

A local hot-edit, even a one-byte/comment change, changes SHA-256 and fails the gate.

## Freshness rules

The provenance verifier reports remaining coverage as:

```text
valid_through - current UTC date
```

Default minimum coverage is 30 days.

Possible freshness failures include:

- `CALENDAR_EXPIRED` — `valid_through` is already in the past;
- `CALENDAR_COVERAGE_INSUFFICIENT` — coverage remains, but is below the configured minimum;
- `CALENDAR_GENERATED_IN_FUTURE` — `generated_at` is more than five minutes ahead of the host UTC clock.

Inspect a calendar without checking a signed manifest:

```bash
python -m src.calendar_provenance \
  --mode inspect \
  --path market_calendar.yaml \
  --min-coverage-days 30
```

For deterministic testing, add `--at <ISO-8601 timestamp>`.

## Provenance verification

Verify the deployed file against a signed-manifest binding:

```bash
python -m src.calendar_provenance \
  --mode verify \
  --path market_calendar.yaml \
  --manifest release/release_manifest.json \
  --min-coverage-days 30 \
  --required
```

The verifier fails closed when:

- a required calendar is missing;
- a signed binding exists but the deployed calendar is missing;
- a calendar exists but the signed manifest has no calendar binding;
- SHA-256 differs;
- schema version or provenance metadata differs from the signed binding;
- the Phase 24 calendar itself is malformed;
- freshness checks fail.

If the calendar is optional, absent, and the signed manifest has no calendar binding, the result is `CALENDAR_NOT_CONFIGURED` and the gate is neutral.

## Windows supervisor integration

`deploy/windows/run-live-supervisor.ps1` now runs the provenance/freshness gate on every supervised-live start/restart.

When `FOREX_REQUIRE_SIGNED_RELEASE=1`, Phase 25 provenance verification is enabled by default.

Environment variables:

```text
FOREX_MARKET_CALENDAR_PATH=market_calendar.yaml
FOREX_REQUIRE_MARKET_CALENDAR=1
FOREX_REQUIRE_MARKET_CALENDAR_PROVENANCE=1
FOREX_MARKET_CALENDAR_MIN_COVERAGE_DAYS=30
```

Rules:

- `FOREX_REQUIRE_MARKET_CALENDAR_PROVENANCE=1` requires `FOREX_REQUIRE_SIGNED_RELEASE=1` because the signed release is the trust anchor.
- `FOREX_REQUIRE_MARKET_CALENDAR=1` makes a missing file fail closed.
- a calendar bound into the signed manifest must exist even when `FOREX_REQUIRE_MARKET_CALENDAR` is not set.
- `FOREX_MARKET_CALENDAR_MIN_COVERAGE_DAYS` must be a non-negative integer.

Supervisor gate order is now:

```text
Signed Bundle
→ Signed Release
→ Calendar Provenance/Freshness
→ Restart Reconciliation
→ supervised live process
```

## Updating the production calendar

Do not edit the production calendar in place and continue trading under the old signature.

Use this flow instead:

1. obtain/review the updated broker schedule;
2. update `generated_at` and `valid_through`;
3. validate the Phase 24 structure;
4. regenerate or reuse the deployment manifest as appropriate;
5. bind the new calendar SHA-256 into the manifest;
6. sign the manifest offline;
7. deploy the calendar + manifest + signature together;
8. verify before restarting supervised live.

This turns calendar changes into auditable release changes instead of invisible local configuration edits.

## Scope and limitations

Phase 25 proves that the deployed calendar matches the calendar approved when the release manifest was signed and that its declared review horizon is still current. It does **not** prove that the broker's published holiday schedule itself is correct, and it does not discover future holidays automatically.

Live trading remains disabled by default and still requires all existing arming, account, margin, position, recovery, release, and operational gates. No control in this phase guarantees profitability or execution quality.
