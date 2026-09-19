# Phase 22 — Forward-Only Paper Warm Start

Phase 22 prevents a fresh MT5 paper-trading state from replaying the historical completed bars fetched for indicator context.

## Problem fixed

Before Phase 22, `paper-mt5-once` and `paper-mt5-daemon` passed the entire fetched history directly to `PaperTradingEngine.process()`. A fresh state has no `last_bar_time`, so every fetched historical bar was considered new and could create historical `SIGNAL`, `ENTRY` and `EXIT` events.

## New first-run behavior

For MT5 paper modes only:

1. Fetch completed history as before.
2. If the paper state has no cursor, call `warm_start()` instead of `process()`.
3. Validate OHLC/index shape.
4. Set `last_bar_time` to the newest completed bar.
5. Persist the state atomically.
6. Emit one audit-only `WARM_START` event.
7. Do not calculate or queue historical paper execution events.

On the next poll, the full history can still be used by the strategy indicators, but only bars strictly newer than the persisted cursor are processed by the execution engine.

## Execution timing after warm start

A signal produced by the first genuinely new completed bar is queued normally. It may execute no earlier than the following completed bar open. Historical signals that existed before the warm-start cursor are deliberately ignored because the forward-paper process was not running when they occurred.

## Restart behavior

Once `last_bar_time` exists, restart behavior is unchanged: already-seen bars are skipped and only bars newer than the persisted cursor are processed.

## Existing pre-Phase-22 paper states

Phase 22 does not rewrite or reset an existing paper state automatically. If an old state already contains historical replay trades, review/export it first and start a new paper state file if you want a clean forward-only experiment. Automatic balance/position-history repair would be unsafe and is intentionally not attempted.

## Scope

`paper-demo` remains a historical simulation tool and still processes supplied synthetic history from the beginning. The forward-only warm-start rule applies only to MT5 paper-forward modes. Paper mode still has no broker order path.

## Regression coverage

`tests/test_phase22_paper_warm_start.py` verifies history-only seeding, no historical execution events, idempotent restart, next-bar-open timing and fail-closed handling of unexpected execution state.
