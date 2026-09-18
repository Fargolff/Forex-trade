# Phase 18 — Working-Order & Recovery-State Reconciliation

`PHASE18_WORKING_ORDER_RECOVERY`

Phase 18 closes the gap between an MT5 submission being accepted and a broker deal becoming available.

## Broker evidence

The MT5 adapter now exposes current working orders and historical orders. Every record carries the exact order ticket, side, initial/remaining volume, setup/done timestamps, symbol, magic, protection prices, comment and normalized broker state.

When `order_send` returns an order ticket, live state persists that ticket and the normalized receipt before interpreting `FILLED`, `PARTIAL` or `PLACED`. A crash after receipt persistence therefore leaves deterministic evidence for restart reconciliation.

## Recovery rules

Recovery never guesses an order identity. Phase 18 only applies order-state automation when a persisted exact broker order ticket exists.

- **Working order:** matching `STARTED`, `PLACED` or request state remains fail-closed with `PENDING_INTENT_ORDER_WORKING`. The intent is kept and the order is never resent.
- **Partial evidence:** partial state, reduced remaining volume, terminal order with linked deals, or mismatched identity remains CRITICAL and requires reconciliation.
- **Full fill:** Phase 17 deal/position proof still controls filled recovery, and the proven deal order must equal the persisted broker order ticket.
- **Terminal no-fill:** `CANCELED`, `EXPIRED` or `REJECTED` can clear an intent only when there are zero linked deals and current position state proves no execution effect. A dedicated idempotent `RECOVERY_NO_FILL` audit row is written.
- **Missing order evidence:** if a persisted broker order ticket is absent from both active and historical order snapshots, recovery remains fail-closed.

## Orphan order guard

Restart reconciliation treats any managed working order that is not the exact ticket of the current pending intent as `UNTRACKED_WORKING_ORDER` CRITICAL. This prevents a restarted process from trading while an old broker order can still execute later.

## Non-goals

Phase 18 does not cancel orders, resend orders, repair positions, resize exposure, or flatten automatically. `order_send=None` still has no deterministic order ticket and remains a manual-reconciliation case.
