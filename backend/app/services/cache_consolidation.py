from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from math import sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.core.runtime_paths import backend_cache_root

logger = logging.getLogger("quant.cache_consolidation")

# full-sync 任务锁与状态
_full_sync_lock = threading.Lock()
_full_sync_running = False
_meta_backfill_lock = threading.Lock()
_meta_backfill_running = False
_available_trade_dates_cache: tuple[float, List[str]] = (0.0, [])
_available_trade_dates_lock = threading.Lock()
_AVAILABLE_TRADE_DATES_TTL_SECONDS = 30.0

# 单次 API 允许返回的最大行数（无 symbols 时的保护上限）
MAX_SYMBOL_ROWS_PER_REQUEST = 50000

# all-in-one 合并实现版本（人工维护，便于线上快速识别是否为新逻辑产物）
MERGE_IMPL_VERSION = "2026-03-04.1"

# 参与覆盖率统计的因子键（与 AGENTS.md 因子合同及合并行字段一致）
FACTOR_KEYS_FOR_COVERAGE = (
    "volume", "turnover", "change_pct", "momentum_5", "momentum_20", "volatility_20", "volume_ratio_5",
    "float_mv", "total_mv", "pe_ttm", "pb",
    "is_st", "is_limit_up", "is_limit_down", "is_suspended",
    "open", "high", "low", "close", "price", "turnover_rate",
)


def _cache_root() -> Path:
    return backend_cache_root()


def _daily_dir() -> Path:
    return _cache_root() / "daily"


def _spot_dir() -> Path:
    return _cache_root() / "tushare_spot"


def _snapshot_dir() -> Path:
    return _cache_root() / "tushare_snapshot"


def _backtest_dir() -> Path:
    return _cache_root() / "backtest"


def _bundle_dir() -> Path:
    d = _cache_root() / "all_in_one"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dates_dir() -> Path:
    d = _bundle_dir() / "dates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _consolidated_day_file(date_ymd: str) -> Path:
    return _bundle_dir() / f"{date_ymd}.json"


def _init_status_file() -> Path:
    return _bundle_dir() / "init_status.json"


def write_init_status(data: Dict[str, Any]) -> None:
    """
    写入初始化状态，便于首次加载排障与重试判断。
    """
    payload = dict(data or {})
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        from app.data_platform.db import _conn
        with _conn() as conn:
            conn.execute(
                """INSERT INTO dc_app_state (state_key,payload_json,updated_at) VALUES (?,?,?)
                   ON CONFLICT(state_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                ("market_data_init_status", json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("cache_consolidation: 写入 init_status 失败 %s", str(e)[:120])


def read_init_status() -> Dict[str, Any]:
    try:
        from app.data_platform.db import _conn
        with _conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM dc_app_state WHERE state_key=?", ("market_data_init_status",)
            ).fetchone()
        raw = json.loads(str(dict(row).get("payload_json") or "{}")) if row else {}
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_consolidation: PostgreSQL init status read failed %s", str(exc)[:120])
        return {}


def is_strict_first_load_enabled() -> bool:
    raw = str(os.getenv("INIT_STRICT_FIRST_LOAD", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def validate_first_load_quality(
    *,
    spot_count: int,
    snapshot_count: int,
    basic_info_count: int,
    merged_payload: Optional[Dict[str, Any]],
    trade_date: str,
) -> Dict[str, Any]:
    """
    当日首次加载的质量校验：
    - 保证关键数据源不为空
    - 保证合并行数接近行情规模
    - 保证核心因子覆盖率不过低
    """
    checks: Dict[str, Any] = {}
    errors: List[str] = []
    ymd = _normalize_ymd(trade_date)
    merged_counts = (merged_payload or {}).get("counts") if isinstance((merged_payload or {}).get("counts"), dict) else {}
    merged_rows = int((merged_counts or {}).get("symbol_rows") or 0)
    factor_coverage = (merged_payload or {}).get("factor_coverage") if isinstance((merged_payload or {}).get("factor_coverage"), dict) else {}
    day_cov = factor_coverage.get(ymd) if isinstance(factor_coverage, dict) else {}
    if not isinstance(day_cov, dict):
        day_cov = {}

    checks["spot_count"] = int(spot_count)
    checks["snapshot_count"] = int(snapshot_count)
    checks["basic_info_count"] = int(basic_info_count)
    checks["merged_rows"] = int(merged_rows)

    if spot_count <= 0:
        errors.append("spot_count=0，行情为空")
    if snapshot_count <= 0:
        errors.append("snapshot_count=0，快照为空")
    if basic_info_count <= 0:
        errors.append("basic_info_count=0，基础信息为空")
    if merged_rows <= 0:
        errors.append("merged_rows=0，all-in-one 合并为空")

    if spot_count > 0 and snapshot_count > 0:
        expected_min = int(min(spot_count, snapshot_count) * 0.90)
        checks["merged_rows_min_expected"] = expected_min
        if merged_rows < expected_min:
            errors.append(f"merged_rows={merged_rows} 低于期望阈值 {expected_min}")

    if basic_info_count > 0 and spot_count > 0:
        ratio_basic_spot = basic_info_count / max(1, spot_count)
        checks["basic_vs_spot_ratio"] = round(ratio_basic_spot, 4)
        if ratio_basic_spot < 0.95:
            errors.append(f"basic_info/spot 比例过低 {ratio_basic_spot:.3f} < 0.95")

    for k in ("close", "volume", "change_pct"):
        v = float(day_cov.get(k, 0) or 0)
        checks[f"coverage_{k}"] = round(v, 4)
        if merged_rows > 0 and v < 0.95:
            errors.append(f"{k} 覆盖率过低 {v:.3f} < 0.95")

    return {"ok": len(errors) == 0, "checks": checks, "errors": errors, "trade_date": ymd}


def _normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip()
    if "." in s:
        s = s.split(".", 1)[0]
    for p in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if s.startswith(p):
            s = s[len(p) :]
            break
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _normalize_ymd(v: str) -> str:
    s = str(v or "").strip()
    if len(s) == 10 and "-" in s:
        s = s.replace("-", "")
    return s if len(s) == 8 and s.isdigit() else ""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _upsert(out: Dict[str, Dict[str, Dict[str, Any]]], trade_date: str, symbol: str) -> Dict[str, Any]:
    d = out.setdefault(trade_date, {})
    row = d.setdefault(symbol, {"trade_date": trade_date, "symbol": symbol})
    return row


def compute_factor_coverage(data: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, float]]:
    """
    按日期计算各因子的覆盖率（该日期下拥有该因子非空值的标的数 / 该日期总标的数）。
    返回 { "YYYYMMDD": { "factor_name": ratio, ... }, ... }，ratio 在 [0, 1]。
    """
    result: Dict[str, Dict[str, float]] = {}
    for date_ymd, per_symbol in data.items():
        if not per_symbol:
            continue
        total = len(per_symbol)
        counts: Dict[str, int] = {}
        for row in per_symbol.values():
            if not isinstance(row, dict):
                continue
            for k in FACTOR_KEYS_FOR_COVERAGE:
                if k not in row:
                    continue
                v = row[k]
                if v is None:
                    continue
                counts[k] = counts.get(k, 0) + 1
        result[date_ymd] = {k: (counts.get(k, 0) / total) for k in FACTOR_KEYS_FOR_COVERAGE}
    return result


def _load_spot_by_date() -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    d = _spot_dir()
    if not d.exists():
        return out
    for p in d.glob("spot_*.json"):
        ymd = _normalize_ymd(p.stem.replace("spot_", ""))
        if not ymd:
            continue
        rows = _load_json(p)
        if not isinstance(rows, list):
            continue
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            symbol = _normalize_symbol(str(r.get("symbol", "")))
            if not symbol:
                continue
            per_symbol[symbol] = {
                "name": str(r.get("name", "")),
                "price": float(r.get("price", 0) or 0),
                "close": float(r.get("price", 0) or 0),
                "change_pct": float(r.get("change_pct", 0) or 0),
                "volume": float(r.get("volume", 0) or 0),
                "turnover": float(r.get("turnover", 0) or 0),
            }
        out[ymd] = per_symbol
    return out


def _load_snapshot_by_date() -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    d = _snapshot_dir()
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        if ".tmp." in p.name or p.name.endswith(".lock"):
            continue
        ymd = _normalize_ymd(p.stem)
        if not ymd:
            continue
        payload = _load_json(p)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            symbol = _normalize_symbol(str(r.get("symbol", "")))
            if not symbol:
                ts_code = str(r.get("ts_code", ""))
                symbol = _normalize_symbol(ts_code.split(".")[0] if ts_code else "")
            if not symbol:
                continue
            per_symbol[symbol] = {
                "name": str(r.get("name", "")),
                "close": float(r.get("close", 0) or 0),
                "float_mv": float(r.get("float_mv", 0) or 0),
                "total_mv": float(r.get("total_mv", 0) or 0),
                "pe_ttm": float(r.get("pe_ttm", 0) or 0),
                "pb": float(r.get("pb", 0) or 0),
                "is_st": 1.0 if bool(r.get("is_st", False)) else 0.0,
                "is_suspended": 1.0 if bool(r.get("is_suspended", False)) else 0.0,
                "is_limit_up": 1.0 if bool(r.get("is_limit_up", False)) else 0.0,
                "is_limit_down": 1.0 if bool(r.get("is_limit_down", False)) else 0.0,
            }
        out[ymd] = per_symbol
    return out


def _load_backtest_daily_basic_by_date() -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    d = _backtest_dir()
    if not d.exists():
        return out
    for p in d.glob("daily_basic_*.json"):
        ymd = _normalize_ymd(p.stem.replace("daily_basic_", ""))
        if not ymd:
            continue
        payload = _load_json(p)
        if not isinstance(payload, dict):
            continue
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for symbol, vals in payload.items():
            if not isinstance(vals, dict):
                continue
            sym = _normalize_symbol(str(symbol))
            if not sym:
                continue
            per_symbol[sym] = {
                "float_mv": float(vals.get("float_mv", vals.get("circ_mv", 0)) or 0),
                "total_mv": float(vals.get("total_mv", 0) or 0),
                "pe_ttm": float(vals.get("pe_ttm", 0) or 0),
                "pb": float(vals.get("pb", 0) or 0),
                "turnover_rate": float(vals.get("turnover_rate", 0) or 0),
            }
        out[ymd] = per_symbol
    return out


def _load_daily_and_compute_factors() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    基于 .cache/daily 计算按日期+股票的基础行情与日线因子：
    - open/high/low/close/volume/turnover/change_pct
    - momentum_5/momentum_20/volatility_20/volume_ratio_5
    """
    by_symbol_date: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(dict)
    d = _daily_dir()
    if not d.exists():
        return {}
    for p in d.glob("*_*_*.json"):
        parts = p.stem.split("_")
        if len(parts) < 3:
            continue
        symbol = _normalize_symbol(parts[0])
        if not symbol:
            continue
        rows = _load_json(p)
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            ymd = _normalize_ymd(str(r.get("date", "")))
            if not ymd:
                continue
            by_symbol_date[symbol][ymd] = {
                "open": float(r.get("open", 0) or 0),
                "high": float(r.get("high", 0) or 0),
                "low": float(r.get("low", 0) or 0),
                "close": float(r.get("close", 0) or 0),
                "volume": float(r.get("volume", 0) or 0),
                "turnover": float(r.get("turnover", 0) or 0),
            }

    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for symbol, date_map in by_symbol_date.items():
        dates = sorted(date_map.keys())
        closes = [float(date_map[d]["close"]) for d in dates]
        volumes = [float(date_map[d]["volume"]) for d in dates]
        for i, d0 in enumerate(dates):
            bar = date_map[d0]
            close = closes[i]
            prev_close = closes[i - 1] if i > 0 else 0.0
            change_pct = (close / prev_close - 1.0) if prev_close > 0 else 0.0

            momentum_5 = (close / closes[i - 4] - 1.0) if i >= 4 and closes[i - 4] > 0 else 0.0
            momentum_20 = (close / closes[i - 19] - 1.0) if i >= 19 and closes[i - 19] > 0 else 0.0

            start = max(0, i - 19)
            win_closes = closes[start : i + 1]
            rets: List[float] = []
            for j in range(1, len(win_closes)):
                c0 = win_closes[j - 1]
                c1 = win_closes[j]
                if c0 > 0:
                    rets.append(c1 / c0 - 1.0)
            if rets:
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
                volatility_20 = sqrt(var) if var >= 0 else 0.0
            else:
                volatility_20 = 0.0

            if i >= 4:
                v_win = volumes[i - 4 : i + 1]
                v_avg = sum(v_win) / len(v_win) if v_win else 0.0
                volume_ratio_5 = volumes[i] / v_avg if v_avg > 0 else 0.0
            else:
                volume_ratio_5 = 0.0

            per_day = out.setdefault(d0, {})
            per_day[symbol] = {
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": close,
                "volume": float(bar["volume"]),
                "turnover": float(bar["turnover"]),
                "change_pct": change_pct,
                "momentum_5": momentum_5,
                "momentum_20": momentum_20,
                "volatility_20": volatility_20,
                "volume_ratio_5": volume_ratio_5,
            }
    return out


def _load_daily_and_compute_factors_for_trade_date(trade_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    仅计算指定交易日的基础行情与日线因子，避免单日合并时全量展开所有历史日期。
    返回结构与 _load_daily_and_compute_factors 一致：{ "YYYYMMDD": { symbol: {...} } }。
    """
    ymd = _normalize_ymd(trade_date)
    if not ymd:
        return {}
    d = _daily_dir()
    if not d.exists():
        return {}

    per_day: Dict[str, Dict[str, Any]] = {}
    for p in d.glob("*_*_*.json"):
        parts = p.stem.split("_")
        if len(parts) < 3:
            continue
        symbol = _normalize_symbol(parts[0])
        if not symbol:
            continue
        start_ymd = _normalize_ymd(parts[1])
        end_ymd = _normalize_ymd(parts[2])
        if start_ymd and ymd < start_ymd:
            continue
        if end_ymd and ymd > end_ymd:
            continue

        rows = _load_json(p)
        if not isinstance(rows, list) or not rows:
            continue

        closes: List[float] = []
        volumes: List[float] = []
        bars: List[Dict[str, float]] = []
        target_idx = -1
        for r in rows:
            if not isinstance(r, dict):
                continue
            d0 = _normalize_ymd(str(r.get("date", "")))
            bars.append({
                "date": d0,
                "open": float(r.get("open", 0) or 0),
                "high": float(r.get("high", 0) or 0),
                "low": float(r.get("low", 0) or 0),
                "close": float(r.get("close", 0) or 0),
                "volume": float(r.get("volume", 0) or 0),
                "turnover": float(r.get("turnover", 0) or 0),
            })
            closes.append(float(r.get("close", 0) or 0))
            volumes.append(float(r.get("volume", 0) or 0))
            if d0 == ymd:
                target_idx = len(bars) - 1

        if target_idx < 0 or target_idx >= len(bars):
            continue

        bar = bars[target_idx]
        close = closes[target_idx]
        prev_close = closes[target_idx - 1] if target_idx > 0 else 0.0
        change_pct = (close / prev_close - 1.0) if prev_close > 0 else 0.0
        momentum_5 = (close / closes[target_idx - 4] - 1.0) if target_idx >= 4 and closes[target_idx - 4] > 0 else 0.0
        momentum_20 = (close / closes[target_idx - 19] - 1.0) if target_idx >= 19 and closes[target_idx - 19] > 0 else 0.0

        start = max(0, target_idx - 19)
        win_closes = closes[start : target_idx + 1]
        rets: List[float] = []
        for j in range(1, len(win_closes)):
            c0 = win_closes[j - 1]
            c1 = win_closes[j]
            if c0 > 0:
                rets.append(c1 / c0 - 1.0)
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            volatility_20 = sqrt(var) if var >= 0 else 0.0
        else:
            volatility_20 = 0.0

        if target_idx >= 4:
            v_win = volumes[target_idx - 4 : target_idx + 1]
            v_avg = sum(v_win) / len(v_win) if v_win else 0.0
            volume_ratio_5 = volumes[target_idx] / v_avg if v_avg > 0 else 0.0
        else:
            volume_ratio_5 = 0.0

        per_day[symbol] = {
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": close,
            "volume": float(bar["volume"]),
            "turnover": float(bar["turnover"]),
            "change_pct": change_pct,
            "momentum_5": momentum_5,
            "momentum_20": momentum_20,
            "volatility_20": volatility_20,
            "volume_ratio_5": volume_ratio_5,
        }
    return {ymd: per_day} if per_day else {}


def _build_merge_reason_text(
    date_count: int,
    symbol_rows: int,
    source_status_by_date: Dict[str, Dict[str, Any]],
    date_filter: str,
) -> str:
    """
    生成合并结果原因描述，便于日志快速定位数据源异常。
    """
    if date_count <= 0:
        if date_filter:
            return f"目标交易日 {date_filter} 在源缓存中不存在（未发现可合并日期）"
        return "未发现可合并日期（四类源缓存均为空或日期不可解析）"
    if symbol_rows <= 0:
        if not source_status_by_date:
            return "可合并日期存在，但四类源均无有效行"
        parts: List[str] = []
        for d0 in sorted(source_status_by_date.keys()):
            s = source_status_by_date.get(d0) or {}
            parts.append(
                f"{d0}:daily={int(s.get('daily_rows', 0))},snapshot={int(s.get('snapshot_rows', 0))},"
                f"backtest={int(s.get('backtest_rows', 0))},spot={int(s.get('spot_rows', 0))}"
            )
        return "存在可合并日期但最终 symbol_rows=0；各源行数如下 -> " + " | ".join(parts)
    return "ok"


def build_consolidated_cache_bundle(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """
    构建最终聚合形态：date -> symbol -> factors。
    - 若传 trade_date，仅合并该交易日
    - 否则按所有已缓存日期合并四类缓存数据
    """
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    date_filter = _normalize_ymd(trade_date or "")
    # 单日合并走增量路径：只计算目标交易日，避免每次初始化全量展开 daily 全历史。
    daily = (
        _load_daily_and_compute_factors_for_trade_date(date_filter)
        if date_filter
        else _load_daily_and_compute_factors()
    )
    snapshot = _load_snapshot_by_date()
    backtest = _load_backtest_daily_basic_by_date()
    spot = _load_spot_by_date()

    available_dates = set(daily.keys()) | set(snapshot.keys()) | set(backtest.keys()) | set(spot.keys())
    if date_filter:
        available_dates = {d for d in available_dates if d == date_filter}

    for d0 in sorted(available_dates):
        # 1) 先填 daily（包含基础价量 + 日线因子）
        for symbol, vals in daily.get(d0, {}).items():
            row = _upsert(out, d0, symbol)
            row.update(vals)

        # 2) 合并 snapshot（市值估值/状态）
        for symbol, vals in snapshot.get(d0, {}).items():
            row = _upsert(out, d0, symbol)
            row.update(vals)

        # 3) backtest daily_basic 作为补充（缺失才回填）
        for symbol, vals in backtest.get(d0, {}).items():
            row = _upsert(out, d0, symbol)
            for k, v in vals.items():
                if k not in row or row.get(k) in (0, 0.0, None, ""):
                    row[k] = v

        # 4) spot 覆盖当日行情（价格/涨跌幅/量额/name）
        for symbol, vals in spot.get(d0, {}).items():
            row = _upsert(out, d0, symbol)
            row.update(vals)
            spot_close = float(vals.get("close", 0) or vals.get("price", 0) or 0)
            row_close = float(row.get("close", 0) or 0)
            if spot_close > 0 and row_close <= 0:
                row["close"] = spot_close

    for d0, per_symbol in out.items():
        for row in per_symbol.values():
            if not isinstance(row, dict):
                continue
            close = float(row.get("close", 0) or 0)
            price = float(row.get("price", 0) or 0)
            if close <= 0 and price > 0:
                row["close"] = price

    date_count = len(out)
    symbol_rows = sum(len(v) for v in out.values())
    factor_coverage = compute_factor_coverage(out)
    source_status_by_date: Dict[str, Dict[str, Any]] = {}
    for d0 in sorted(available_dates):
        source_status_by_date[d0] = {
            "daily_rows": len(daily.get(d0, {})),
            "snapshot_rows": len(snapshot.get(d0, {})),
            "backtest_rows": len(backtest.get(d0, {})),
            "spot_rows": len(spot.get(d0, {})),
        }
    # success 定义：至少产出 1 个交易日且至少有 1 行 symbol 数据。
    merge_success = bool(date_count > 0 and symbol_rows > 0)
    merge_message = "ok" if merge_success else "no_merged_rows"
    merge_reason = _build_merge_reason_text(date_count, symbol_rows, source_status_by_date, date_filter)
    payload: Dict[str, Any] = {
        "schema_version": "2.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "merge_meta": {
            "impl_version": MERGE_IMPL_VERSION,
            "modified_marker": "20260304",
            "success": merge_success,
            "message": merge_message,
            "reason": merge_reason,
            "trade_date_filter": date_filter or "",
            "source_status_by_date": source_status_by_date,
        },
        "counts": {
            "dates": date_count,
            "symbol_rows": symbol_rows,
            "source_dates": {
                "daily": len(daily),
                "tushare_snapshot": len(snapshot),
                "backtest_daily_basic": len(backtest),
                "tushare_spot": len(spot),
            },
        },
        "factor_coverage": factor_coverage,
        "data": out,
    }

    output_dir = _bundle_dir()
    if not out:
        logger.warning(
            "cache_consolidation: 本次未产出有效 merged 数据（trade_date_filter=%s），跳过写入 latest",
            date_filter or "",
        )
        if date_filter:
            legacy_day_file = output_dir / f"{date_filter}.json"
            if legacy_day_file.exists():
                legacy_payload = _load_json(legacy_day_file)
                if isinstance(legacy_payload, dict):
                    legacy_counts = legacy_payload.get("counts") if isinstance(legacy_payload.get("counts"), dict) else {}
                    legacy_rows = int(legacy_counts.get("symbol_rows") or 0)
                    legacy_payload["merge_meta"] = {
                        "impl_version": MERGE_IMPL_VERSION,
                        "modified_marker": "20260304",
                        "success": legacy_rows > 0,
                        "message": "legacy_preserved_no_source",
                        "reason": merge_reason,
                        "trade_date_filter": date_filter,
                        "source_status": source_status_by_date.get(date_filter, {}),
                    }
                    legacy_day_file.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")
                    logger.warning(
                        "cache_consolidation: 源缓存缺失，已保留并标记历史 all_in_one 文件 %s（symbol_rows=%d）",
                        legacy_day_file,
                        legacy_rows,
                    )
        if not merge_success:
            logger.warning(
                "cache_consolidation: 合并结果异常 success=false message=%s reason=%s",
                merge_message,
                merge_reason,
            )
        return payload

    latest_date = max(out.keys()) if out else datetime.now().strftime("%Y%m%d")
    dated_file = output_dir / f"{latest_date}.json"
    latest_file = output_dir / "latest.json"
    body = json.dumps(payload, ensure_ascii=False)
    dated_file.write_text(body, encoding="utf-8")
    latest_file.write_text(body, encoding="utf-8")
    # 单层结构：按日文件写入 all_in_one 根目录，不再写 all_in_one/dates。
    for d0, per_symbol in out.items():
        day_source_status = source_status_by_date.get(d0, {})
        day_success = len(per_symbol) > 0
        day_payload = {
            "schema_version": "2.0",
            "generated_at": payload["generated_at"],
            "trade_date": d0,
            "merge_meta": {
                "impl_version": MERGE_IMPL_VERSION,
                "modified_marker": "20260304",
                "success": day_success,
                "message": "ok" if day_success else "no_rows_for_trade_date",
                "trade_date_filter": d0,
                "source_status": day_source_status,
            },
            "counts": {"dates": 1, "symbol_rows": len(per_symbol)},
            "factor_coverage": {d0: factor_coverage.get(d0, {})},
            "data": {d0: per_symbol},
        }
        (output_dir / f"{d0}.json").write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "cache_consolidation: merged cache written -> %s (dates=%d symbol_rows=%d)",
        latest_file,
        date_count,
        symbol_rows,
    )
    if not merge_success:
        logger.warning(
            "cache_consolidation: 合并结果异常 success=false message=%s reason=%s",
            merge_message,
            merge_reason,
        )
    elif date_filter:
        s = source_status_by_date.get(date_filter) or {}
        if int(s.get("daily_rows", 0)) <= 0 or int(s.get("spot_rows", 0)) <= 0 or int(s.get("snapshot_rows", 0)) <= 0:
            logger.warning(
                "cache_consolidation: 单日合并成功但源数据不完整 trade_date=%s daily=%d snapshot=%d backtest=%d spot=%d",
                date_filter,
                int(s.get("daily_rows", 0)),
                int(s.get("snapshot_rows", 0)),
                int(s.get("backtest_rows", 0)),
                int(s.get("spot_rows", 0)),
            )
    return payload


def cleanup_cache_temp_files() -> Dict[str, Any]:
    """
    清理缓存目录中的临时/中间文件，不删除原始数据缓存。
    仅清理：
    - *.tmp.*
    - *.lock
    """
    root = _cache_root()
    removed = 0
    failed = 0
    removed_files: List[str] = []
    if not root.exists():
        return {"removed": 0, "failed": 0, "files": []}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if ".tmp." not in name and not name.endswith(".lock"):
            continue
        try:
            path.unlink()
            removed += 1
            if len(removed_files) < 200:
                removed_files.append(str(path))
        except OSError:
            failed += 1
    logger.info("cache_consolidation: temp files cleanup done (removed=%d failed=%d)", removed, failed)
    return {"removed": removed, "failed": failed, "files": removed_files}


def cleanup_source_cache_dirs() -> Dict[str, Any]:
    """
    清理原始分散缓存目录，仅保留 all_in_one 统一缓存。
    目标目录：
    - .cache/tushare_spot
    - .cache/tushare_snapshot
    - .cache/backtest
    - .cache/daily
    """
    root = _cache_root()
    targets = [
        root / "tushare_spot",
        root / "tushare_snapshot",
        root / "backtest",
        root / "daily",
    ]
    removed_dirs: List[str] = []
    removed_files = 0
    failed = 0
    for t in targets:
        if not t.exists():
            continue
        if root not in t.parents:
            failed += 1
            continue
        for p in sorted(t.rglob("*"), reverse=True):
            if p.is_file():
                try:
                    p.unlink()
                    removed_files += 1
                except OSError:
                    failed += 1
        for p in sorted(t.rglob("*"), reverse=True):
            if p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    failed += 1
        try:
            t.rmdir()
            removed_dirs.append(str(t))
        except OSError:
            failed += 1
    logger.info(
        "cache_consolidation: source cache dirs cleanup done (removed_dirs=%d removed_files=%d failed=%d)",
        len(removed_dirs),
        removed_files,
        failed,
    )
    return {
        "removed_dirs": len(removed_dirs),
        "removed_files": removed_files,
        "failed": failed,
        "dirs": removed_dirs,
    }


def read_latest_consolidated_cache() -> Optional[Dict[str, Any]]:
    latest_file = _bundle_dir() / "latest.json"
    if not latest_file.exists():
        return None
    payload = _load_json(latest_file)
    return payload if isinstance(payload, dict) else None


def backfill_merge_meta_for_existing_all_in_one(force: bool = False) -> Dict[str, int]:
    """
    为 all_in_one 历史文件补齐 merge_meta（仅补缺失，不覆盖已有值）。
    用于平滑升级旧缓存格式，避免仅当日文件有状态信息。
    """
    marker = _bundle_dir() / ".merge_meta_backfilled_20260304.marker"
    if marker.exists() and not force:
        return {"updated": 0, "skipped": 0, "failed": 0}

    output_dir = _bundle_dir()
    updated = 0
    skipped = 0
    failed = 0
    for p in output_dir.glob("*.json"):
        if p.name == "full_sync_status.json":
            continue
        payload = _load_json(p)
        if not isinstance(payload, dict):
            failed += 1
            continue
        if isinstance(payload.get("merge_meta"), dict) and payload.get("merge_meta"):
            skipped += 1
            continue
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        symbol_rows = int(counts.get("symbol_rows") or 0)
        dates = int(counts.get("dates") or 0)
        trade_date = str(payload.get("trade_date") or "")
        if not trade_date:
            stem = p.stem
            trade_date = stem if len(stem) == 8 and stem.isdigit() else ""
        payload["merge_meta"] = {
            "impl_version": MERGE_IMPL_VERSION,
            "modified_marker": "20260304",
            "success": bool(symbol_rows > 0 or dates > 0),
            "message": "legacy_backfilled",
            "reason": "历史文件补齐 merge_meta",
            "trade_date_filter": trade_date,
        }
        try:
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            updated += 1
        except OSError:
            failed += 1
    logger.info(
        "cache_consolidation: merge_meta backfill done (updated=%d skipped=%d failed=%d)",
        updated,
        skipped,
        failed,
    )
    if failed == 0:
        try:
            marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass
    return {"updated": updated, "skipped": skipped, "failed": failed}


def trigger_backfill_merge_meta_async(reason: str = "", force: bool = False) -> Dict[str, Any]:
    """
    后台触发历史 merge_meta 补齐。
    - 若已有任务在跑，返回 started=False, running=True
    - 否则启动后台线程并返回 started=True
    """
    return {
        "started": False,
        "running": False,
        "storage": "postgresql",
        "reason": reason,
        "message": "No file-cache metadata backfill is required.",
    }

    global _meta_backfill_running
    with _meta_backfill_lock:
        if _meta_backfill_running:
            return {"started": False, "running": True, "reason": reason}
        _meta_backfill_running = True

    def _run() -> None:
        global _meta_backfill_running
        try:
            result = backfill_merge_meta_for_existing_all_in_one(force=force)
            logger.info(
                "cache_consolidation: async merge_meta backfill done reason=%s updated=%d skipped=%d failed=%d",
                reason,
                int(result.get("updated", 0)),
                int(result.get("skipped", 0)),
                int(result.get("failed", 0)),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cache_consolidation: async merge_meta backfill failed reason=%s err=%s", reason, str(e)[:160])
        finally:
            with _meta_backfill_lock:
                _meta_backfill_running = False

    threading.Thread(target=_run, daemon=True, name="merge-meta-backfill").start()
    return {"started": True, "running": True, "reason": reason}


def _apply_symbol_filter(payload: Dict[str, Any], symbols: Optional[List[str]]) -> Dict[str, Any]:
    if not symbols:
        return payload
    wanted = {_normalize_symbol(s) for s in symbols if _normalize_symbol(s)}
    if not wanted:
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    filtered_data: Dict[str, Dict[str, Any]] = {}
    for d0, per_symbol in data.items():
        if not isinstance(per_symbol, dict):
            continue
        picked = {k: v for k, v in per_symbol.items() if _normalize_symbol(k) in wanted}
        if picked:
            filtered_data[d0] = picked
    out = dict(payload)
    out["data"] = filtered_data
    out["counts"] = {
        **(payload.get("counts") if isinstance(payload.get("counts"), dict) else {}),
        "dates": len(filtered_data),
        "symbol_rows": sum(len(v) for v in filtered_data.values()),
    }
    return out


def _read_consolidated_postgres(date_ymd: str, symbols: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    """Build the all-in-one compatibility payload from PostgreSQL snapshots."""
    try:
        from app.data_platform.db import _conn
        with _conn() as conn:
            if not date_ymd:
                row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM dc_stock_daily_snapshot").fetchone()
                date_ymd = str(dict(row).get("trade_date") or "") if row else ""
            if not date_ymd:
                return None
            rows = conn.execute(
                """SELECT symbol,name,close,change_pct,volume,turnover,total_mv,circ_mv,pe_ttm,pb,
                          turnover_rate,is_suspended,up_limit,down_limit,data_quality_score
                   FROM dc_stock_daily_snapshot WHERE trade_date=? ORDER BY symbol""",
                (date_ymd,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL all-in-one read failed: %s", str(exc)[:160])
        return None
    wanted = {_normalize_symbol(item) for item in (symbols or []) if _normalize_symbol(item)}
    day: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        item = dict(raw)
        symbol = _normalize_symbol(str(item.get("symbol") or ""))
        if not symbol or (wanted and symbol not in wanted):
            continue
        row = {
            "trade_date": date_ymd, "symbol": symbol, "name": str(item.get("name") or ""),
            "price": float(item.get("close") or 0), "close": float(item.get("close") or 0),
            "change_pct": float(item.get("change_pct") or 0), "volume": float(item.get("volume") or 0),
            "turnover": float(item.get("turnover") or 0), "float_mv": float(item.get("circ_mv") or 0),
            "total_mv": float(item.get("total_mv") or 0), "pe_ttm": float(item.get("pe_ttm") or 0),
            "pb": float(item.get("pb") or 0), "turnover_rate": float(item.get("turnover_rate") or 0),
            "is_suspended": bool(item.get("is_suspended")), "up_limit": float(item.get("up_limit") or 0),
            "down_limit": float(item.get("down_limit") or 0), "data_quality_score": float(item.get("data_quality_score") or 0),
        }
        day[symbol] = row
    if not day:
        return None
    data = {date_ymd: day}
    return {
        "schema_version": "postgresql-1", "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": date_ymd, "counts": {"dates": 1, "symbol_rows": len(day)},
        "factor_coverage": compute_factor_coverage(data), "data": data,
    }


def read_consolidated_cache(date: Optional[str] = None, symbols: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    读取合并缓存：
    - 指定 date 时优先读取 all_in_one/<date>.json（单层结构）
    - 可选 symbols 过滤
    """
    date_ymd = _normalize_ymd(date or "")
    postgres_payload = _read_consolidated_postgres(date_ymd, symbols)
    if postgres_payload is not None:
        return postgres_payload
    # Production is PostgreSQL-only.  Legacy cache bundles must never become
    # an implicit alternate source of truth when a historical day is absent.
    return None
    payload: Optional[Dict[str, Any]] = None
    if date_ymd:
        day_file = _bundle_dir() / f"{date_ymd}.json"
        if day_file.exists():
            raw = _load_json(day_file)
            payload = raw if isinstance(raw, dict) else None
    if payload is None:
        payload = read_latest_consolidated_cache()
        if payload is None:
            return None
        if date_ymd:
            data = payload.get("data")
            if not isinstance(data, dict):
                return None
            per_day = data.get(date_ymd)
            if not isinstance(per_day, dict):
                return None
            payload = {
                "schema_version": payload.get("schema_version", "2.0"),
                "generated_at": payload.get("generated_at"),
                "trade_date": date_ymd,
                "counts": {"dates": 1, "symbol_rows": len(per_day)},
                "data": {date_ymd: per_day},
            }
    return _apply_symbol_filter(payload, symbols)


def has_consolidated_for_date(date_ymd: str) -> bool:
    """
    判断指定交易日的合并缓存是否已存在（用于启动/按需更新，不再每次全量重建）。
    """
    ymd = _normalize_ymd(date_ymd)
    if not ymd:
        return False
    try:
        from app.data_platform.db import _conn
        with _conn() as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM dc_stock_daily_snapshot WHERE trade_date=? LIMIT 1", (ymd,)
            ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL consolidated-date check failed: %s", str(exc)[:120])
        return False


def _latest_source_mtime_for_date(date_ymd: str) -> Optional[float]:
    """
    计算指定交易日对应源缓存文件的最新 mtime。
    若该日期在源缓存中不存在则返回 None。
    """
    ymd = _normalize_ymd(date_ymd)
    if not ymd:
        return None
    mtimes: List[float] = []

    spot_file = _spot_dir() / f"spot_{ymd}.json"
    if spot_file.exists():
        mtimes.append(spot_file.stat().st_mtime)

    snapshot_file = _snapshot_dir() / f"{ymd}.json"
    if snapshot_file.exists():
        mtimes.append(snapshot_file.stat().st_mtime)

    backtest_file = _backtest_dir() / f"daily_basic_{ymd}.json"
    if backtest_file.exists():
        mtimes.append(backtest_file.stat().st_mtime)

    dd = _daily_dir()
    if dd.exists():
        for p in dd.glob("*_*_*.json"):
            parts = p.stem.split("_")
            if len(parts) < 3:
                continue
            end_ymd = parts[2]
            if len(end_ymd) == 8 and end_ymd.isdigit() and end_ymd >= ymd:
                mtimes.append(p.stat().st_mtime)

    if not mtimes:
        return None
    return max(mtimes)


def should_rebuild_consolidated_for_date(date_ymd: str) -> bool:
    """
    判断指定日期 all-in-one 是否需要重建：
    - 文件不存在：需要重建
    - 源缓存较新：需要重建
    """
    ymd = _normalize_ymd(date_ymd)
    if not ymd:
        return False
    target = _consolidated_day_file(ymd)
    if not target.exists():
        return True
    latest_source_mtime = _latest_source_mtime_for_date(ymd)
    if latest_source_mtime is None:
        return False
    return latest_source_mtime > target.stat().st_mtime


# ---------- full-sync 历史全量合并（异步任务 + 断点续跑） ----------


def _full_sync_status_path() -> Path:
    return _bundle_dir() / "full_sync_status.json"


def list_materialized_all_in_one_dates() -> List[str]:
    """已落盘的 all-in-one 按日文件（YYYYMMDD）。"""
    dates: set[str] = set()
    base = _bundle_dir()
    for p in base.glob("*.json"):
        stem = p.stem
        if stem in {"latest", "init_status", "full_sync_status"}:
            continue
        ymd = _normalize_ymd(stem)
        if ymd:
            dates.add(ymd)
    latest = read_latest_consolidated_cache()
    if isinstance(latest, dict):
        data = latest.get("data")
        if isinstance(data, dict):
            for key in data.keys():
                ymd = _normalize_ymd(str(key))
                if ymd:
                    dates.add(ymd)
    return sorted(dates)


def list_daily_cache_trade_dates() -> List[str]:
    """从 .cache/daily 日线窗口提取可用交易日（优先读指数样本，避免全量扫描）。"""
    d = _daily_dir()
    if not d.exists():
        return []
    for symbol in ("000001", "399001", "000300"):
        candidates = sorted(d.glob(f"{symbol}_*_*.json"))
        for p in candidates:
            rows = _load_json(p)
            if not isinstance(rows, list) or not rows:
                continue
            out: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ymd = _normalize_ymd(str(row.get("date", "")))
                if ymd:
                    out.add(ymd)
            if out:
                return sorted(out)
    daily = _load_daily_and_compute_factors()
    return sorted(daily.keys())


def list_available_trade_dates() -> List[str]:
    """可用于合并/量化任务的交易日：已落盘 all-in-one + daily 可展开日期。"""
    global _available_trade_dates_cache
    now = time.monotonic()
    with _available_trade_dates_lock:
        cached_at, cached_dates = _available_trade_dates_cache
        if cached_dates and now - cached_at <= _AVAILABLE_TRADE_DATES_TTL_SECONDS:
            return list(cached_dates)
    try:
        from app.data_platform.db import _conn
        with _conn() as conn:
            rows = conn.execute("SELECT trade_date FROM dc_available_trade_dates ORDER BY trade_date").fetchall()
            dates = [
                value for value in (str(dict(row).get("trade_date") or "") for row in rows)
                if _normalize_ymd(value)
            ]
            if not dates:
                rows = conn.execute(
                    """SELECT cal.cal_date AS trade_date
                       FROM dc_trade_calendar cal
                       WHERE cal.is_open=1
                         AND EXISTS (
                           SELECT 1 FROM dc_stock_daily_snapshot s
                           WHERE s.trade_date=cal.cal_date
                           LIMIT 1
                         )
                       ORDER BY cal.cal_date"""
                ).fetchall()
                dates = [
                    value for value in (str(dict(row).get("trade_date") or "") for row in rows)
                    if _normalize_ymd(value)
                ]
                stamp = datetime.now().isoformat(timespec="seconds")
                for value in dates:
                    conn.execute(
                        """INSERT INTO dc_available_trade_dates(trade_date,source_code,available_time,updated_at)
                           VALUES(?,?,?,?)
                           ON CONFLICT(trade_date) DO UPDATE SET
                             source_code=excluded.source_code,
                             available_time=excluded.available_time,
                             updated_at=excluded.updated_at""",
                        (value, "dc_stock_daily_snapshot", f"{value[:4]}-{value[4:6]}-{value[6:8]}T15:00:00", stamp),
                    )
            if not dates:
                rows = conn.execute(
                    "SELECT DISTINCT trade_date FROM dc_tushare_raw_records WHERE api_name='daily' ORDER BY trade_date"
                ).fetchall()
                dates = [
                    value for value in (str(dict(row).get("trade_date") or "") for row in rows)
                    if _normalize_ymd(value)
                ]
        with _available_trade_dates_lock:
            _available_trade_dates_cache = (time.monotonic(), list(dates))
        return dates
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL available-trade-dates read failed: %s", str(exc)[:120])
        return []


def _collect_available_dates() -> List[str]:
    """收集可执行 full-sync 的交易日（YYYYMMDD）。"""
    return list_available_trade_dates()


def get_full_sync_status() -> Dict[str, Any]:
    """读取 full-sync 任务状态（供 GET /api/init/full-sync/status）."""
    path = _full_sync_status_path()
    if not path.exists():
        return {"status": "idle", "message": "从未运行过 full-sync"}
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return {"status": "idle", "message": "状态文件无效"}
    return raw


def _write_full_sync_status(data: Dict[str, Any]) -> None:
    path = _full_sync_status_path()
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("cache_consolidation: 写入 full_sync 状态失败 %s", e)


def run_full_sync_background(skip_existing: bool = True, cleanup_sources: bool = True) -> bool:
    """
    在后台线程中执行历史全量合并（按日合并，可断点续跑）。
    若已有任务在跑则返回 False；否则启动并返回 True。
    """
    global _full_sync_running
    with _full_sync_lock:
        if _full_sync_running:
            return False
        _full_sync_running = True

    def _run() -> None:
        global _full_sync_running
        try:
            available = _collect_available_dates()
            total = len(available)
            _write_full_sync_status({
                "status": "running",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "total_dates": total,
                "processed_dates": 0,
                "current_date": "",
                "error": None,
            })
            processed = 0
            for i, ymd in enumerate(available):
                if skip_existing and has_consolidated_for_date(ymd):
                    processed += 1
                    if i % 100 == 0 or i == total - 1:
                        _write_full_sync_status({
                            "status": "running",
                            "started_at": get_full_sync_status().get("started_at", ""),
                            "total_dates": total,
                            "processed_dates": processed,
                            "current_date": ymd,
                            "error": None,
                        })
                    continue
                try:
                    build_consolidated_cache_bundle(trade_date=ymd)
                    processed += 1
                except Exception as e:
                    logger.warning("cache_consolidation: full-sync 单日 %s 失败 %s", ymd, e)
                    _write_full_sync_status({
                        "status": "failed",
                        "started_at": get_full_sync_status().get("started_at", ""),
                        "total_dates": total,
                        "processed_dates": processed,
                        "current_date": ymd,
                        "error": str(e)[:500],
                    })
                    break
                if (i + 1) % 50 == 0 or i == total - 1:
                    _write_full_sync_status({
                        "status": "running",
                        "started_at": get_full_sync_status().get("started_at", ""),
                        "total_dates": total,
                        "processed_dates": processed,
                        "current_date": ymd,
                        "error": None,
                    })
            else:
                _write_full_sync_status({
                    "status": "completed",
                    "started_at": get_full_sync_status().get("started_at", ""),
                    "total_dates": total,
                    "processed_dates": processed,
                    "current_date": available[-1] if available else "",
                    "error": None,
                })
                if cleanup_sources:
                    try:
                        cleanup_source_cache_dirs()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("cache_consolidation: full-sync 后清理源目录失败 %s", e)
                logger.info("cache_consolidation: full-sync 完成 total=%d processed=%d", total, processed)
        finally:
            with _full_sync_lock:
                _full_sync_running = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True
