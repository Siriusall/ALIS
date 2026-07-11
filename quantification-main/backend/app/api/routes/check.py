"""
诊断接口：检查 TuShare 日数据是否具备完整获取权限。
用于排查「策略信心值都相同」等问题（常因 daily_basic/市值 无数据导致）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter
from app.integrations.tushare_client import create_strict_tushare_pro

router = APIRouter(tags=["check"])


def _run_tushare_diagnostics() -> dict[str, Any]:
    """
    检查：1) TUSHARE_TOKEN 是否配置  2) daily_basic 是否有数据  3) stock_basic 是否可用。
    不抛异常，所有错误写入 errors，并尽量返回已得到的部分结果。
    """
    result: dict[str, Any] = {
        "token_configured": False,
        "env_path": None,
        "daily_basic_ok": False,
        "daily_basic_count": 0,
        "sample_trade_date": None,
        "stock_basic_ok": False,
        "stock_basic_count": 0,
        "errors": [],
    }
    import os

    from app.core.project_env import get_primary_env_path, load_project_env

    # 1) Token 与 .env
    load_project_env()
    env_path = get_primary_env_path()
    result["env_path"] = str(env_path)
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        result["errors"].append("TUSHARE_TOKEN 未配置（环境变量或 .env 中均无）")
        return result
    result["token_configured"] = True

    # 2) 初始化 TuShare 并拉取 daily_basic
    try:
        pro = create_strict_tushare_pro(token)
    except Exception as e:
        result["errors"].append(f"TuShare 初始化失败: {e}")
        return result

    # 3) daily_basic：先查最近交易日，若无则向前找有数据的日期
    from app.core.trade_calendar import get_last_trade_date
    for d in range(0, 8):
        check_date = (get_last_trade_date(for_data_availability=True) - timedelta(days=d)).strftime("%Y%m%d")
        try:
            df = pro.daily_basic(
                trade_date=check_date,
                fields="ts_code,trade_date,close,total_mv,circ_mv,pe_ttm,pb",
            )
            if df is not None and not df.empty:
                result["daily_basic_ok"] = True
                result["daily_basic_count"] = int(len(df))
                result["sample_trade_date"] = check_date
                break
        except Exception as e:
            result["errors"].append(f"daily_basic({check_date}) 调用异常: {e}")
            break
    else:
        result["errors"].append(
            "近 7 日内 daily_basic 均无数据：可能非交易日、或账号积分不足（该接口约需 2000 积分）"
        )

    # 4) stock_basic
    try:
        df = pro.stock_basic(list_status="L", fields="ts_code,symbol,name")
        if df is not None and not df.empty:
            result["stock_basic_ok"] = True
            result["stock_basic_count"] = int(len(df))
        else:
            result["errors"].append("stock_basic 返回为空")
    except Exception as e:
        result["errors"].append(f"stock_basic 调用异常: {e}")

    return result


@router.get("/check/tushare")
def check_tushare() -> dict[str, Any]:
    """
    检查 TuShare 日数据获取权限：
    - token 是否在 .env/环境变量中配置
    - daily_basic（市值/估值）是否有返回（今日或最近交易日）
    - stock_basic 是否可用

    若 daily_basic 无数据，策略打分会缺少市值等因子，容易出现「信心值都相同」。
    """
    return _run_tushare_diagnostics()


@router.get("/check/data-health")
def check_data_health(limit_days: int = 30) -> dict[str, Any]:
    """Local data coverage and factor-coverage health report."""

    from app.services.data_health import build_data_health_report

    return build_data_health_report(limit_days=limit_days)
