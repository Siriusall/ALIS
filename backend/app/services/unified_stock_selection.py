from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.backtest_data_source import BacktestDataSource
from app.services.point_in_time import normalize_explicit_timestamp, signal_available_time
from app.services.production_strategy import resolve_production_strategy_champion


UNIFIED_SELECTION_SCHEMA_VERSION = "alis_unified_stock_selection_v1"


def _compact_date(value: str) -> str:
    compact = str(value or "").strip().replace("-", "")[:8]
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"invalid signal date: {value}")
    return compact


def _rank_full_market(
    *,
    data_source: BacktestDataSource,
    strategy_id: int,
    signal_date: str,
    top_n: int,
    exchange_group: str,
    scoring_timeout_sec: int | None,
    explain_symbols: list[str] | None = None,
    as_of_time: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    from app.services.quant_run_engine import _build_target_symbols

    return _build_target_symbols(
        ds=data_source,
        strategy_id=int(strategy_id),
        signal_date=signal_date,
        top_n=int(top_n),
        exchange_group=exchange_group,
        scoring_timeout_sec=scoring_timeout_sec,
        explain_symbols=explain_symbols,
        model_inference_mode="online_limited_live",
        as_of_time=as_of_time,
    )


def _prepare_research_admission(ts_code: str, *, as_of_time: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.services.research_workflow import evaluate_core_pool_eligibility
    from app.services.thesis_store import generate_thesis_for_stock

    prepared = generate_thesis_for_stock(ts_code, as_of_time=as_of_time)
    thesis = prepared.get("item") if isinstance(prepared.get("item"), dict) else None
    eligibility = evaluate_core_pool_eligibility(
        ts_code,
        as_of_time=as_of_time,
        prepared_thesis=thesis,
    )
    return prepared, eligibility


def _apply_dynamic_admission(
    symbols: list[str], *, as_of_time: str,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    from app.services.quant_run_engine import _apply_dynamic_strategy_gate

    return _apply_dynamic_strategy_gate(symbols, as_of_time=as_of_time)


def _ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        return value
    return f"{value}.SH" if value[:1] in {"5", "6", "9"} else f"{value}.SZ"


def _selection_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze_thesis(thesis: dict[str, Any] | None) -> dict[str, Any]:
    item = thesis if isinstance(thesis, dict) else {}
    hypotheses = []
    for hypothesis in item.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        evidence = [
            entry for entry in (hypothesis.get("evidence") or [])
            if isinstance(entry, dict)
        ]
        latest = evidence[-1] if evidence else {}
        hypotheses.append({
            "statement": str(hypothesis.get("statement") or ""),
            "metric": str(hypothesis.get("metric") or ""),
            "necessary": bool(hypothesis.get("necessary")),
            "expected": hypothesis.get("expected") if isinstance(hypothesis.get("expected"), dict) else {},
            "status": str(hypothesis.get("status") or ""),
            "latest_evidence": {
                "value": latest.get("value"),
                "reason": str(latest.get("reason") or ""),
                "evidence_as_of_time": latest.get("evidence_as_of_time"),
            },
        })
    return {
        "id": int(item.get("id") or 0),
        "title": str(item.get("title") or ""),
        "thesis_type": str(item.get("thesis_type") or ""),
        "core_thesis": str(item.get("core_thesis") or ""),
        "derived_status": str(item.get("derived_status") or ""),
        "derived_confidence": item.get("derived_confidence"),
        "hypotheses": hypotheses,
    }


def _freeze_dynamic_gate(value: Any) -> Any:
    """Drop operational refresh clocks while retaining point-in-time evidence."""
    if isinstance(value, dict):
        return {
            str(key): _freeze_dynamic_gate(item)
            for key, item in value.items()
            if str(key) not in {"created_at", "updated_at", "generated_at"}
        }
    if isinstance(value, list):
        return [_freeze_dynamic_gate(item) for item in value]
    return value


def build_unified_stock_selection(
    *,
    signal_date: str,
    top_n: int = 20,
    exchange_group: str = "HS",
    scoring_timeout_sec: int | None = None,
    holding_symbols: list[str] | None = None,
    observation_cutoff: str | None = None,
) -> dict[str, Any]:
    """Build the sole formal equity-selection input without executing trades."""

    compact_date = _compact_date(signal_date)
    # Historical replay keeps the strict T-day 15:00 cutoff.  A newly created
    # current-day plan, however, is frozen when the complete close slice is
    # actually received; using a guessed 15:00 wall clock here would reject
    # valid post-close observations and make the plan disagree with its data
    # contract.  The caller must provide the captured observation cutoff for
    # that live path; absent it we preserve the replay-safe historical default.
    cutoff = normalize_explicit_timestamp(observation_cutoff) if observation_cutoff else signal_available_time(compact_date)
    if not cutoff:
        raise ValueError("invalid observation cutoff")
    champion = resolve_production_strategy_champion()
    strategy_id = int(champion.get("strategy_id") or 0)
    if strategy_id <= 0:
        raise RuntimeError("production Champion has no bound selection strategy")

    holdings = list(dict.fromkeys(
        str(value or "").split(".")[0]
        for value in (holding_symbols or [])
        if str(value or "")
    ))
    data_source = BacktestDataSource()
    data_source.set_as_of_time(cutoff)
    ranked_symbols, ranking_detail = _rank_full_market(
        data_source=data_source,
        strategy_id=strategy_id,
        signal_date=compact_date,
        top_n=max(1, int(top_n)),
        exchange_group=str(exchange_group or "HS"),
        scoring_timeout_sec=scoring_timeout_sec,
        explain_symbols=holdings,
        as_of_time=cutoff,
    )
    ranked_symbols = list(dict.fromkeys(
        str(value or "").strip() for value in ranked_symbols if str(value or "").strip()
    ))[:max(1, int(top_n))]
    selection_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in (ranking_detail.get("selections") or [])
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }

    research_targets = list(dict.fromkeys(ranked_symbols + holdings))
    research_admitted: list[str] = []
    research_blocks: list[dict[str, Any]] = []
    research_errors: list[dict[str, Any]] = []
    research_evidence: dict[str, dict[str, Any]] = {}
    for symbol in research_targets:
        code = _ts_code(symbol)
        try:
            thesis_preparation, eligibility = _prepare_research_admission(
                code, as_of_time=cutoff,
            )
        except Exception as exc:  # one bad stock must not hide the rest of the cross-section
            error = {
                "symbol": symbol,
                "ts_code": code,
                "code": "research_admission_error",
                "message": str(exc)[:300],
            }
            research_errors.append(error)
            research_evidence[symbol] = {
                "evaluation_status": "error",
                "eligible_for_core": False,
                "error": error,
                "thesis_preparation": {
                    "ok": False,
                    "evidence_as_of_time": cutoff,
                },
            }
            continue
        failed_gates = [
            str(gate.get("key") or gate.get("label") or "")
            for gate in (eligibility.get("gates") or [])
            if not bool(gate.get("passed"))
        ]
        thesis = (
            thesis_preparation.get("item")
            if isinstance(thesis_preparation.get("item"), dict)
            else None
        )
        research_evidence[symbol] = {
            "evaluation_status": "evaluated",
            "eligible_for_core": bool(eligibility.get("eligible_for_core")),
            "failed_gates": failed_gates,
            "gates": eligibility.get("gates") or [],
            "thesis_count": int(eligibility.get("thesis_count") or 0),
            "thesis_preparation": {
                "ok": bool(thesis_preparation.get("ok")),
                "evidence_as_of_time": cutoff,
            },
            "thesis_snapshot": _freeze_thesis(thesis),
            "research_dimensions": eligibility.get("research_dimensions") or {},
            "raw_metrics": eligibility.get("raw_metrics") or {},
            "valuation": eligibility.get("valuation") or {},
        }
        if eligibility.get("eligible_for_core"):
            research_admitted.append(symbol)
        else:
            research_blocks.append({
                "symbol": symbol,
                "ts_code": code,
                "side": "BUY",
                "code": "unified_research_admission_failed",
                "message": "统一选股研究硬门未通过：" + ",".join(failed_gates),
            })

    dynamic_admitted, dynamic_blocks, dynamic_gate = _apply_dynamic_admission(
        research_admitted,
        as_of_time=cutoff,
    )
    dynamic_gate = _freeze_dynamic_gate(dynamic_gate)
    dynamic_admitted_set = set(dynamic_admitted)
    admitted_symbols = [symbol for symbol in ranked_symbols if symbol in dynamic_admitted_set]
    holding_admitted_symbols = [symbol for symbol in holdings if symbol in dynamic_admitted_set]
    filter_summary = ranking_detail.get("filter_summary") if isinstance(ranking_detail.get("filter_summary"), dict) else {}
    universe_count = int(filter_summary.get("universe") or 0)
    evaluated_count = len(research_targets) - len(research_errors)
    error_ratio = len(research_errors) / max(1, len(research_targets))
    coverage_gates = {
        "full_market_snapshot": universe_count >= 500,
        "research_recall_nonempty": bool(ranked_symbols),
        "research_evaluation_coverage": bool(ranked_symbols) and error_ratio <= 0.20,
        "at_least_one_research_evaluation": evaluated_count > 0,
    }
    infrastructure_blocked = not all(coverage_gates.values())
    status = "blocked" if infrastructure_blocked else "ready" if admitted_symbols else "empty_opportunity"
    blocked_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for block in ranking_detail.get("blocked_events") or []:
        if not isinstance(block, dict):
            continue
        symbol = str(block.get("symbol") or block.get("ts_code") or "").split(".")[0]
        if symbol:
            blocked_by_symbol.setdefault(symbol, []).append(block)
    signal_snapshots = {
        symbol: (
            {
                "status": "scored",
                "selection": selection_by_symbol[symbol],
                "blocks": blocked_by_symbol.get(symbol, []),
            }
            if symbol in selection_by_symbol
            else {
                "status": "not_ranked_or_model_veto",
                "selection": {},
                "blocks": blocked_by_symbol.get(symbol, []),
            }
        )
        for symbol in research_targets
    }
    market_context = (
        ranking_detail.get("selection_market_context")
        if isinstance(ranking_detail.get("selection_market_context"), dict) else {}
    )
    environment_snapshot = {
        "schema_version": "alis_selection_environment_snapshot_v1",
        "selection_market_state": str(ranking_detail.get("selection_market_state") or ""),
        "production_market_state": str(ranking_detail.get("selection_production_market_state") or ""),
        "transparent_market_state": str(ranking_detail.get("selection_transparent_market_state") or ""),
        "short_benchmark_return": ranking_detail.get("selection_short_benchmark_return"),
        "long_benchmark_return": ranking_detail.get("selection_long_benchmark_return"),
        "market_state": market_context.get("market_state") if isinstance(market_context.get("market_state"), dict) else {},
        "market_profile": market_context.get("market_profile") if isinstance(market_context.get("market_profile"), dict) else {},
        "observations": market_context.get("observations") if isinstance(market_context.get("observations"), dict) else {},
        "market_state_binding": market_context.get("market_state_binding") if isinstance(market_context.get("market_state_binding"), dict) else {},
        "market_state_runtime": market_context.get("market_state_runtime") if isinstance(market_context.get("market_state_runtime"), dict) else {},
    }
    factor_expert_snapshot = {
        "schema_version": "alis_factor_expert_selection_v1",
        "selected_factors": list(ranking_detail.get("selected_factors") or []),
        "factor_weights": (
            ranking_detail.get("selected_factor_weights")
            if isinstance(ranking_detail.get("selected_factor_weights"), dict) else {}
        ),
        "factor_bindings": (
            champion.get("factor_bindings")
            if isinstance(champion.get("factor_bindings"), dict) else {}
        ),
        "expert_route": (
            ranking_detail.get("selection_expert_route")
            if isinstance(ranking_detail.get("selection_expert_route"), dict) else {}
        ),
    }
    model_signal_snapshot = {
        "schema_version": "alis_model_signal_snapshot_v1",
        "authorization_policy": {
            "schema_version": "alis_model_online_tiered_authority_v1",
            "all_models_score_daily": True,
            "formal_promotion_requires_fresh_oos": True,
            "first_live_review_after_future_sessions": 20,
            "champion_failure_warning_only": True,
            "tiers": {
                "observation_only": [
                    "medium_horizon_lambdarank_v1",
                    "capital_persistence_multitask_v1",
                    "leader_lambdarank_v1",
                    "event_impact_assessor_v2",
                    "narrative_transition_multitask_v1",
                    "market_regime_hsmm_v1",
                ],
            },
        },
        "fusion": (
            ranking_detail.get("model_signal_fusion")
            if isinstance(ranking_detail.get("model_signal_fusion"), dict) else {}
        ),
        "symbols": signal_snapshots,
        "daily_ab": {
            "schema_version": "alis_daily_model_ab_snapshot_v1",
            "with_models": {
                "ranked_symbols": ranked_symbols,
                "admitted_symbols": admitted_symbols,
            },
            "without_models": (
                ranking_detail.get("model_disabled_baseline")
                if isinstance(ranking_detail.get("model_disabled_baseline"), dict) else {}
            ),
            "evaluation_policy": {
                "minimum_future_sessions": 20,
                "bad_result": "auto_deweight_or_shadow_only",
                "good_result": "increase_authority_gradually_but_do_not_promote_without_fresh_oos",
            },
        },
    }

    contract_base = {
        "schema_version": UNIFIED_SELECTION_SCHEMA_VERSION,
        "signal_date": compact_date,
        "data_as_of_time": cutoff,
        "strategy_id": strategy_id,
        "champion_id": int(champion.get("champion_id") or 0),
        "champion_version": str(champion.get("champion_version") or ""),
        "ranked_symbols": ranked_symbols,
        "research_admitted_symbols": research_admitted,
        "admitted_symbols": admitted_symbols,
        "holding_symbols": holdings,
        "holding_admitted_symbols": holding_admitted_symbols,
        "research_evidence": research_evidence,
        "research_blocks": research_blocks,
        "research_errors": research_errors,
        "dynamic_blocks": dynamic_blocks,
        "dynamic_gate": dynamic_gate,
        "environment_snapshot": environment_snapshot,
        "factor_expert_snapshot": factor_expert_snapshot,
        "model_signal_snapshot": model_signal_snapshot,
        "coverage": {
            "universe_count": universe_count,
            "ranked_count": len(ranked_symbols),
            "evaluated_count": evaluated_count,
            "holding_review_count": len(holdings),
            "research_error_count": len(research_errors),
            "research_error_ratio": round(error_ratio, 8),
            "gates": coverage_gates,
        },
        "source_policy": "full_market_factor_model_then_research_then_dynamic_gate_v1",
        "authority": {
            "formal_candidate_authority": "unified_selection_contract",
            "legacy_pool_membership_required": False,
            "legacy_pool_mutation_may_change_contract": False,
        },
    }
    selection_contract = {
        **contract_base,
        "selection_hash": _selection_hash(contract_base),
    }
    return {
        "ok": not infrastructure_blocked,
        "status": status,
        "schema_version": UNIFIED_SELECTION_SCHEMA_VERSION,
        "selection_contract": selection_contract,
        "selection_hash": selection_contract["selection_hash"],
        "signal_date": compact_date,
        "data_as_of_time": cutoff,
        "strategy_id": strategy_id,
        "champion_id": int(champion.get("champion_id") or 0),
        "champion_version": str(champion.get("champion_version") or ""),
        "ranked_symbols": ranked_symbols,
        "research_admitted_symbols": research_admitted,
        "admitted_symbols": admitted_symbols,
        "holding_symbols": holdings,
        "holding_admitted_symbols": holding_admitted_symbols,
        "selections": [
            selection_by_symbol[symbol]
            for symbol in ranked_symbols
            if symbol in selection_by_symbol
        ],
        "research_evidence": research_evidence,
        "research_blocks": research_blocks,
        "research_errors": research_errors,
        "dynamic_blocks": dynamic_blocks,
        "dynamic_gate": dynamic_gate,
        "ranking_detail": ranking_detail,
        "policy": {
            "formal_candidate_authority": "unified_selection_contract",
            "legacy_pool_selector": "compatibility_preview_only",
            "point_in_time_cutoff": cutoff,
        },
    }
