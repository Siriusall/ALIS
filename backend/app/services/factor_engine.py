"""
日线因子引擎：因子注册表 + 截面/日线因子计算，支持按策略配置动态选择因子并组合打分。
"""
from __future__ import annotations

import logging
import math
from datetime import date
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.trade_calendar import get_last_trade_date
from app.integrations.market_data_client import StockQuote

logger = logging.getLogger("quant.factor_engine")
from app.services.cache_consolidation import read_consolidated_cache
from app.services.daily_data import DailyBar, get_daily_bars_lookback  # DailyCacheNotReadyError 由调用方捕获
from app.services.strategy_config import (
    ParsedStrategyConfig,
    get_strategy_factor_names,
    get_strategy_score_for_market_state,
    parse_strategy_content,
)
from app.services.factor_platform import compute_registered_factor
from app.services.backtest_data_source import HistBar, SnapshotRow
from app.db.store import _conn as business_conn
from app.services.tushare_snapshot import get_daily_snapshot, get_latest_cached_snapshot_date
from app.services.tushare_snapshot import DailySnapshotRow
from app.services.tushare_universe import build_index_membership_set
from app.services.backtest_engine import _apply_universe_controls, _rank_percentiles


def _mv_to_yi_text(v_wan: float) -> str:
    """市值日志辅助：万元 -> 亿元字符串。"""
    try:
        return f"{(float(v_wan) / 10000.0):.4f}亿"
    except (TypeError, ValueError):
        return "n/a"


@dataclass
class FactorScore:
    symbol: str
    name: str
    price: float
    change_pct: float
    volume: float
    turnover: float
    confidence: float
    # 可选：各因子原始值，供调试或前端展示
    factor_values: Optional[Dict[str, float]] = None


@dataclass
class FactorContext:
    """因子计算上下文：截面行情 + 日线历史面板，供需要横截面或历史的因子使用。"""
    quotes: List[StockQuote]
    daily_bars: Dict[str, List[DailyBar]]  # symbol -> 按日期升序的 DailyBar 列表
    benchmark_return: float | None = None
    benchmark_returns: List[float] | None = None


# 因子函数签名：(symbol, hist, today, context) -> float
# hist 为该 symbol 的日线序列（升序），today 为当日快照（可能为 None）
FactorFn = Callable[
    [str, List[DailyBar], Optional[StockQuote], FactorContext],
    float,
]


def _normalize(values: List[float]) -> List[float]:
    if not values:
        return []
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        return [0.0 for _ in values]
    return [(v - min_v) / (max_v - min_v) for v in values]


def _norm_symbol(symbol: str) -> str:
    s = str(symbol or "").strip()
    if "." in s:
        s = s.split(".", 1)[0]
    low = s.lower()
    for p in ("sh", "sz", "bj"):
        if low.startswith(p):
            s = s[len(p) :]
            break
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _load_consolidated_rows(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    从 all_in_one 读取当日（最近交易日）聚合行：symbol -> factors。
    """
    trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    payload = read_consolidated_cache(date=trade_date, symbols=symbols)
    data = payload.get("data") if isinstance(payload, dict) else None
    per_day = data.get(trade_date) if isinstance(data, dict) else None
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(per_day, dict):
        for sym, row in per_day.items():
            if isinstance(row, dict):
                out[_norm_symbol(str(sym))] = row
    # The versioned materialization is the authoritative source when present.
    # Legacy all-in-one fields remain a compatibility fallback during rollout.
    try:
        normalized = sorted({_norm_symbol(symbol) for symbol in symbols if _norm_symbol(symbol)})
        for offset in range(0, len(normalized), 500):
            batch = normalized[offset: offset + 500]
            placeholders = ",".join("?" for _ in batch)
            with business_conn() as conn:
                rows = conn.execute(
                    f"""SELECT v.ts_code,d.factor_key,v.raw_value
                        FROM factor_values v
                        JOIN factor_definitions d ON d.id=v.factor_definition_id
                        WHERE v.trade_date=? AND d.status='active'
                          AND v.ts_code IN ({placeholders})""",
                    (trade_date, *batch),
                ).fetchall()
            for item in rows:
                record = dict(item)
                symbol = _norm_symbol(str(record.get("ts_code") or ""))
                out.setdefault(symbol, {})[str(record.get("factor_key") or "")] = record.get("raw_value")
    except Exception as exc:
        logger.debug("versioned factor materialization unavailable: %s", str(exc)[:120])
    return out


# ---------- 截面因子（仅用当日 quote）----------
def _factor_volume(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    if not today:
        return 0.0
    return float(today.volume)


def _factor_turnover(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    if not today:
        return 0.0
    return float(today.turnover)


def _factor_change_pct(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    if not today:
        return 0.0
    return abs(float(today.change_pct))


# ---------- 日线因子（需 hist，可选 today）----------
def _factor_momentum_5(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    return _unified_factor("momentum_5", symbol, hist, today, context)


def _factor_momentum_20(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    return _unified_factor("momentum_20", symbol, hist, today, context)


def _factor_volatility_20(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    return _unified_factor("volatility_20", symbol, hist, today, context)


def _factor_volume_ratio_5(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    return _unified_factor("volume_ratio_5", symbol, hist, today, context)


def _unified_factor(
    key: str,
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    current_close = float(today.price) if today else float(hist[-1].close if hist else 0)
    current_volume = float(today.volume) if today else float(hist[-1].volume if hist else 0)
    row = SnapshotRow(
        symbol=symbol,
        date=get_last_trade_date(for_data_availability=True).isoformat(),
        open=current_close,
        high=current_close,
        low=current_close,
        close=current_close,
        prev_close=current_close,
        change_pct=float(today.change_pct) if today else 0.0,
        volume=current_volume,
        turnover=float(today.turnover) if today else 0.0,
        name=str(today.name) if today else "",
    )
    bars = [
        HistBar(
            date=str(getattr(item, "date", "")),
            open=float(item.close), high=float(item.close), low=float(item.close),
            close=float(item.close), volume=float(item.volume),
            turnover=float(getattr(item, "turnover", 0) or 0),
        )
        for item in hist
    ]
    snapshot = _ensure_snapshot(symbol, context)
    daily_basic = {
        symbol: {
            "float_mv": float(getattr(snapshot, "float_mv", 0) or 0) if snapshot else 0,
            "total_mv": float(getattr(snapshot, "total_mv", 0) or 0) if snapshot else 0,
            "pe_ttm": float(getattr(snapshot, "pe_ttm", 0) or 0) if snapshot else 0,
            "pb": float(getattr(snapshot, "pb", 0) or 0) if snapshot else 0,
            "turnover_rate": float(getattr(snapshot, "turnover_rate", 0) or 0) if snapshot else 0,
        }
    }
    return compute_registered_factor(
        key,
        row,
        bars,
        daily_basic,
        benchmark_return=context.benchmark_return,
        benchmark_returns=context.benchmark_returns,
    )


def _factor_momentum_60(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("momentum_60", symbol, hist, today, context)


def _factor_momentum_120(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("momentum_120", symbol, hist, today, context)


def _factor_relative_strength_60(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("relative_strength_60", symbol, hist, today, context)


def _factor_beta_60(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("beta_60", symbol, hist, today, context)


def _factor_turnover_rate(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("turnover_rate", symbol, hist, today, context)


def _factor_total_mv(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("total_mv", symbol, hist, today, context)


def _factor_downside_volatility_60(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("downside_volatility_60", symbol, hist, today, context)


def _factor_max_drawdown_60(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("max_drawdown_60", symbol, hist, today, context)


def _factor_earnings_yield(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("earnings_yield", symbol, hist, today, context)


def _factor_book_to_price(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    return _unified_factor("book_to_price", symbol, hist, today, context)


def _factor_industry_candidate(symbol: str, hist: List[DailyBar], today: Optional[StockQuote], context: FactorContext) -> float:
    """Keep unpromoted industry factors neutral in legacy on-the-fly scoring."""
    return 0.0


# ---------- TuShare 截面因子（市值/估值/状态/指数成分）----------
def _ensure_snapshot(symbol: str, context: FactorContext):
    """
    工具函数：按当前自然日从 TuShare 获取快照（带缓存），并返回该 symbol 对应记录（可能为 None）。
    cache_only 时若请求的交易日无缓存，只允许回退到该日或更早的快照。
    """
    trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    snapshot_map = get_daily_snapshot(trade_date, cache_only=True)
    if not snapshot_map:
        fallback_date = get_latest_cached_snapshot_date(on_or_before=trade_date)
        if fallback_date:
            snapshot_map = get_daily_snapshot(fallback_date, cache_only=True)
        if not snapshot_map:
            logger.warning(
                "[选股] _ensure_snapshot: trade_date=%s 无缓存且 fallback 后仍为空，symbol=%s 将得 float_mv=0",
                trade_date,
                symbol,
            )
    row = snapshot_map.get(symbol) if snapshot_map else None
    if snapshot_map and row is None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("[选股] _ensure_snapshot: symbol=%s 不在快照中（快照共 %d 只）", symbol, len(snapshot_map))
    return row


def _factor_float_mv(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    if not row:
        return 0.0
    return float(row.float_mv or 0.0)


def _factor_pe_ttm(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    if not row:
        return 0.0
    return float(row.pe_ttm or 0.0)


def _factor_pb(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    if not row:
        return 0.0
    return float(row.pb or 0.0)


def _factor_is_st(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    return 1.0 if row and row.is_st else 0.0


def _factor_is_suspended(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    return 1.0 if row and row.is_suspended else 0.0


def _factor_is_limit_up(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    return 1.0 if row and row.is_limit_up else 0.0


def _factor_is_limit_down(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    row = _ensure_snapshot(symbol, context)
    return 1.0 if row and row.is_limit_down else 0.0


def _factor_in_hs300(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    # 使用沪深300指数代码 000300.SH
    trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    members = build_index_membership_set("000300.SH", trade_date, cache_only=True)
    return 1.0 if members.get(symbol) else 0.0


def _factor_in_csi500(
    symbol: str,
    hist: List[DailyBar],
    today: Optional[StockQuote],
    context: FactorContext,
) -> float:
    # 使用中证500指数代码 000905.SH
    trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    members = build_index_membership_set("000905.SH", trade_date, cache_only=True)
    return 1.0 if members.get(symbol) else 0.0


# ---------- 因子注册表 ----------
FACTOR_REGISTRY: Dict[str, FactorFn] = {
    "volume": _factor_volume,
    "turnover": _factor_turnover,
    "turnover_rate": _factor_turnover_rate,
    "change_pct": _factor_change_pct,
    "momentum_5": _factor_momentum_5,
    "momentum_20": _factor_momentum_20,
    "momentum_60": _factor_momentum_60,
    "momentum_120": _factor_momentum_120,
    "relative_strength_60": _factor_relative_strength_60,
    "volatility_20": _factor_volatility_20,
    "downside_volatility_60": _factor_downside_volatility_60,
    "max_drawdown_60": _factor_max_drawdown_60,
    "beta_60": _factor_beta_60,
    "earnings_yield": _factor_earnings_yield,
    "book_to_price": _factor_book_to_price,
    "volume_ratio_5": _factor_volume_ratio_5,
    "industry_demand_proxy": _factor_industry_candidate,
    "industry_inventory_proxy": _factor_industry_candidate,
    "industry_ppi_yoy_proxy": _factor_industry_candidate,
    # TuShare 截面因子
    "float_mv": _factor_float_mv,
    "total_mv": _factor_total_mv,
    "pe_ttm": _factor_pe_ttm,
    "pb": _factor_pb,
    "is_st": _factor_is_st,
    "is_suspended": _factor_is_suspended,
    "is_limit_up": _factor_is_limit_up,
    "is_limit_down": _factor_is_limit_down,
    "in_hs300": _factor_in_hs300,
    "in_csi500": _factor_in_csi500,
}

# 需要日线历史的因子（用于决定是否拉取 daily_bars）
FACTORS_NEED_HISTORY = {
    "momentum_5", "momentum_20", "momentum_60", "momentum_120",
    "relative_strength_60", "volatility_20", "downside_volatility_60",
    "max_drawdown_60", "beta_60",
    "volume_ratio_5",
}

# 单次选股拉取日线的标的数上限（全市场约 5000+，设 10000 即不截断）
_DAILY_BARS_SYMBOL_CAP = 10000

# 仅依赖 TuShare 快照的因子：可先按这些因子预筛，再只对候选池拉日线，避免全市场拉日线导致超时/封禁
SNAPSHOT_ONLY_FACTORS = {"float_mv", "total_mv", "pe_ttm", "pb", "is_suspended", "is_limit_up", "is_limit_down", "is_st"}


def _snapshot_only_vals(symbol: str, snapshot_map: Dict[str, DailySnapshotRow]) -> Dict[str, float]:
    """从快照中取仅需 snapshot 的因子值，用于预筛（不拉日线）。"""
    row = snapshot_map.get(symbol)
    if not row:
        return {f: 0.0 for f in SNAPSHOT_ONLY_FACTORS}
    return {
        "float_mv": float(row.float_mv or 0),
        "total_mv": float(row.total_mv or 0),
        "pe_ttm": float(row.pe_ttm or 0),
        "pb": float(row.pb or 0),
        "is_suspended": 1.0 if row.is_suspended else 0.0,
        "is_limit_up": 1.0 if row.is_limit_up else 0.0,
        "is_limit_down": 1.0 if row.is_limit_down else 0.0,
        "is_st": 1.0 if row.is_st else 0.0,
    }


_DEFAULT_WEIGHTS = {"volume": 0.4, "turnover": 0.4, "change_pct": 0.2}


def _weights_from_strategy(strategy: Dict[str, Any]) -> Dict[str, float]:
    """Extract formula weights from strategy. For code/preset without dict content, return default."""
    if not strategy:
        return _DEFAULT_WEIGHTS
    content = strategy.get("content")
    if isinstance(content, dict):
        # 新格式：content.score.factors 或 content 直接为权重
        score_cfg = content.get("score") if isinstance(content.get("score"), dict) else None
        factors_cfg = score_cfg.get("factors") if score_cfg else None
        if isinstance(factors_cfg, dict):
            total = sum(float(v) for v in factors_cfg.values())
            if total > 0:
                return {k: float(v) / total for k, v in factors_cfg.items()}
        # 旧格式：content 直接为 volume/turnover/change_pct 权重
        w_vol = float(content.get("volume", _DEFAULT_WEIGHTS["volume"]))
        w_turn = float(content.get("turnover", _DEFAULT_WEIGHTS["turnover"]))
        w_chg = float(content.get("change_pct", _DEFAULT_WEIGHTS["change_pct"]))
        total = w_vol + w_turn + w_chg
        if total <= 0:
            return _DEFAULT_WEIGHTS
        return {"volume": w_vol / total, "turnover": w_turn / total, "change_pct": w_chg / total}
    return _DEFAULT_WEIGHTS


def _apply_filters(
    quotes: List[StockQuote],
    factor_values_by_symbol: Dict[str, Dict[str, float]],
    filters: List[Dict[str, Any]],
    snapshot_available: bool = True,
    log_rejections_label: Optional[str] = None,
) -> List[StockQuote]:
    """保留通过所有 filter 条件的 quote。若某因子无值（未计算或数据缺失）则跳过该条过滤，避免整池被清空。
    log_rejections_label: 若传入（如 "全量过滤"），则对前几条未通过的记录打 INFO 日志，便于排查。
    """
    if not filters:
        return list(quotes)
    out: List[StockQuote] = []
    rejected_logged = 0
    for q in quotes:
        vals = factor_values_by_symbol.get(q.symbol) or {}
        pass_all = True
        failed_factor, failed_op, failed_threshold, failed_v = None, None, None, None
        for f in filters:
            factor_name = f.get("factor")
            op = f.get("op", ">")
            threshold = float(f.get("value", 0))
            # 市值因子：TuShare 快照单位为万元。若策略里按「亿」填写（如 20 表示 20 亿），则转为万：20 亿 = 200000 万
            if factor_name in ("float_mv", "total_mv") and 0 < threshold < 10000:
                threshold = threshold * 10000.0
            v = vals.get(factor_name)
            if v is None:
                logger.debug("跳过过滤 %s：%s 无值", q.symbol, factor_name)
                continue
            # 市值为 0 常见于 TuShare 快照缺失或 symbol 未匹配。
            # - 若当日快照整体不可用（snapshot_available=False），跳过该条过滤避免整池清空
            # - 若快照可用，则按真实数值参与过滤（v=0 会自然被诸如 >= 200000 过滤掉）
            if factor_name in ("float_mv", "total_mv") and v == 0 and not snapshot_available:
                logger.debug("跳过过滤 %s：%s=0（快照不可用）", q.symbol, factor_name)
                continue
            if op == ">":
                if not (v > threshold):
                    pass_all = False
                    failed_factor, failed_op, failed_threshold, failed_v = factor_name, op, threshold, v
                    break
            elif op == ">=":
                if not (v >= threshold):
                    pass_all = False
                    failed_factor, failed_op, failed_threshold, failed_v = factor_name, op, threshold, v
                    break
            elif op == "<":
                if not (v < threshold):
                    pass_all = False
                    failed_factor, failed_op, failed_threshold, failed_v = factor_name, op, threshold, v
                    break
            elif op == "<=":
                if not (v <= threshold):
                    pass_all = False
                    failed_factor, failed_op, failed_threshold, failed_v = factor_name, op, threshold, v
                    break
            elif op == "==":
                if not (v == threshold):
                    pass_all = False
                    failed_factor, failed_op, failed_threshold, failed_v = factor_name, op, threshold, v
                    break
        if pass_all:
            out.append(q)
        elif rejected_logged < 5 and failed_factor is not None and log_rejections_label:
            if failed_factor in ("float_mv", "total_mv"):
                logger.info(
                    "[选股] %s未通过示例: symbol=%s factor=%s op=%s threshold=%s(=%s) value=%s(=%s)",
                    log_rejections_label,
                    q.symbol,
                    failed_factor,
                    failed_op,
                    failed_threshold,
                    _mv_to_yi_text(float(failed_threshold or 0.0)),
                    failed_v,
                    _mv_to_yi_text(float(failed_v or 0.0)),
                )
            else:
                logger.info(
                    "[选股] %s未通过示例: symbol=%s factor=%s op=%s threshold=%s value=%s",
                    log_rejections_label,
                    q.symbol,
                    failed_factor,
                    failed_op,
                    failed_threshold,
                    failed_v,
                )
            rejected_logged += 1
    return out


def score_factors(quotes: List[StockQuote]) -> List[FactorScore]:
    """Default strategy: volume 0.4 + turnover 0.4 + change_pct 0.2."""
    return score_factors_with_strategy(quotes, None)


def score_factors_with_strategy(
    quotes: List[StockQuote],
    strategy: Dict[str, Any] | None,
    out_warnings: Optional[List[str]] = None,
) -> List[FactorScore]:
    """
    按策略配置对 quotes 打分。
    - strategy 为 None 时使用默认三因子权重。
    - 否则解析 content 得到 ParsedStrategyConfig，按 score 中的因子名从注册表取因子并加权；
      若策略用到需日线历史的因子，会从缓存加载日线后计算（不请求远端）；再应用 filters，最后按加权得分排序。
    """
    if not quotes:
        return []

    if strategy is None:
        weights = _DEFAULT_WEIGHTS
        volumes = _normalize([q.volume for q in quotes])
        turnovers = _normalize([q.turnover for q in quotes])
        changes = _normalize([abs(q.change_pct) for q in quotes])
        scores: List[FactorScore] = []
        for idx, quote in enumerate(quotes):
            confidence = round(
                (
                    volumes[idx] * weights.get("volume", 0.0)
                    + turnovers[idx] * weights.get("turnover", 0.0)
                    + changes[idx] * weights.get("change_pct", 0.0)
                )
                * 100,
                2,
            )
            scores.append(
                FactorScore(
                    symbol=quote.symbol,
                    name=quote.name,
                    price=quote.price,
                    change_pct=quote.change_pct,
                    volume=quote.volume,
                    turnover=quote.turnover,
                    confidence=confidence,
                    factor_values=None,
                )
            )
        return sorted(scores, key=lambda x: x.confidence, reverse=True)

    # 解析策略配置
    content = strategy.get("content")
    strategy_type = strategy.get("type", "formula")
    config = parse_strategy_content(content, strategy_type)
    market_state = ""
    if isinstance(content, dict) and isinstance(content.get("regime_scores"), dict):
        try:
            from app.services.market_context import market_context_as_of
            from app.services.point_in_time import signal_available_time

            trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y-%m-%d")
            context = market_context_as_of(signal_available_time(trade_date))
            market_state = str(((context.get("market_state") or {}).get("state") or "")) if context.get("ok") else ""
        except Exception:  # noqa: BLE001
            market_state = ""
    score_weights = get_strategy_score_for_market_state(content, strategy_type, market_state)
    if not score_weights:
        score_weights = _DEFAULT_WEIGHTS

    strategy_id = strategy.get("id")

    # 应用 universe：若配置了指数，先限制在指数成分内
    if config.universe and config.universe.get("index"):
        index_code = str(config.universe.get("index", "")).strip()
        if index_code:
            if "." not in index_code:
                index_code = index_code + ".SH" if index_code.startswith(("000", "399")) else index_code + ".SZ"
            try:
                trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
                members = build_index_membership_set(index_code, trade_date, cache_only=True)
                if members:
                    quotes = [q for q in quotes if members.get(q.symbol)]
                    logger.info("universe 指数 %s: 成分内 %d 只", index_code, len(quotes))
                if not quotes:
                    logger.info("[选股] strategy_id=%s universe 指数成分后 quotes=0，直接返回", strategy_id)
                    return []
            except Exception as e:
                logger.warning("universe 指数 %s 拉取失败，使用全池: %s", index_code, e)

    # 所需因子名：打分用 + 过滤用
    required_factors = get_strategy_factor_names(content, strategy_type)
    if float(config.universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
        required_factors.add("float_mv")
    for f in config.filters:
        fn = f.get("factor")
        if fn:
            required_factors.add(fn)
    consolidated_rows = _load_consolidated_rows([q.symbol for q in quotes])
    consolidated_fields: set[str] = set()
    for row in consolidated_rows.values():
        consolidated_fields.update(row.keys())

    # TuShare 截面快照可用性：用于决定是否要“跳过市值=0的过滤”
    snapshot_available = True
    snapshot_needed = any(
        f in required_factors and f not in consolidated_fields
        for f in ("float_mv", "total_mv", "pe_ttm", "pb", "is_suspended", "is_limit_up", "is_limit_down", "is_st")
    )
    if snapshot_needed:
        try:
            trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
            snap = get_daily_snapshot(trade_date, cache_only=True)
            used_fallback = False
            if not snap:
                fallback = get_latest_cached_snapshot_date(on_or_before=trade_date)
                if fallback:
                    snap = get_daily_snapshot(fallback, cache_only=True)
                    used_fallback = True
            snapshot_available = bool(snap)
            logger.info(
                "[选股] strategy_id=%s 快照 trade_date=%s snapshot_available=%s used_fallback=%s",
                strategy.get("id"),
                trade_date,
                snapshot_available,
                used_fallback,
            )
        except Exception as e:
            snapshot_available = False
            logger.warning("[选股] strategy_id=%s 快照异常: %s", strategy.get("id"), e)
    if snapshot_needed and not snapshot_available:
        snapshot_warn = (
            "选股所需的点时截面快照不可用，已阻断本次选股；"
            "不得跳过市值、估值或交易状态过滤。"
        )
        logger.warning(snapshot_warn)
        if out_warnings is not None:
            out_warnings.append(snapshot_warn)
        return []

    need_hist = any(
        f in FACTORS_NEED_HISTORY and f in FACTOR_REGISTRY and f not in consolidated_fields
        for f in required_factors
    )
    # 若需日线且策略含“仅快照”过滤（如市值区间），先按快照预筛，再只对候选池从缓存加载日线（不请求远端）
    pre_filters = [f for f in config.filters if f.get("factor") in SNAPSHOT_ONLY_FACTORS]
    if need_hist and snapshot_available and pre_filters:
        trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
        snapshot_map = get_daily_snapshot(trade_date, cache_only=True)
        if not snapshot_map:
            fallback_date = get_latest_cached_snapshot_date(on_or_before=trade_date)
            if fallback_date:
                snapshot_map = get_daily_snapshot(fallback_date, cache_only=True)
        if snapshot_map:
            factor_values_snapshot = {
                q.symbol: _snapshot_only_vals(q.symbol, snapshot_map) for q in quotes
            }
            candidate_quotes = _apply_filters(
                quotes, factor_values_snapshot, pre_filters, snapshot_available=True
            )
            logger.info(
                "[选股] strategy_id=%s 预筛（仅快照）: quotes=%d -> candidate_quotes=%d",
                strategy_id,
                len(quotes),
                len(candidate_quotes),
            )
            if len(candidate_quotes) < len(quotes):
                logger.info(
                    "预筛（仅快照因子）: %d -> %d 只，仅对候选池从缓存加载日线",
                    len(quotes),
                    len(candidate_quotes),
                )
                quotes = candidate_quotes
                if not quotes:
                    logger.info("[选股] strategy_id=%s 预筛后 quotes=0，直接返回", strategy_id)
                    return []
        else:
            logger.info("[选股] strategy_id=%s 预筛跳过: snapshot_map 为空", strategy_id)


    # 若仍需日线且标的数仍过多（如未配置 TuShare 无法市值预筛），只对「按成交量取前 N」从缓存加载日线
    if need_hist and len(quotes) > _DAILY_BARS_SYMBOL_CAP:
        by_volume = sorted(quotes, key=lambda q: getattr(q, "volume", 0) or 0, reverse=True)
        quotes = by_volume[:_DAILY_BARS_SYMBOL_CAP]
        cap_msg = (
            f"日线标的数超限，仅对成交量前 {_DAILY_BARS_SYMBOL_CAP} 只从缓存加载日线并打分；"
            "建议配置 TUSHARE_TOKEN 使市值策略预筛生效。"
        )
        logger.warning("%s", cap_msg)
        if out_warnings is not None:
            out_warnings.append(cap_msg)

    daily_bars: Dict[str, List[DailyBar]] = {}
    if need_hist:
        symbols = [q.symbol for q in quotes]
        end_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
        logger.info(
            "策略需日线，从缓存加载 %d 只（lookback %d 天，仅缓存不请求远端）",
            len(symbols),
            config.lookback_days,
        )
        daily_bars = get_daily_bars_lookback(
            symbols, end_date, lookback_days=config.lookback_days, require_cache=True
        )
        bars_ok = sum(1 for s in symbols if daily_bars.get(s))
        logger.info(
            "[选股] strategy_id=%s 日线加载: symbols=%d 有数据=%d",
            strategy_id,
            len(symbols),
            bars_ok,
        )

    benchmark_returns: List[float] = []
    if required_factors & {"beta_60", "relative_strength_60"}:
        benchmark_source = BacktestDataSource()
        benchmark_returns = benchmark_source.get_index_returns("000300.SH", end_date, lookback=60)
    benchmark_return = (
        math.prod(1.0 + value for value in benchmark_returns) - 1.0
        if benchmark_returns else None
    )
    context = FactorContext(
        quotes=quotes,
        daily_bars=daily_bars,
        benchmark_return=benchmark_return,
        benchmark_returns=benchmark_returns,
    )
    quote_by_symbol = {q.symbol: q for q in quotes}
    factor_values_by_symbol: Dict[str, Dict[str, float]] = {}

    for quote in quotes:
        sym = quote.symbol
        hist = daily_bars.get(sym, [])
        vals: Dict[str, float] = {}
        consolidated_row = consolidated_rows.get(_norm_symbol(sym)) or {}
        for fn in required_factors:
            if fn in consolidated_row:
                try:
                    cv = float(consolidated_row.get(fn) or 0)
                    vals[fn] = abs(cv) if fn == "change_pct" else cv
                    continue
                except (TypeError, ValueError):
                    pass
            factor_fn = FACTOR_REGISTRY.get(fn)
            if factor_fn is None:
                continue
            try:
                v = factor_fn(sym, hist, quote, context)
                vals[fn] = v
            except Exception:
                vals[fn] = 0.0
        factor_values_by_symbol[sym] = vals

    # 统计全量过滤前 float_mv 分布，便于排查「全量过滤后为 0」的问题
    if config.filters and ("float_mv" in str(config.filters) or "total_mv" in str(config.filters)):
        mv_in_range = 0
        mv_zero = 0
        for fv in factor_values_by_symbol.values():
            v = fv.get("float_mv")
            if v is not None:
                if v == 0:
                    mv_zero += 1
                elif 200000 <= v <= 300000:
                    mv_in_range += 1
        logger.info(
            "[选股] strategy_id=%s 全量过滤前 float_mv: 在[20,30]亿区间=%d 只为0=%d 共=%d",
            strategy_id,
            mv_in_range,
            mv_zero,
            len(factor_values_by_symbol),
        )
    if config.filters and logger.isEnabledFor(logging.INFO):
        for sym in list(factor_values_by_symbol.keys())[:2]:
            fv = factor_values_by_symbol[sym]
            mv_info = {k: fv.get(k) for k in ("float_mv", "total_mv") if k in fv}
            if mv_info:
                mv_info_text: Dict[str, Any] = {}
                for k, v in mv_info.items():
                    mv_info_text[k] = {"wan": v, "yi": _mv_to_yi_text(float(v or 0.0))}
                logger.info("[选股] strategy_id=%s 因子采样 %s: %s", strategy_id, sym, mv_info_text)

    # 应用过滤（全量：含日线因子；打日志便于排查「预筛有、全量后为 0」）
    universe_quotes = _apply_universe_controls(quotes, factor_values_by_symbol, config.universe)
    filtered_quotes = _apply_filters(
        universe_quotes,
        factor_values_by_symbol,
        config.filters,
        snapshot_available=snapshot_available,
        log_rejections_label="全量过滤",
    )

    # 仅对打分因子做截面归一化并加权
    score_factor_names = [
        f for f in score_weights.keys()
        if f in FACTOR_REGISTRY or f in consolidated_fields
    ]
    if not score_factor_names:
        score_factor_names = ["volume", "turnover", "change_pct"]
        score_weights = _DEFAULT_WEIGHTS

    # 收集过滤后每只标的在各因子上的值，做归一化
    values_per_factor: Dict[str, List[float]] = {f: [] for f in score_factor_names}
    for q in filtered_quotes:
        vals = factor_values_by_symbol.get(q.symbol) or {}
        for f in score_factor_names:
            values_per_factor[f].append(vals.get(f, 0.0))
    normalized_per_factor: Dict[str, List[float]] = {}
    for f in score_factor_names:
        normalized_per_factor[f] = (
            _rank_percentiles(values_per_factor[f])
            if config.score_normalization == "rank_percentile"
            else _normalize(values_per_factor[f])
        )

    # 先计算原始得分（可能为正或负），再做一次截面 min-max 归一化到 [0, 100]
    raw_scores: List[float] = []
    for idx, quote in enumerate(filtered_quotes):
        w_sum = 0.0
        for f in score_factor_names:
            w = score_weights.get(f, 0.0)
            norm_list = normalized_per_factor.get(f)
            if norm_list is not None and idx < len(norm_list):
                w_sum += w * norm_list[idx]
        raw_scores.append(w_sum)

    if not raw_scores:
        return []

    min_score = min(raw_scores)
    max_score = max(raw_scores)

    scaled_confidences: List[float] = []
    if max_score == min_score:
        # 所有得分相等时统一给 50；接口层会检测并返回警告提示用户
        scaled_confidences = [50.0 for _ in raw_scores]
    else:
        for s in raw_scores:
            norm = (s - min_score) / (max_score - min_score)
            scaled_confidences.append(round(norm * 100, 2))

    scores: List[FactorScore] = []
    for idx, quote in enumerate(filtered_quotes):
        confidence = scaled_confidences[idx]
        fv = factor_values_by_symbol.get(quote.symbol)
        scores.append(
            FactorScore(
                symbol=quote.symbol,
                name=quote.name,
                price=quote.price,
                change_pct=quote.change_pct,
                volume=quote.volume,
                turnover=quote.turnover,
                confidence=confidence,
                factor_values=fv,
            )
        )
    return sorted(scores, key=lambda x: x.confidence, reverse=True)
