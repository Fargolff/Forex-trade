from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .strategy import available_strategies, build_signals, strategy_spec


RESULT_COLUMNS = [
    "strategy",
    "family",
    "status",
    "return_pct",
    "max_drawdown_pct",
    "sharpe_approx",
    "trades",
    "win_rate",
    "profit_factor",
    "net_profit",
    "error",
]


def run_strategy_batch(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    names: Iterable[str] | None = None,
) -> pd.DataFrame:
    selected = list(names) if names is not None else available_strategies()
    rows: list[dict[str, object]] = []

    for name in selected:
        try:
            spec = strategy_spec(name)
            signals = build_signals(df, name)
            result = run_backtest(signals, backtest_cfg)
            rows.append(
                {
                    "strategy": name,
                    "family": spec.family,
                    "status": "ok",
                    "return_pct": result["return_pct"],
                    "max_drawdown_pct": result["max_drawdown_pct"],
                    "sharpe_approx": result["sharpe_approx"],
                    "trades": result["trades"],
                    "win_rate": result["win_rate"],
                    "profit_factor": result["profit_factor"],
                    "net_profit": result["net_profit"],
                    "error": "",
                }
            )
        except Exception as exc:  # A failed hypothesis should not abort the whole research batch.
            rows.append(
                {
                    "strategy": name,
                    "family": strategy_spec(name).family if name in available_strategies() else "unknown",
                    "status": "error",
                    "return_pct": float("nan"),
                    "max_drawdown_pct": float("nan"),
                    "sharpe_approx": float("nan"),
                    "trades": 0,
                    "win_rate": float("nan"),
                    "profit_factor": float("nan"),
                    "net_profit": float("nan"),
                    "error": str(exc),
                }
            )

    frame = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    if frame.empty:
        return frame

    # Ranking is deliberately simple in Phase 3. Phase 4 will replace this with
    # out-of-sample robustness scoring rather than selecting the prettiest backtest.
    frame["rank_score"] = (
        frame["sharpe_approx"].fillna(-999.0)
        - 2.0 * frame["max_drawdown_pct"].fillna(1.0)
    )
    return frame.sort_values(["status", "rank_score"], ascending=[False, False]).reset_index(drop=True)


def save_research_results(results: pd.DataFrame, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(target, index=False)
    return target
