# Phase 23 — Broker Swap / Financing Cost Realism

Phase 23 adds explicit overnight-financing realism to research and paper trading while avoiding synthetic double charging in live trading.

## Research / paper model

`financing:` in `config.yaml` defines synthetic **account-currency cash per lot per 1x rollover**. Positive values are credits; negative values are costs. The default is disabled, so existing results do not change unless the operator opts in.

The schedule uses a UTC rollover hour plus seven Monday-through-Sunday multipliers. The default `[1, 1, 3, 1, 1, 0, 0]` models a Wednesday triple-swap convention. A charge occurs only when a position was already open across the boundary: `start < rollover <= end`.

Backtests include financing in account equity/net profit and expose `total_financing`. Each trade also records the financing accrued while it was open. Paper mode books financing to persistent balance, emits a `FINANCING` audit event, persists cumulative financing, and remains idempotent across restarts.

## Upgrade behavior for existing paper state

A pre-Phase-23 paper position has no financing cursor. On load, Phase 23 anchors that position to the state's existing `last_bar_time`, not its historical entry time. This deliberately avoids silently retro-charging many days of swap when upgrading an old paper experiment. Financing starts forward from the last already-processed bar.

## Live behavior: observe, do not synthesize

Live accounts already receive broker-booked swap/financing. Phase 23 therefore does **not** deduct a synthetic charge from live equity. `MT5Broker.symbol_spec()` captures raw `swap_long`, `swap_short`, `swap_mode`, and `swap_rollover3days`, and live-health exposes those raw terms for comparison/audit.

Do not copy `swap_long` or `swap_short` directly into the research cash assumptions unless you have confirmed the broker's `swap_mode`, instrument contract, account currency, and conversion rules. MT5 supports multiple swap calculation modes and the raw number is not universally account-currency cash per lot.

## Limitations

- Financing assumptions are static over the test period; broker rates can change historically.
- Holiday and exceptional rollover calendars are not yet modeled.
- Live reconciliation continues to use broker deal-history `swap` as the realized source of truth.
- This improves cost realism but does not guarantee research/live equivalence or profitability.
