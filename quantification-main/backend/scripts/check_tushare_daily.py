#!/usr/bin/env python3
"""
独立脚本：检查 TuShare 日数据是否具备完整获取权限。
用法（在 backend 目录下）：
  py -3.12 scripts/check_tushare_daily.py
  python scripts/check_tushare_daily.py
或先激活 .venv 再运行。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

# 保证可导入 app（backend 为当前目录或已加入 path）
if __name__ == "__main__":
    backend = Path(__file__).resolve().parent.parent
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

def main() -> None:
    from dotenv import load_dotenv

    # .env：项目根(quantification) 或 backend 或当前目录
    backend_dir = Path(__file__).resolve().parent.parent
    for env_path in [backend_dir.parent / ".env", backend_dir / ".env", Path.cwd() / ".env"]:
        if env_path.exists():
            load_dotenv(env_path, override=False)
            print(f"[1] 已加载 .env: {env_path}")
            break
    else:
        print("[1] 未找到 .env 文件")

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        print("[2] TUSHARE_TOKEN 未配置 → 请在 .env 或环境变量中设置 TUSHARE_TOKEN")
        return
    print("[2] TUSHARE_TOKEN 已配置")

    try:
        import tushare as ts
    except ImportError:
        print("[3] 未安装 tushare，请执行: pip install tushare")
        return

    # 严格按照 TuShare 要求：以下两行必须执行，否则无法获取数据
    pro = ts.pro_api(token)
    pro._DataApi__token = token  # type: ignore[attr-defined]
    pro._DataApi__http_url = "http://lianghua.nanyangqiankun.top"  # type: ignore[attr-defined]

    # daily_basic（市值/估值，策略打分依赖）
    print("\n[3] 检查 daily_basic（市值/估值，策略信心值依赖）…")
    for d in range(0, 8):
        check_date = (date.today() - timedelta(days=d)).strftime("%Y%m%d")
        try:
            df = pro.daily_basic(
                trade_date=check_date,
                fields="ts_code,trade_date,close,total_mv,circ_mv,pe_ttm,pb",
            )
            if df is not None and not df.empty:
                print(f"    ✓ 有数据，日期 {check_date}，条数 {len(df)}")
                break
        except Exception as e:
            print(f"    ✗ 调用异常: {e}")
            break
    else:
        print("    近 7 日均无数据 → 可能非交易日，或账号积分不足（daily_basic 约需 2000 积分）")

    # stock_basic
    print("\n[4] 检查 stock_basic（股票列表）…")
    try:
        df = pro.stock_basic(list_status="L", fields="ts_code,symbol,name")
        if df is not None and not df.empty:
            print(f"    ✓ 有数据，条数 {len(df)}")
        else:
            print("    ✗ 返回为空")
    except Exception as e:
        print(f"    ✗ 调用异常: {e}")

    print("\n若 daily_basic 无数据，策略打分会缺少市值等因子，容易出现「信心值都相同」。")
    print("TuShare 积分与权限说明: https://tushare.pro/document/1?doc_id=108")


if __name__ == "__main__":
    main()
