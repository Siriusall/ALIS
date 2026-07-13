from __future__ import annotations

"""
TuShare 集成客户端。

严格按照用户提供的初始化方式：
    pro = ts.pro_api(token)
    pro._DataApi__token = token
    pro._DataApi__http_url = 'http://lianghua.nanyangqiankun.top'

token 从项目根目录的 `.env` 中读取：TUSHARE_TOKEN=...
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import tushare as ts
import pandas as pd

from dotenv import load_dotenv

_client_lock = threading.Lock()
_client: Optional["TushareClient"] = None


def _load_token_from_env() -> str:
    """
    从项目根目录或 backend/.env 中加载 TUSHARE_TOKEN。
    若环境变量已存在则不覆盖。
    """
    service_root = Path(__file__).resolve().parents[1]
    load_dotenv(service_root / ".env", override=False)
    # Upgrade compatibility: reuse the existing local token without asking the user to configure it again.
    load_dotenv(service_root.parent / "quantification-main" / ".env", override=False)
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN not set in environment or .env")
    return token


def load_tushare_token_from_env() -> str:
    """公开的 token 读取入口（从 .env 或环境变量读取 TUSHARE_TOKEN）。"""
    return _load_token_from_env()


def create_strict_tushare_pro(token: str):
    """
    严格按固定方式初始化 TuShare Pro：
      import tushare as ts
      pro = ts.pro_api(token)
      pro._DataApi__token = token
      pro._DataApi__http_url = "http://lianghua.nanyangqiankun.top"
    """
    pro = ts.pro_api(token)
    pro._DataApi__token = token  # type: ignore[attr-defined]
    pro._DataApi__http_url = "http://lianghua.nanyangqiankun.top"  # type: ignore[attr-defined]
    return pro


class TushareClient:
    """
    包装 TuShare Pro 接口，提供常用方法：
      - daily_basic
      - daily
      - suspend_d
      - stk_limit
      - stock_basic
      - index_weight
    """

    def __init__(self) -> None:
        # 在 Windows 下 requests 可能继承系统代理（如 127.0.0.1:11223），
        # 会导致 TuShare 偶发走本地代理超时；这里默认对 TuShare 域名直连。
        no_proxy = os.getenv("NO_PROXY", "")
        bypass_hosts = ["lianghua.nanyangqiankun.top", "api.waditu.com", "127.0.0.1", "localhost"]
        tokens = [x.strip() for x in no_proxy.split(",") if x.strip()]
        changed = False
        for host in bypass_hosts:
            if host not in tokens:
                tokens.append(host)
                changed = True
        if changed:
            merged = ",".join(tokens)
            os.environ["NO_PROXY"] = merged
            os.environ["no_proxy"] = merged

        token = _load_token_from_env()
        self.token = token
        self.http_url = "http://lianghua.nanyangqiankun.top"
        self.pro = create_strict_tushare_pro(token)

    # ---- 轻量封装 TuShare 接口，统一 DataFrame 返回 ----

    def daily_basic(self, trade_date: str, ts_code: str | None = None, fields: Optional[str] = None) -> pd.DataFrame:
        params: Dict[str, Any] = {"trade_date": trade_date}
        if ts_code is not None:
            params["ts_code"] = ts_code
        df = self.pro.daily_basic(**params, fields=fields)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def daily_basic_resilient(
        self,
        trade_date: str,
        ts_code: str = "",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        严格使用 TuShare Pro 接口（pro.daily_basic）重试获取。
        保持与用户要求一致：仅通过 ts.pro_api + _DataApi__token/_DataApi__http_url。
        """
        retry_sleep = (1.0, 2.0, 3.0)
        last_exc: Exception | None = None
        for i in range(len(retry_sleep) + 1):
            try:
                df = self.daily_basic(trade_date=trade_date, ts_code=ts_code, fields=fields)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
                last_exc = RuntimeError(f"daily_basic empty for trade_date={trade_date}")
            except Exception as e:  # noqa: BLE001
                last_exc = e
            if i < len(retry_sleep):
                time.sleep(retry_sleep[i])
        if last_exc is not None:
            raise RuntimeError(f"daily_basic_resilient failed: {str(last_exc)[:200]}") from last_exc
        return pd.DataFrame()

    def daily(self, trade_date: str, ts_code: str | None = None, fields: Optional[str] = None) -> pd.DataFrame:
        params: Dict[str, Any] = {"trade_date": trade_date}
        if ts_code:
            params["ts_code"] = ts_code
        df = self.pro.daily(**params, fields=fields)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def suspend_d(self, trade_date: str) -> pd.DataFrame:
        df = self.pro.suspend_d(trade_date=trade_date)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def stk_limit(self, trade_date: str, ts_code: str | None = None, fields: Optional[str] = None) -> pd.DataFrame:
        params: Dict[str, Any] = {"trade_date": trade_date}
        if ts_code:
            params["ts_code"] = ts_code
        df = self.pro.stk_limit(**params, fields=fields)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def stock_basic(self, list_status: str = "L", fields: Optional[str] = None) -> pd.DataFrame:
        params: Dict[str, Any] = {"exchange": "", "list_status": list_status}
        df = self.pro.stock_basic(**params, fields=fields)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def index_weight(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.pro.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def get_tushare_client() -> TushareClient:
    """
    获取全局单例 TushareClient，避免重复初始化。
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = TushareClient()
    return _client


def reset_tushare_client() -> TushareClient:
    """
    强制重建全局 TuShare 客户端（重新读取 .env / 环境变量中的 token）。
    适用于运行中 token 变更或上游连接异常后的自愈重试。
    """
    global _client
    with _client_lock:
        _client = TushareClient()
        return _client
