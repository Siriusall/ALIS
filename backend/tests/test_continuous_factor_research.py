from app.services import continuous_factor_research as research


def _factors():
    return [
        {
            "id": index,
            "factor_key": key,
            "version": 1,
            "status": "candidate",
            "category": category,
            "direction": direction,
            "formula": {"op": "field", "field": key},
        }
        for index, (key, category, direction) in enumerate(
            (
                ("momentum_20", "趋势", 1),
                ("volatility_20", "波动", -1),
                ("pb", "估值", -1),
                ("turnover", "流动性", 1),
                ("volume_ratio_5", "量价", 1),
                ("beta_60", "风险", -1),
            ),
            start=1,
        )
    ]


def test_proposals_mutate_the_best_completed_population(monkeypatch):
    snapshot = {"snapshot_id": "frozen-1", "factors": _factors()}
    monkeypatch.setattr(
        research,
        "_experiment_rows",
        lambda _snapshot_id: [
            {
                "experiment_id": "parent-best",
                "generation": 0,
                "status": "completed",
                "strategy_hash": "old",
                "fitness": 0.2,
                "factor_weights": {
                    "momentum_20": 0.4,
                    "volatility_20": -0.3,
                    "pb": -0.3,
                },
            }
        ],
    )

    proposal = research.propose_next_combinations(snapshot, count=1)[0]

    assert proposal["parent_experiment_id"] == "parent-best"
    assert proposal["mutation_type"] != "diverse_seed"
    assert len(proposal["factor_weights"]) >= 3
    assert abs(sum(abs(value) for value in proposal["factor_weights"].values()) - 1.0) < 1e-8


def test_multiple_testing_penalty_increases_with_trials():
    folds = [
        {
            "excess_return": 0.03,
            "total_return": 0.05,
            "max_drawdown": -0.08,
            "total_fees": 100.0,
        },
        {
            "excess_return": 0.02,
            "total_return": 0.04,
            "max_drawdown": -0.06,
            "total_fees": 100.0,
        },
    ]

    early_score, early = research._fitness(folds, trial_count=1)
    late_score, late = research._fitness(folds, trial_count=100)

    assert late["multiple_testing_penalty"] > early["multiple_testing_penalty"]
    assert late_score < early_score


def test_research_strategy_is_explicitly_non_production(monkeypatch):
    monkeypatch.setattr(
        research,
        "resolve_production_strategy_champion",
        lambda: {"strategy": {"content": {"lookback_days": 20, "rebalance": {}}}},
    )

    content = research._strategy_content(
        {"momentum_20": 0.6, "volatility_20": -0.4}
    )

    assert content["research_only"] is True
    assert content["lookback_days"] == 120
    assert content["score"]["factors"]["momentum_20"] == 0.6


def test_population_keeps_distinct_risk_and_diversity_elites():
    def row(experiment_id, fitness, drawdown, instability, weights, stage="quick_screen"):
        return {
            "experiment_id": experiment_id,
            "fitness": fitness,
            "factor_weights": weights,
            "research_stage": stage,
            "metrics": {"aggregate": {
                "worst_drawdown": drawdown,
                "excess_instability": instability,
            }},
        }

    selected = research._diverse_population([
        row("fitness", 0.10, -0.20, 0.10, {"a": 0.5, "b": 0.5}),
        row("drawdown", 0.05, -0.03, 0.08, {"a": 0.48, "b": 0.52}),
        row("stable", 0.04, -0.08, 0.01, {"c": 0.5, "d": 0.5}),
        row("confirmed", 0.03, -0.06, 0.03, {"e": 0.5, "f": 0.5}, "confirmation"),
    ], limit=4)

    roles = {item["experiment_id"]: role for item, role in selected}
    assert roles["confirmed"] == "confirmed_elite"
    assert roles["fitness"] == "fitness_elite"
    assert roles["drawdown"] == "drawdown_elite"
    assert roles["stable"] == "stability_elite"


def test_epoch_requires_quick_screen_and_confirmation():
    quick = [
        {"research_stage": "quick_screen", "status": "completed"}
        for _ in range(research.QUICK_SCREEN_TARGET)
    ]
    confirmations = [
        {"research_stage": "confirmation", "status": "completed"}
        for _ in range(research.CONFIRMATION_ELITE_COUNT)
    ]

    assert research._research_epoch_complete(quick) is False
    assert research._research_epoch_complete(quick + confirmations) is True
