# Phase 15 — Broker-Accurate Position Sizing & Margin Safety

Phase 15 hardens the final path immediately before a real MT5 order is submitted. It does not change strategy selection or research logic.

## Safety model

A new live order is allowed only when all of the following are true:

1. The connected MT5 login is in `live.allowed_account_logins` when account pinning is required.
2. Bid/ask values are positive and not crossed.
3. Entry, stop-loss and take-profit normalize to valid broker tick-size prices.
4. Stop-loss and take-profit remain on the correct side of entry after normalization.
5. Protective distances satisfy the broker's advertised stops/freeze constraints.
6. MT5 `order_calc_profit` can calculate the account-currency loss for one lot from entry to stop.
7. The risk-sized volume remains above broker minimum and below the configured per-order cap.
8. The order remains below the portfolio total-lot cap.
9. MT5 `order_calc_margin` succeeds.
10. Projected margin/free-margin ratios remain inside configured caps.
11. Existing `order_check` and all prior live/production gates still pass.

Any failure rejects the new order. Phase 15 never increases volume to satisfy a broker minimum.

## Account allowlist

In `config.yaml`:

```yaml
live:
  enabled: false
  allowed_account_logins:
    - 12345678
  require_account_allowlist: true
```

When `live.enabled: true`, the default policy requires at least one allowed login. Use the exact numeric MT5 account login shown by the terminal/broker. Do not commit account credentials or passwords.

If the connected account changes, preflight fails. The production supervisor also emits `ACCOUNT_LOGIN_NOT_ALLOWED` as a CRITICAL incident before starting the live engine.

## Broker-native risk sizing

Historical backtests still use model assumptions. Real live entries use MT5's account-aware calculation:

```text
risk budget = account equity × live risk fraction × strategy weight
loss per lot = abs(order_calc_profit(1 lot, entry → stop))
lots = risk budget / loss per lot
```

The result is rounded down to the broker's volume step and capped by `max_lot_per_order`.

This is preferable to assuming a fixed pip value because the broker calculation reflects the symbol contract and account-currency conversion path exposed by MT5.

## Margin gate

Example:

```yaml
live:
  max_margin_fraction_of_equity: 0.25
  min_free_margin_fraction_after_order: 0.50
```

For each proposed order:

```text
required margin = order_calc_margin(...)
projected margin = current margin + required margin
projected free margin = current free margin - required margin
```

The order is rejected if projected margin exceeds the configured equity fraction, projected free margin is non-positive, or projected free margin falls below the configured equity fraction.

These limits are intentionally conservative and are independent of the Phase 9 portfolio stress test.

## Price / stop validation

Phase 15 reads these MT5 symbol fields:

```text
trade_tick_size
trade_stops_level
trade_freeze_level
```

Entry/SL/TP are normalized to `trade_tick_size`. The protective distance uses the more conservative of stops/freeze levels. This can reject some orders that the broker might otherwise accept; the intent is to fail closed rather than rely on broker-specific modification semantics.

## Rollout procedure

1. Keep `live.enabled: false`.
2. Add only the intended MT5 login(s) to `allowed_account_logins`.
3. Run `python -m src.main --mode live-preflight` and verify `account_login_allowed = PASS`.
4. Confirm the symbol specification and broker stops/freeze levels in a demo account.
5. Run paper-forward and production health as before.
6. Enable live only after Phase 11–14 gates are also healthy.
7. Start with the existing tiny lot/risk caps.
8. Review `live_events.csv` for `broker_risk_or_protection`, `broker_margin_calc_failed`, and `margin_gate` rejections before changing any thresholds.

## Important limitations

- `order_calc_profit` and `order_calc_margin` are broker/terminal calculations; a successful calculation does not guarantee fill price or execution quality.
- Margin can change between pre-check and actual execution.
- Stops/freeze semantics differ by broker and symbol. Phase 15 deliberately uses a conservative interpretation.
- This safety layer reduces operational sizing/margin mistakes; it does not make a trading strategy profitable.
