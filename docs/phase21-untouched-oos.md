# Phase 21 — True Untouched OOS Research Integrity

Phase 21 removes final-OOS leakage from portfolio construction.

## Two-stage protocol

1. **PRE-OOS FREEZE**
   - parameter selection uses Train + Validation only;
   - walk-forward, parameter stability and Monte Carlo qualification use pre-OOS data only;
   - candidate inclusion uses the pre-OOS verdict only;
   - strategy return correlations and diversification weights use pre-OOS curves only;
   - selected strategies, parameters and weights are serialized into a deterministic SHA-256 design fingerprint.
2. **OOS EVALUATION**
   - only after the fingerprint is frozen is the final OOS slice evaluated;
   - OOS return, Sharpe, drawdown, profit factor and portfolio Monte Carlo are evaluation outputs only;
   - OOS results cannot add/remove candidates, change parameters or change weights.

## Structural guarantee

`qualify_strategy_pre_oos()` accepts only Train and Validation frames. It has no OOS argument. `research_portfolio()` computes the frozen design before it runs any OOS backtest.

The regression test mutates only final-OOS OHLC values and requires the following to remain identical:

- candidate inclusion;
- selected parameters;
- active strategy set;
- portfolio weights;
- frozen-design SHA-256 fingerprint.

This does not make a strategy profitable and does not prevent every form of research overfitting. It specifically prevents final OOS values from influencing the Phase 5 portfolio construction decision.
