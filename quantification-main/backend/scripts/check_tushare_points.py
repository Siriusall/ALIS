#!/usr/bin/env python
from __future__ import annotations

"""
检查 TuShare 账户积分的脚本。

用法（在 backend 目录下，已激活 .venv）:
    python scripts/check_tushare_points.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
import tushare as ts


def main() -> None:
    # 尝试从项目根(quantification) 或 backend 或当前目录加载 .env
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

    # 严格按你要求的初始化方式
    pro = ts.pro_api(token)
    pro._DataApi__token = token  # 必须有
    pro._DataApi__http_url = "http://lianghua.nanyangqiankun.top"  # 必须有

    print("\n[3] 查询账户积分信息 …")
    try:
        df = pro.user(token=token)
        print(df)
    except Exception as e:
        print(f"查询失败: {e}")


if __name__ == "__main__":
    main()

