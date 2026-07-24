from fastapi import APIRouter, HTTPException

from app.api.state import add_log
from app.integrations.market_data_client import fetch_symbol_detail
from app.schemas.signal import SignalDetailResponse
from app.services.factor_engine import score_factors
from app.services.stock_selector import select_affordable_stocks


router = APIRouter()


def _extract_realtime(detail: dict) -> dict | None:
    """从 detail 提取实时价量，有则用，无则 None"""
    if "最新价" in detail and "涨跌幅" in detail:
        return {
            "price": float(detail.get("最新价") or 0),
            "change_pct": float(detail.get("涨跌幅") or 0),
            "volume": float(detail.get("成交量") or 0),
            "turnover": float(detail.get("成交额") or 0),
        }
    return None


@router.get("/signals/{symbol}", response_model=SignalDetailResponse)
def get_signal_detail(symbol: str) -> SignalDetailResponse:
    quotes = select_affordable_stocks(limit=None)
    if not quotes:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "spot_cache_empty",
                "message": "行情数据不可用（无缓存）。",
                "hint": "请先执行初始化：POST /api/init 或重启后端。",
            },
        )
    scores = score_factors(quotes)
    score_map = {s.symbol: s for s in scores}
    detail = fetch_symbol_detail(symbol)
    add_log("INFO", f"信号详情请求：{symbol}")

    rt = _extract_realtime(detail)
    score = score_map.get(symbol)
    if rt is not None:
        price, change_pct, volume, turnover = rt["price"], rt["change_pct"], rt["volume"], rt["turnover"]
        name = str(detail.get("名称", ""))
    elif score is not None:
        price, change_pct, volume, turnover = score.price, score.change_pct, score.volume, score.turnover
        name = score.name
    else:
        price, change_pct, volume, turnover, name = 0.0, 0.0, 0.0, 0.0, ""

    confidence = score.confidence if score else 0.0
    return SignalDetailResponse(
        symbol=symbol,
        name=name,
        factors={
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "turnover": turnover,
            "raw": detail,
        },
        confidence=confidence,
    )
