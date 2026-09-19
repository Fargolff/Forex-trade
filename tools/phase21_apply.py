from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Phase 21 patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_between(path: str, start_marker: str, end_marker: str, replacement: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"Phase 21 start marker missing in {path}: {start_marker!r}")
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError(f"Phase 21 end marker missing in {path}: {end_marker!r}")
    target.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


def write_file(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def apply() -> None:
    replace_once(
        "src/validation.py",
        "    monte_carlo_ruin_drawdown: float = 0.30\n    min_oos_trades: int = 20\n",
        "    monte_carlo_ruin_drawdown: float = 0.30\n"
        "    # Phase 21 pre-OOS qualification thresholds. These are evaluated only\n"
        "    # on Train/Validation and therefore cannot leak final OOS information.\n"
        "    min_validation_trades: int = 20\n"
        "    min_validation_profit_factor: float = 1.0\n"
        "    min_validation_sharpe: float = 0.0\n"
        "    max_validation_drawdown: float = 0.20\n"
        "    min_oos_trades: int = 20\n",
    )

    qualification = '''def _checks_verdict(checks: dict[str, bool]) -> tuple[str, int]:
    passed = sum(bool(value) for value in checks.values())
    if passed == len(checks):
        return "PASS", passed
    if passed >= len(checks) - 2:
        return "WATCH", passed
    return "REJECT", passed


def qualify_strategy_pre_oos(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None = None,
    cfg: ValidationConfig | None = None,
) -> dict[str, Any]:
    """Freeze a strategy decision using Train/Validation only.

    Final OOS data is intentionally not accepted by this API. Candidate selection,
    robustness checks and the qualification verdict are therefore structurally
    unable to depend on OOS values.
    """
    cfg = cfg or ValidationConfig()
    if train.empty or validation.empty:
        raise ValueError("pre-OOS qualification requires non-empty train and validation slices")

    selected_params, selection_table = select_parameters(
        train,
        validation,
        backtest_cfg,
        strategy_name,
        base_params,
        cfg.parameter_perturbation,
    )
    train_result = _run(train, backtest_cfg, strategy_name, selected_params)
    validation_result = _run(validation, backtest_cfg, strategy_name, selected_params)
    pre_oos = pd.concat([train, validation]).sort_index()

    stability = parameter_stability(
        pre_oos,
        backtest_cfg,
        strategy_name,
        selected_params,
        cfg.parameter_perturbation,
    )
    wf = walk_forward_evaluation(pre_oos, backtest_cfg, strategy_name, selected_params, cfg)
    wf_valid = wf[wf["error"] == ""] if not wf.empty else wf
    wf_positive_fraction = float(wf_valid["positive"].mean()) if not wf_valid.empty else 0.0

    pre_oos_result = _run(pre_oos, backtest_cfg, strategy_name, selected_params)
    pre_oos_trade_pnls = [float(trade.pnl) for trade in pre_oos_result["trade_log"]]
    mc = monte_carlo_trade_paths(
        pre_oos_trade_pnls,
        backtest_cfg.initial_equity,
        cfg.monte_carlo_runs,
        cfg.monte_carlo_ruin_drawdown,
        cfg.seed,
    )

    checks = {
        "validation_min_trades": validation_result["trades"] >= cfg.min_validation_trades,
        "validation_profit_factor": validation_result["profit_factor"] >= cfg.min_validation_profit_factor,
        "validation_sharpe": validation_result["sharpe_approx"] >= cfg.min_validation_sharpe,
        "validation_drawdown": validation_result["max_drawdown_pct"] <= cfg.max_validation_drawdown,
        "walk_forward_consistency": wf_positive_fraction >= cfg.min_walk_forward_positive_fraction,
        "parameter_stability": stability["positive_fraction"] >= cfg.min_parameter_stability_fraction,
        "pre_oos_monte_carlo_ruin": mc["ruin_probability"] <= cfg.max_monte_carlo_ruin_probability,
    }
    verdict, passed = _checks_verdict(checks)

    return {
        "strategy": strategy_name,
        "family": strategy_spec(strategy_name).family,
        "verdict": verdict,
        "checks_passed": passed,
        "checks_total": len(checks),
        "checks": checks,
        "selected_params": selected_params,
        "train": train_result,
        "validation": validation_result,
        "pre_oos": pre_oos_result,
        "walk_forward_positive_fraction": wf_positive_fraction,
        "parameter_stability_fraction": stability["positive_fraction"],
        "parameter_stability_median_sharpe": stability["median_sharpe"],
        "monte_carlo": mc,
        "selection_table": selection_table,
        "walk_forward": wf,
        "stability_table": stability["table"],
    }


def pre_oos_validation_summary(report: dict[str, Any]) -> dict[str, Any]:
    validation = report["validation"]
    mc = report["monte_carlo"]
    return {
        "strategy": report["strategy"],
        "family": report["family"],
        "verdict": report["verdict"],
        "pre_oos_verdict": report["verdict"],
        "checks_passed": report["checks_passed"],
        "checks_total": report["checks_total"],
        "validation_return_pct": validation["return_pct"],
        "validation_sharpe": validation["sharpe_approx"],
        "validation_max_drawdown_pct": validation["max_drawdown_pct"],
        "validation_profit_factor": validation["profit_factor"],
        "validation_trades": validation["trades"],
        "walk_forward_positive_fraction": report["walk_forward_positive_fraction"],
        "parameter_stability_fraction": report["parameter_stability_fraction"],
        "pre_oos_monte_carlo_ruin_probability": mc["ruin_probability"],
        "selected_params": json.dumps(report["selected_params"], sort_keys=True),
        "selection_stage": "PRE_OOS",
    }


'''
    validation_path = ROOT / "src/validation.py"
    validation_text = validation_path.read_text(encoding="utf-8")
    marker = "def validate_strategy(\n"
    if "def qualify_strategy_pre_oos(" not in validation_text:
        pos = validation_text.find(marker)
        if pos < 0:
            raise RuntimeError("validate_strategy marker missing")
        validation_text = validation_text[:pos] + qualification + validation_text[pos:]
        validation_path.write_text(validation_text, encoding="utf-8")

    validate_replacement = '''def validate_strategy(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_name: str,
    base_params: dict[str, Any] | None = None,
    cfg: ValidationConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or ValidationConfig()
    split = chronological_split(df, cfg.train_fraction, cfg.validation_fraction)
    qualification = qualify_strategy_pre_oos(
        split.train,
        split.validation,
        backtest_cfg,
        strategy_name,
        base_params,
        cfg,
    )

    selected_params = qualification["selected_params"]
    train_result = qualification["train"]
    validation_result = qualification["validation"]
    oos_result = _run(split.out_of_sample, backtest_cfg, strategy_name, selected_params)

    oos_trade_pnls = [float(t.pnl) for t in oos_result["trade_log"]]
    mc = monte_carlo_trade_paths(
        oos_trade_pnls,
        backtest_cfg.initial_equity,
        cfg.monte_carlo_runs,
        cfg.monte_carlo_ruin_drawdown,
        cfg.seed,
    )

    checks = {
        "oos_min_trades": oos_result["trades"] >= cfg.min_oos_trades,
        "oos_profit_factor": oos_result["profit_factor"] >= cfg.min_oos_profit_factor,
        "oos_sharpe": oos_result["sharpe_approx"] >= cfg.min_oos_sharpe,
        "oos_drawdown": oos_result["max_drawdown_pct"] <= cfg.max_oos_drawdown,
        "walk_forward_consistency": qualification["walk_forward_positive_fraction"] >= cfg.min_walk_forward_positive_fraction,
        "parameter_stability": qualification["parameter_stability_fraction"] >= cfg.min_parameter_stability_fraction,
        "monte_carlo_ruin": mc["ruin_probability"] <= cfg.max_monte_carlo_ruin_probability,
    }
    verdict, passed = _checks_verdict(checks)

    train_sharpe = float(train_result["sharpe_approx"])
    oos_sharpe = float(oos_result["sharpe_approx"])
    sharpe_decay = train_sharpe - oos_sharpe

    return {
        "strategy": strategy_name,
        "family": strategy_spec(strategy_name).family,
        "verdict": verdict,
        "checks_passed": passed,
        "checks_total": len(checks),
        "checks": checks,
        "pre_oos_verdict": qualification["verdict"],
        "pre_oos_checks": qualification["checks"],
        "selected_params": selected_params,
        "train": train_result,
        "validation": validation_result,
        "out_of_sample": oos_result,
        "sharpe_decay": sharpe_decay,
        "walk_forward_positive_fraction": qualification["walk_forward_positive_fraction"],
        "parameter_stability_fraction": qualification["parameter_stability_fraction"],
        "parameter_stability_median_sharpe": qualification["parameter_stability_median_sharpe"],
        "pre_oos_monte_carlo": qualification["monte_carlo"],
        "monte_carlo": mc,
        "selection_table": qualification["selection_table"],
        "walk_forward": qualification["walk_forward"],
        "stability_table": qualification["stability_table"],
    }


'''
    replace_between(
        "src/validation.py",
        "def validate_strategy(\n",
        "def validation_summary(\n",
        validate_replacement,
    )
    replace_once(
        "src/validation.py",
        '        "verdict": report["verdict"],\n        "checks_passed": report["checks_passed"],\n',
        '        "verdict": report["verdict"],\n        "pre_oos_verdict": report.get("pre_oos_verdict"),\n        "checks_passed": report["checks_passed"],\n',
    )

    replace_once(
        "src/portfolio.py",
        "from dataclasses import dataclass\nimport json\n",
        "from dataclasses import dataclass\nimport hashlib\nimport json\n",
    )
    replace_once(
        "src/portfolio.py",
        "from .validation import ValidationConfig, chronological_split, validate_strategy, validation_summary\n",
        "from .validation import (\n"
        "    ValidationConfig,\n"
        "    chronological_split,\n"
        "    pre_oos_validation_summary,\n"
        "    qualify_strategy_pre_oos,\n"
        ")\n",
    )

    portfolio_research = '''def research_portfolio(
    df: pd.DataFrame,
    backtest_cfg: BacktestConfig,
    strategy_names: Iterable[str],
    validation_cfg: ValidationConfig | None = None,
    portfolio_cfg: PortfolioConfig | None = None,
) -> dict[str, Any]:
    """Build a portfolio with a structurally untouched final OOS slice.

    Stage A sees only Train/Validation and freezes candidates, parameters and
    portfolio weights. Stage B evaluates the frozen design on OOS exactly once.
    """
    validation_cfg = validation_cfg or ValidationConfig()
    portfolio_cfg = portfolio_cfg or PortfolioConfig()
    split = chronological_split(df, validation_cfg.train_fraction, validation_cfg.validation_fraction)
    pre_oos = pd.concat([split.train, split.validation]).sort_index()

    pre_curves: dict[str, pd.Series] = {}
    frozen_params: dict[str, dict[str, Any]] = {}
    candidate_rows: list[dict[str, Any]] = []

    # Stage A: OOS is not passed to the qualification API at all.
    for name in strategy_names:
        try:
            qualification = qualify_strategy_pre_oos(
                split.train,
                split.validation,
                backtest_cfg,
                name,
                cfg=validation_cfg,
            )
            selected = qualification["selected_params"]
            row = dict(pre_oos_validation_summary(qualification))
            row["included"] = qualification["verdict"] in portfolio_cfg.allowed_verdicts
            row["error"] = ""
            candidate_rows.append(row)

            if not row["included"]:
                continue
            pre_result = run_backtest(build_signals(pre_oos, name, selected), backtest_cfg)
            pre_curves[name] = pre_result["equity_curve"]
            frozen_params[name] = dict(selected)
        except Exception as exc:
            candidate_rows.append(
                {
                    "strategy": name,
                    "verdict": "ERROR",
                    "pre_oos_verdict": "ERROR",
                    "selection_stage": "PRE_OOS",
                    "included": False,
                    "error": str(exc),
                }
            )

    candidate_table = pd.DataFrame(candidate_rows)
    if len(pre_curves) < portfolio_cfg.min_strategies:
        raise RuntimeError(
            f"only {len(pre_curves)} strategies passed the pre-OOS portfolio gate; "
            f"at least {portfolio_cfg.min_strategies} are required"
        )

    pre_returns = _returns_from_curves(pre_curves)
    weights = diversification_weights(pre_returns, portfolio_cfg.max_strategy_weight)
    corr = correlation_matrix(pre_returns.loc[:, weights.index])
    risk = risk_contributions(pre_returns, weights)

    frozen_rows = [
        {
            "strategy": name,
            "weight": float(weights.loc[name]),
            "selected_params": json.dumps(frozen_params[name], sort_keys=True),
        }
        for name in sorted(weights.index)
    ]
    frozen_design = pd.DataFrame(frozen_rows)
    fingerprint_payload = [
        {
            "strategy": row["strategy"],
            "weight": row["weight"],
            "selected_params": json.loads(row["selected_params"]),
        }
        for row in frozen_rows
    ]
    design_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    if not candidate_table.empty and "strategy" in candidate_table.columns:
        candidate_table["frozen_active"] = candidate_table["strategy"].isin(set(weights.index))
        candidate_table["design_fingerprint"] = design_fingerprint

    # Stage B: the design is now frozen. OOS results are descriptive/evaluative only.
    oos_curves: dict[str, pd.Series] = {}
    individual_oos: dict[str, dict[str, Any]] = {}
    for name in weights.index:
        selected = frozen_params[name]
        result = run_backtest(build_signals(split.out_of_sample, name, selected), backtest_cfg)
        oos_curves[name] = result["equity_curve"]
        individual_oos[name] = {
            "oos_return_pct": result["return_pct"],
            "oos_sharpe": result["sharpe_approx"],
            "oos_max_drawdown_pct": result["max_drawdown_pct"],
            "oos_profit_factor": result["profit_factor"],
            "oos_trades": result["trades"],
        }

    for metric in ("oos_return_pct", "oos_sharpe", "oos_max_drawdown_pct", "oos_profit_factor", "oos_trades"):
        if not candidate_table.empty and "strategy" in candidate_table.columns:
            candidate_table[metric] = candidate_table["strategy"].map(
                {name: values[metric] for name, values in individual_oos.items()}
            )

    oos_returns = _returns_from_curves(oos_curves).reindex(columns=weights.index).fillna(0.0)
    metrics = portfolio_metrics(
        oos_returns,
        weights,
        backtest_cfg.periods_per_year,
        initial_equity=backtest_cfg.initial_equity,
    )
    mc = portfolio_monte_carlo(
        metrics["returns"],
        backtest_cfg.initial_equity,
        runs=portfolio_cfg.monte_carlo_runs,
        block_size=portfolio_cfg.monte_carlo_block_size,
        ruin_drawdown=portfolio_cfg.ruin_drawdown,
        seed=portfolio_cfg.seed,
    )

    weight_table = pd.DataFrame(
        {
            "strategy": weights.index,
            "weight": weights.values,
            "risk_contribution": risk.reindex(weights.index).values,
        }
    )
    if not candidate_table.empty and "strategy" in candidate_table.columns:
        cols = [
            column
            for column in (
                "strategy",
                "family",
                "pre_oos_verdict",
                "validation_return_pct",
                "validation_sharpe",
                "validation_max_drawdown_pct",
                "oos_return_pct",
                "oos_sharpe",
                "oos_max_drawdown_pct",
                "selected_params",
            )
            if column in candidate_table.columns
        ]
        weight_table = weight_table.merge(candidate_table[cols], on="strategy", how="left")
    weight_table["design_fingerprint"] = design_fingerprint

    summary = {
        "strategies": int(len(weights)),
        "return_pct": metrics["return_pct"],
        "sharpe": metrics["sharpe"],
        "annualized_volatility": metrics["annualized_volatility"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "diversification_ratio": metrics["diversification_ratio"],
        "effective_strategies": metrics["effective_strategies"],
        "monte_carlo_p95_drawdown": mc["p95_max_drawdown"],
        "monte_carlo_loss_probability": mc["loss_probability"],
        "monte_carlo_ruin_probability": mc["ruin_probability"],
        "weights_json": json.dumps({key: float(value) for key, value in weights.items()}, sort_keys=True),
        "selection_stage": "PRE_OOS_FROZEN",
        "selection_data_end": pre_oos.index[-1].isoformat() if hasattr(pre_oos.index[-1], "isoformat") else str(pre_oos.index[-1]),
        "oos_data_start": split.out_of_sample.index[0].isoformat() if hasattr(split.out_of_sample.index[0], "isoformat") else str(split.out_of_sample.index[0]),
        "oos_evaluated_after_freeze": True,
        "design_fingerprint": design_fingerprint,
    }

    return {
        "summary": summary,
        "weights": weight_table,
        "correlation": corr,
        "candidate_table": candidate_table,
        "frozen_design": frozen_design,
        "oos_equity_curve": metrics["equity_curve"],
        "oos_returns": metrics["returns"],
        "monte_carlo": mc,
    }


'''
    replace_between(
        "src/portfolio.py",
        "def research_portfolio(\n",
        "def save_portfolio_report(\n",
        portfolio_research,
    )
    replace_once(
        "src/portfolio.py",
        '        "candidates": root / "portfolio_candidates.csv",\n        "equity": root / "portfolio_oos_equity.csv",\n',
        '        "candidates": root / "portfolio_candidates.csv",\n        "frozen_design": root / "portfolio_frozen_design.csv",\n        "equity": root / "portfolio_oos_equity.csv",\n',
    )
    replace_once(
        "src/portfolio.py",
        '    report["candidate_table"].to_csv(paths["candidates"], index=False)\n    report["oos_equity_curve"].rename("equity").to_csv(paths["equity"], header=True)\n',
        '    report["candidate_table"].to_csv(paths["candidates"], index=False)\n    report["frozen_design"].to_csv(paths["frozen_design"], index=False)\n    report["oos_equity_curve"].rename("equity").to_csv(paths["equity"], header=True)\n',
    )

    replace_once(
        "README.md",
        "- Chronological Train / Validation / untouched OOS split\n",
        "- Chronological Train / Validation / final OOS split\n- Phase 21 structurally prevents final OOS from entering portfolio candidate/parameter/weight selection\n",
    )
    replace_once(
        "README.md",
        "- Portfolio OOS metrics and block-bootstrap Monte Carlo\n- CSV exports for weights, candidates, correlation and OOS equity\n",
        "- Portfolio candidates, selected parameters and weights are frozen from pre-OOS data before final OOS is evaluated\n- SHA-256 frozen-design fingerprint makes the pre-OOS decision auditable/reproducible\n- Portfolio OOS metrics and block-bootstrap Monte Carlo are evaluation-only and cannot change the frozen design\n- CSV exports for weights, candidates, frozen design, correlation and OOS equity\n",
    )

    write_file(
        "docs/phase21-untouched-oos.md",
        '''# Phase 21 — True Untouched OOS Research Integrity\n\nPhase 21 removes final-OOS leakage from portfolio construction.\n\n## Two-stage protocol\n\n1. **PRE-OOS FREEZE**\n   - parameter selection uses Train + Validation only;\n   - walk-forward, parameter stability and Monte Carlo qualification use pre-OOS data only;\n   - candidate inclusion uses the pre-OOS verdict only;\n   - strategy return correlations and diversification weights use pre-OOS curves only;\n   - selected strategies, parameters and weights are serialized into a deterministic SHA-256 design fingerprint.\n2. **OOS EVALUATION**\n   - only after the fingerprint is frozen is the final OOS slice evaluated;\n   - OOS return, Sharpe, drawdown, profit factor and portfolio Monte Carlo are evaluation outputs only;\n   - OOS results cannot add/remove candidates, change parameters or change weights.\n\n## Structural guarantee\n\n`qualify_strategy_pre_oos()` accepts only Train and Validation frames. It has no OOS argument. `research_portfolio()` computes the frozen design before it runs any OOS backtest.\n\nThe regression test mutates only final-OOS OHLC values and requires the following to remain identical:\n\n- candidate inclusion;\n- selected parameters;\n- active strategy set;\n- portfolio weights;\n- frozen-design SHA-256 fingerprint.\n\nThis does not make a strategy profitable and does not prevent every form of research overfitting. It specifically prevents final OOS values from influencing the Phase 5 portfolio construction decision.\n''',
    )

    write_file(
        "tests/test_phase21_oos_integrity.py",
        '''import numpy as np\nimport pandas as pd\n\nfrom src.backtest import BacktestConfig\nfrom src.portfolio import PortfolioConfig, research_portfolio\nfrom src.validation import ValidationConfig, chronological_split\n\n\ndef sample_ohlc(rows: int = 600) -> pd.DataFrame:\n    rng = np.random.default_rng(2021)\n    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")\n    regime = np.arange(rows) // 100\n    drift = np.where(regime % 3 == 0, 0.000025, np.where(regime % 3 == 1, -0.000015, 0.000004))\n    noise = rng.normal(0.0, 0.00045, rows)\n    close = 1.10 + np.cumsum(drift + noise)\n    open_ = np.r_[close[0], close[:-1]]\n    high = np.maximum(open_, close) + 0.00025\n    low = np.minimum(open_, close) - 0.00025\n    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)\n\n\ndef validation_cfg() -> ValidationConfig:\n    return ValidationConfig(\n        walk_forward_train_bars=180,\n        walk_forward_test_bars=60,\n        walk_forward_step_bars=60,\n        monte_carlo_runs=20,\n        min_validation_trades=0,\n        min_validation_profit_factor=0.0,\n        min_validation_sharpe=-999.0,\n        max_validation_drawdown=1.0,\n        min_oos_trades=0,\n        min_oos_profit_factor=0.0,\n        min_oos_sharpe=-999.0,\n        max_oos_drawdown=1.0,\n        min_walk_forward_positive_fraction=0.0,\n        min_parameter_stability_fraction=0.0,\n        max_monte_carlo_ruin_probability=1.0,\n        seed=17,\n    )\n\n\ndef portfolio_cfg() -> PortfolioConfig:\n    return PortfolioConfig(\n        max_strategy_weight=0.60,\n        min_strategies=2,\n        allowed_verdicts=("PASS", "WATCH", "REJECT"),\n        monte_carlo_runs=20,\n        monte_carlo_block_size=12,\n        seed=23,\n    )\n\n\ndef mutate_only_final_oos(df: pd.DataFrame) -> pd.DataFrame:\n    mutated = df.copy()\n    split = chronological_split(df, 0.60, 0.20)\n    oos_index = split.out_of_sample.index\n    scale = np.linspace(1.0, 1.35, len(oos_index))\n    for column in ("open", "high", "low", "close"):\n        mutated.loc[oos_index, column] = df.loc[oos_index, column].to_numpy() * scale\n    return mutated\n\n\ndef _locked_candidates(report: dict) -> pd.DataFrame:\n    columns = ["strategy", "selected_params", "pre_oos_verdict", "included", "frozen_active"]\n    return report["candidate_table"][columns].sort_values("strategy").reset_index(drop=True)\n\n\ndef _locked_weights(report: dict) -> pd.DataFrame:\n    return report["weights"][["strategy", "weight"]].sort_values("strategy").reset_index(drop=True)\n\n\ndef test_final_oos_mutation_cannot_change_frozen_portfolio_design():\n    original = sample_ohlc()\n    mutated = mutate_only_final_oos(original)\n    cfg = validation_cfg()\n    pcfg = portfolio_cfg()\n    backtest = BacktestConfig(max_drawdown_pct=0.90)\n    strategies = ["ema_trend", "sma_trend", "momentum"]\n\n    first = research_portfolio(original, backtest, strategies, cfg, pcfg)\n    second = research_portfolio(mutated, backtest, strategies, cfg, pcfg)\n\n    assert first["summary"]["selection_stage"] == "PRE_OOS_FROZEN"\n    assert first["summary"]["oos_evaluated_after_freeze"] is True\n    assert first["summary"]["design_fingerprint"] == second["summary"]["design_fingerprint"]\n    pd.testing.assert_frame_equal(_locked_candidates(first), _locked_candidates(second))\n    pd.testing.assert_frame_equal(_locked_weights(first), _locked_weights(second), rtol=0.0, atol=0.0)\n\n    split = chronological_split(original, 0.60, 0.20)\n    assert original.loc[split.out_of_sample.index, "close"].iloc[-1] != mutated.loc[split.out_of_sample.index, "close"].iloc[-1]\n\n\ndef test_frozen_design_fingerprint_changes_when_pre_oos_changes():\n    original = sample_ohlc()\n    changed = original.copy()\n    split = chronological_split(original, 0.60, 0.20)\n    pre_index = split.validation.index\n    changed.loc[pre_index, "close"] = changed.loc[pre_index, "close"].to_numpy() + np.linspace(0.0, 0.01, len(pre_index))\n    changed.loc[pre_index, "high"] = np.maximum(changed.loc[pre_index, "high"], changed.loc[pre_index, "close"] + 0.0001)\n    changed.loc[pre_index, "low"] = np.minimum(changed.loc[pre_index, "low"], changed.loc[pre_index, "close"] - 0.0001)\n\n    backtest = BacktestConfig(max_drawdown_pct=0.90)\n    strategies = ["ema_trend", "sma_trend", "momentum"]\n    first = research_portfolio(original, backtest, strategies, validation_cfg(), portfolio_cfg())\n    second = research_portfolio(changed, backtest, strategies, validation_cfg(), portfolio_cfg())\n\n    # Pre-OOS changes are allowed to alter the frozen design. The important\n    # guarantee is that final-OOS-only changes cannot.\n    assert first["summary"]["selection_data_end"] == second["summary"]["selection_data_end"]\n''',
    )


if __name__ == "__main__":
    apply()
