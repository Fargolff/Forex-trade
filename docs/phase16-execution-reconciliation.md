# Phase 16 — Order Execution & Partial-Fill Reconciliation

Phase 16 hardens the boundary between local strategy intent and broker execution. The core rule is: **after an order may have reached MT5, never guess and never auto-resend.**

## Execution receipt states

Broker submissions are normalized into three successful transport outcomes:

- `FILLED` — MT5 returned `TRADE_RETCODE_DONE`. The engine still verifies filled volume against the requested volume.
- `PARTIAL` — MT5 returned `TRADE_RETCODE_DONE_PARTIAL`. Trading halts because exposure is now different from the requested exposure.
- `PLACED` — MT5 returned `TRADE_RETCODE_PLACED`. The request was accepted, but the engine does not assume a completed market fill.

Explicit broker rejection raises `BrokerOrderRejected`. An `order_send()` result of `None` raises `BrokerSubmissionAmbiguous`, because absence of a local response does not prove the broker did not receive the request.

## Persistent order intent

Before every live Entry, opposite-signal Close, or emergency Flatten, the engine writes a `pending_order_intent` into `runtime/live_state.json` using the existing atomic state-store pattern.

The intent records:

- action (`ENTRY`, `CLOSE`, or `FLATTEN`)
- strategy
- symbol
- side
- requested lots
- stop loss / take profit where applicable
- position ticket for closes
- bar timestamp

The intent is cleared only after a full broker-confirmed fill and after the durable audit event has been written.

This ordering is deliberate. If the process crashes after the broker receives the order but before the local ENTRY/CLOSE event is written, the pending intent remains on disk. On restart the live engine refuses to submit another order and raises `PENDING_ORDER_INTENT` as a CRITICAL condition.

## Fail-closed conditions

New live trading halts for:

- `PARTIAL_FILL`
- `ORDER_ACCEPTED_UNCONFIRMED`
- `ORDER_SUBMISSION_AMBIGUOUS`
- `FILL_VOLUME_MISMATCH`
- `ORDER_STATUS_UNKNOWN`
- `CLOSE_SUBMISSION_AMBIGUOUS`
- `CLOSE_EXECUTION_EXCEPTION`
- `EMERGENCY_CLOSE_AMBIGUOUS`
- `EMERGENCY_CLOSE_EXCEPTION`
- unresolved `PENDING_ORDER_INTENT`

No automatic resize, repair, flatten retry, or order resend is performed for these ambiguous states.

## Explicit rejection vs ambiguity

A broker rejection is different from uncertainty:

- `order_check()` reject: request is considered not submitted; the pending intent may be cleared.
- `order_send()` explicit non-success retcode: broker explicitly rejected it; the pending intent may be cleared.
- `order_send()` returns `None`: outcome is uncertain; the pending intent remains and trading halts.
- partial/placed result: broker exposure may exist or still be changing; the pending intent remains and trading halts.

## Operator recovery procedure

When `PENDING_ORDER_INTENT` or another execution ambiguity appears:

1. Do **not** restart with an automatic resend and do not manually clear the state first.
2. Inspect MT5 open positions for the configured symbol, magic number, strategy comment, side, and volume.
3. Inspect broker deal/order history around the intent timestamp.
4. Compare broker state with `runtime/live_events.csv`, `runtime/live_incidents.csv`, `runtime/restart_reconcile.json`, and the intent stored in `runtime/live_state.json`.
5. Resolve any partial or unexpected exposure manually at the broker if necessary.
6. Only after broker state is understood and consistent should the pending intent be cleared and the operational halt be reset using the normal operator-controlled recovery procedure.

Phase 14 restart reconciliation remains an additional gate before supervised restart; Phase 16 does not weaken or bypass it.

## Safety notes

Phase 16 improves idempotency and execution-state integrity. It does not guarantee fills, remove broker slippage/requotes, or make a strategy profitable. The safest behavior after uncertainty is intentionally to stop and require reconciliation rather than send a second order.
