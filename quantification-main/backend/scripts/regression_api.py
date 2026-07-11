"""
关键接口回归：请求 /api/overview、/api/candidates、/api/signals/{symbol}、
/api/positions、/api/cache/all、/api/init/full-sync、/api/init/full-sync/status，
记录状态码与通过/失败，并写入 backend/REGRESSION_RESULTS.md。
用法：在项目根目录或 backend 目录执行
  python backend/scripts/regression_api.py
  python scripts/regression_api.py
需先启动后端（默认 http://127.0.0.1:8000），可通过环境变量 BASE_URL 覆盖。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("需要 requests，请先 pip install requests")
    sys.exit(2)

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
RESULTS_PATH = Path(__file__).resolve().parents[1] / "REGRESSION_RESULTS.md"


def get(path: str, params: dict | None = None, timeout: int = 30) -> tuple[int, dict | list | None]:
    r = requests.get(f"{BASE_URL}{path}", params=params or {}, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


def post(path: str, json: dict | None = None, timeout: int = 10) -> tuple[int, dict | list | None]:
    r = requests.post(f"{BASE_URL}{path}", json=json or {}, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    # 使用一个近期交易日，仅用于 cache/all 校验
    date_ymd = datetime.now().strftime("%Y%m%d")

    # GET /api/overview
    code, body = get("/api/overview")
    ok = code == 200 and isinstance(body, dict)
    results.append(("/api/overview", ok, f"status={code}"))

    # GET /api/candidates?limit=1
    code, body = get("/api/candidates", {"limit": 1})
    ok = code == 200 and (isinstance(body, list) or (isinstance(body, dict) and "candidates" in body))
    results.append(("/api/candidates", ok, f"status={code}"))

    # GET /api/signals/{symbol}（用 000001 仅校验接口可调）
    code, body = get("/api/signals/000001")
    ok = code == 200 and isinstance(body, dict)
    results.append(("/api/signals/{symbol}", ok, f"status={code}"))

    # GET /api/positions
    code, body = get("/api/positions")
    ok = code == 200 and isinstance(body, dict) and "positions" in body
    results.append(("/api/positions", ok, f"status={code}"))

    # GET /api/cache/all?date=YYYYMMDD&symbols=000001（必填 date，带 symbols 避免大包）
    code, body = get("/api/cache/all", {"date": date_ymd, "symbols": "000001,000002"})
    ok = code == 200 and isinstance(body, dict) and "data" in body
    results.append(("/api/cache/all", ok, f"status={code}"))

    # GET /api/cache/all 无 date 应 4xx（400 或 422）
    code, _ = get("/api/cache/all")
    ok = 400 <= code < 500
    results.append(("/api/cache/all 无 date 返回 4xx", ok, f"status={code}"))

    # POST /api/init/full-sync（仅校验可调，不阻塞）
    code, body = post("/api/init/full-sync")
    ok = code == 200 and isinstance(body, dict) and ("ok" in body or "status" in body)
    results.append(("/api/init/full-sync", ok, f"status={code}"))

    # GET /api/init/full-sync/status
    code, body = get("/api/init/full-sync/status")
    ok = code == 200 and isinstance(body, dict) and "status" in body
    results.append(("/api/init/full-sync/status", ok, f"status={code}"))

    failed = [r for r in results if not r[1]]
    for name, ok, msg in results:
        print(f"{'PASS' if ok else 'FAIL'} {name} ({msg})")
    if failed:
        print(f"\n失败: {len(failed)}/{len(results)}")
    else:
        print(f"\n通过: {len(results)}/{len(results)}")

    # 写入 REGRESSION_RESULTS.md
    lines = [
        "# 关键接口回归结果",
        "",
        f"运行时间: {datetime.now().isoformat(timespec='seconds')}",
        f"BASE_URL: {BASE_URL}",
        "",
        "| 接口 | 结果 | 备注 |",
        "|------|------|------|",
    ]
    for name, ok, msg in results:
        lines.append(f"| {name} | {'通过' if ok else '失败'} | {msg} |")
    lines.extend(["", f"通过: {len(results) - len(failed)}/{len(results)}", ""])
    RESULTS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果已写入 {RESULTS_PATH}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
