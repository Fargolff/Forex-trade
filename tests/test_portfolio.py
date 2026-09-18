import numpy as np
import pandas as pd

from src.portfolio import diversification_weights, portfolio_metrics, portfolio_monte_carlo, risk_contributions


def sample_returns(rows: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(123)
    idx = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    common = rng.normal(0.00001, 0.0004, rows)
    return pd.DataFrame(
        {
            "trend": common + rng.normal(0, 0.00025, rows),
            "breakout": 0.4 * common + rng.normal(0.000005, 0.00045, rows),
            "reversion": -0.2 * common + rng.normal(0.000003, 0.00035, rows),
        },
        index=idx,
    )


def test_diversification_weights_sum_to_one_and_respect_cap():
    returns = sample_returns()
    weights = diversification_weights(returns, max_weight=0.45)

    assert np.isclose(weights.sum(), 1.0)
    assert float(weights.max()) <= 0.45 + 1e-12
    assert set(weights.index) == set(returns.columns)


def test_risk_contributions_and_portfolio_metrics_are_finite():
    returns = sample_returns()
    weights = diversification_weights(returns, max_weight=0.45)
    risk = risk_contributions(returns, weights)
    metrics = portfolio_metrics(returns, weights, periods_per_year=252 * 24)

    assert np.isfinite(risk.to_numpy()).all()
    assert np.isclose(risk.sum(), 1.0)
    assert np.isfinite(metrics["sharpe"])
    assert metrics["max_drawdown_pct"] >= 0.0
    assert metrics["effective_strategies"] >= 1.0


def test_portfolio_monte_carlo_is_reproducible_for_same_seed():
    returns = sample_returns()["trend"]
    a = portfolio_monte_carlo(returns, 10_000.0, runs=200, block_size=12, seed=99)
    b = portfolio_monte_carlo(returns, 10_000.0, runs=200, block_size=12, seed=99)

    assert a == b
    assert 0.0 <= a["loss_probability"] <= 1.0
    assert 0.0 <= a["ruin_probability"] <= 1.0
