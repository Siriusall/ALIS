from dataclasses import dataclass
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api.state import add_log
from app.db.store import get_default_strategy_id, get_strategy
from app.schemas.candidate import CandidateResponse, CandidatesListResponse
from app.services.daily_data import DailyCacheNotReadyError
from app.services.factor_engine import score_factors_with_strategy
from app.services.stock_selector import StockSelectionDataNotReadyError, select_affordable_stocks


router = APIRouter()

_NO_VARIANCE_MSG = (
    "策略打分无差异（所有信心值相同），可能缺少市值/行情日数据。"
    "请检查 TUSHARE_TOKEN 是否配置、或是否为交易日再试。"
)


@dataclass
class _InFlightReq:
    event: threading.Event
    response: Optional[CandidatesListResponse] = None
    error: Optional[Exception] = None


_inflight_lock = threading.Lock()
_inflight_by_key: dict[str, _InFlightReq] = {}


@router.get("/candidates", response_model=CandidatesListResponse)
def get_candidates(
    limit: int = Query(0, ge=0, le=20000),
    strategy_id: Optional[int] = Query(None, description="Strategy id; omit for default"),
    budget: Optional[float] = Query(None, ge=0, description="总金额(元)；传入则只保留1手价格<=该金额的股票"),
    exchange: Optional[str] = Query(None, description="可选交易所过滤：HS/CYB/BJ/ALL"),
) -> CandidatesListResponse:
    """
    limit=0 表示返回全部；budget 传入时先按 1手可买 过滤再打分。数据为真实 A 股行情。
    exchange 传入 HS/CYB/BJ 时按板块过滤（HS=沪深合并不含创业板）；ALL 表示全市场；不传默认 HS。
    日线节奏下：收盘后调用本接口获取候选，再调用 /execute/open 执行，次日按计划交易。
    若策略打分无差异（如市值数据缺失），会在 warnings 中返回提示。
    """
    budget_norm = round(float(budget), 4) if budget is not None else None
    exchange_norm = str(exchange or "").strip().upper() or "HS"
    if exchange_norm not in {"ALL", "HS", "CYB", "BJ", "SH", "SZ"}:
        raise HTTPException(status_code=400, detail="exchange 仅支持 HS/CYB/BJ/ALL")
    req_key = f"{limit}|{strategy_id}|{budget_norm}|{exchange_norm}"
    with _inflight_lock:
        entry = _inflight_by_key.get(req_key)
        if entry is None:
            entry = _InFlightReq(event=threading.Event())
            _inflight_by_key[req_key] = entry
            is_owner = True
        else:
            is_owner = False

    if not is_owner:
        # 并发同参请求去重：等待首个请求完成并直接复用结果，避免重复拉 TuShare 快照。
        entry.event.wait(timeout=300)
        if entry.response is not None:
            return entry.response
        if entry.error is not None:
            add_log("ERROR", str(entry.error))
            if isinstance(entry.error, StockSelectionDataNotReadyError):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "decision_data_not_ready",
                        "trade_date": entry.error.trade_date,
                        "message": str(entry.error),
                        "contract": entry.error.contract,
                    },
                )
            raise HTTPException(
                status_code=503,
                detail={"code": "candidate_score_failed", "message": str(entry.error)[:500]},
            )
        return CandidatesListResponse(candidates=[], warnings=["请求等待超时，请重试"])

    try:
        quotes = select_affordable_stocks(
            limit=None,
            max_one_lot_cost=budget if budget and budget > 0 else None,
            exchange=exchange_norm,
        )
        if not quotes:
            add_log("WARN", f"选股 503：行情缓存为空（strategy_id={strategy_id}），请先执行初始化 POST /api/init")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "spot_cache_empty",
                    "message": "行情数据不可用（无缓存）。",
                    "hint": "请先执行初始化：POST /api/init 或重启后端，再重试选股。",
                    "strategy_id": strategy_id,
                },
            )
        strategy = None
        if strategy_id is not None:
            strategy = get_strategy(strategy_id)
        if strategy is None:
            default_id = get_default_strategy_id()
            if default_id is not None:
                strategy = get_strategy(default_id)
        factor_warnings: list[str] = []
        try:
            scores = score_factors_with_strategy(quotes, strategy, out_warnings=factor_warnings)
        except DailyCacheNotReadyError as e:
            add_log("ERROR", f"选股 503：日线缓存未就绪 strategy_id={strategy_id} 缺失={e.missing_count}")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "daily_cache_not_ready",
                    "message": str(e),
                    "missing_count": e.missing_count,
                    "hint": "请先执行初始化：POST /api/init 或重启后端，再重试选股。",
                    "strategy_id": strategy_id,
                },
            )
        take = len(scores) if limit == 0 else min(limit, len(scores))
        add_log("INFO", f"选股请求：预算={budget} 交易所={exchange_norm}，取前 {take} 只（共打分 {len(scores)} 只）")

        out = [
            CandidateResponse(
                symbol=s.symbol,
                name=s.name,
                price=s.price,
                change_pct=s.change_pct,
                volume=s.volume,
                turnover=s.turnover,
                confidence=s.confidence,
            )
            for s in scores[:take]
        ]
        warnings: list[str] = list(factor_warnings)
        if len(scores) >= 2 and len({s.confidence for s in scores}) == 1:
            add_log("WARN", _NO_VARIANCE_MSG)
            warnings.append(_NO_VARIANCE_MSG)
        resp = CandidatesListResponse(candidates=out, warnings=warnings)
        entry.response = resp
        return resp
    except HTTPException:
        raise
    except StockSelectionDataNotReadyError as exc:
        entry.error = exc
        add_log("ERROR", f"选股 503：目标交易日 {exc.trade_date} 数据未就绪")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "decision_data_not_ready",
                "trade_date": exc.trade_date,
                "message": str(exc),
                "contract": exc.contract,
            },
        ) from exc
    except Exception as e:
        entry.error = e
        add_log("ERROR", str(e))
        raise HTTPException(
            status_code=503,
            detail={"code": "candidate_score_failed", "message": str(e)[:500]},
        ) from e
    finally:
        entry.event.set()
        with _inflight_lock:
            _inflight_by_key.pop(req_key, None)
