#!/usr/bin/env python
from __future__ import annotations

"""
轮询 /api/init/full-sync/status 并在终端显示进度条。

用法（在 backend 目录下）：
  python scripts/watch_full_sync_status.py

如需自定义后端地址，可设置环境变量 BASE_URL（默认 http://127.0.0.1:8000）。
"""

import os
import sys
import time
from typing import Any, Dict

try:
    import requests
except ImportError:
    print("需要 requests，请先在后端虚拟环境中执行: pip install requests")
    sys.exit(2)


BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _fetch_status() -> Dict[str, Any] | None:
    try:
        r = requests.get(f"{BASE_URL}/api/init/full-sync/status", timeout=10)
    except Exception as e:
        print(f"\n请求 full-sync 状态失败: {e}")
        return None
    try:
        data = r.json()
    except Exception:
        print(f"\n状态接口返回非 JSON，status={r.status_code}")
        return None
    if not isinstance(data, dict):
        print(f"\n状态接口返回非字典，status={r.status_code}")
        return None
    return data


def _render_bar(processed: int, total: int, status: str, current_date: str) -> str:
    width = 40
    if total <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, processed / total))
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    pct = f"{ratio * 100:5.1f}%"
    return f"[{bar}] {pct}  {processed}/{total}  status={status}  current={current_date or '-'}"


def main() -> int:
    print(f"监控 full-sync 进度，BASE_URL={BASE_URL}")
    print("若尚未启动 full-sync，可在另一终端调用: POST /api/init/full-sync")

    last_line_len = 0
    while True:
        data = _fetch_status()
        if data is None:
            time.sleep(5)
            continue
        status = str(data.get("status", "") or "")
        total = int(data.get("total_dates") or 0)
        processed = int(data.get("processed_dates") or 0)
        current_date = str(data.get("current_date") or "")

        line = _render_bar(processed, total, status, current_date)
        # 覆盖当前行
        pad = " " * max(0, last_line_len - len(line))
        print("\r" + line + pad, end="", flush=True)
        last_line_len = len(line)

        if status in {"completed", "failed"}:
            print()  # 换行
            print(f"full-sync 结束，状态: {status}")
            if data.get("error"):
                print(f"error: {data['error']}")
            break

        time.sleep(3)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

