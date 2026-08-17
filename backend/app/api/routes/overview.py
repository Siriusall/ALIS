from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.api.state import add_log
from app.schemas.overview import OverviewResponse
from app.services.factor_engine import score_factors
from app.services.stock_selector import StockSelectionDataNotReadyError, select_affordable_stocks


router = APIRouter()


@router.get("/overview", response_model=OverviewResponse)
def get_overview() -> OverviewResponse:
    try:
        quotes = select_affordable_stocks(limit=None)
        if not quotes:
            raise RuntimeError("target_market_snapshot_empty")
        scores = score_factors(quotes)
        avg_confidence = round(
            sum(s.confidence for s in scores) / max(len(scores), 1), 2
        )
        top_signal = scores[0].symbol if scores else ""
        add_log("INFO", f"概览请求：{len(scores)} 只候选")
        return OverviewResponse(
            total_candidates=len(scores),
            avg_confidence=avg_confidence,
            top_signal=top_signal,
            last_run=datetime.now().isoformat(),
        )
    except StockSelectionDataNotReadyError as exc:
        add_log("ERROR", f"概览 503：目标交易日 {exc.trade_date} 数据未就绪")
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
        add_log("ERROR", str(e))
        raise HTTPException(
            status_code=503,
            detail={"code": "overview_data_not_ready", "message": str(e)[:500]},
        ) from e
