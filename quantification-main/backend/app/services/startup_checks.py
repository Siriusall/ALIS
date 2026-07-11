from __future__ import annotations

import logging
from typing import Any, Dict

import pandas as pd

from app.integrations.tushare_client import create_strict_tushare_pro, load_tushare_token_from_env

logger = logging.getLogger("quant.startup_checks")


def _load_tushare_token() -> str:
    """
    读取 TUSHARE_TOKEN：
    - 优先使用当前环境变量
    - 若缺失则尝试加载项目根目录 .env
    """
    try:
        return load_tushare_token_from_env()
    except Exception:
        return ""


def test_tushare_connectivity() -> tuple[bool, str]:
    """
    严格按用户指定方式做 TuShare 连通性测试。
    """
    try:
        token = _load_tushare_token()
        if not token:
            return False, "TUSHARE_TOKEN 未配置"

        # 严格按用户要求：
        # import tushare as ts
        # token = "你购买的token"
        # pro = ts.pro_api(token)
        # pro._DataApi__token = token
        # pro._DataApi__http_url = 'http://lianghua.nanyangqiankun.top'
        # df = pro.daily(ts_code='000001.SZ', start_date='20240101', end_date='20240131')
        pro = create_strict_tushare_pro(token)
        df = pro.daily(ts_code="000001.SZ", start_date="20240101", end_date="20240131")

        if isinstance(df, pd.DataFrame) and not df.empty:
            return True, f"TuShare daily 测试通过，返回 {len(df)} 条"
        return False, "TuShare daily 返回为空"
    except Exception as e:  # noqa: BLE001
        return False, f"TuShare 连通性测试异常: {str(e)[:200]}"


def run_startup_connectivity_checks() -> Dict[str, Any]:
    """
    启动初始化前执行连通性检查；仅记录结果，不阻断启动。
    """
    result: Dict[str, Any] = {"tushare_ok": False, "errors": []}

    ts_ok, ts_msg = test_tushare_connectivity()
    result["tushare_ok"] = ts_ok
    if ts_ok:
        logger.info("启动前连通性: %s", ts_msg)
    else:
        logger.warning("启动前连通性: %s", ts_msg)
        result["errors"].append(ts_msg)

    return result
