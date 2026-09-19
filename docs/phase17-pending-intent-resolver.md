# Phase 17 — Deterministic Pending-Intent Resolver

Phase 17 upgrades the Phase 16 fail-closed order journal into a deterministic restart resolver. The resolver is intentionally conservative: it may clear a pending intent only when broker evidence uniquely proves what happened.

## Evidence required

Every new live intent now records an `intent_id`, UTC `created_at`, the exact `(time_msc, ticket)` broker-deal cursor observed before submission, and a stable position identifier for close/flatten operations. Legacy Phase 16 intents that lack this metadata are never guessed; they remain a manual-reconciliation case.

For an ENTRY, automatic resolution requires all of the following: post-cursor opening deal evidence for the same strategy and side, exactly one broker order/position group whose aggregate filled volume equals the requested lots, exactly one matching current managed position, matching side/strategy/volume, and matching protective stop/take-profit levels.

For CLOSE or FLATTEN, automatic resolution requires post-cursor exit deal evidence for the exact target position, exactly one broker order, aggregate exit volume equal to the requested lots, and proof that the target position no longer exists.

## Recovery behavior

When `python -m src.restart_reconcile --mode verify` proves an intent exactly, it writes one idempotent recovered audit event, clears `pending_order_intent`, advances the full `(time_msc, ticket)` deal cursor, and may clear the halted state only when the halt reason is exclusively an execution-ambiguity code from Phase 16/17. Risk kills, account-identity failures, operational integrity failures, and other unrelated halts are never auto-cleared.

`--mode health` stays read-only. A resolvable pending intent is reported but not mutated.

## Cases that remain fail-closed

- legacy intent without Phase 17 metadata
- partial volume or protection mismatch
- more than one broker order can satisfy the intent
- target close position is still open
- missing broker deal evidence
- broker deals with unsupported/contradictory geometry
- unrelated halt reason remains active

There is still no automatic order resend, position repair, resize, or flatten during restart reconciliation.
