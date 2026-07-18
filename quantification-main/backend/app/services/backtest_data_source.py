from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.integrations.tushare_client import get_tushare_client
from app.data_platform.db import _conn

logger = logging.getLogger("quant.backtest_data_source")


@dataclass
class SnapshotRow:
    symbol: str
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    change_pct: float
    volume: float
    turnover: float
    name: str = ""


@dataclass
class HistBar:
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class BacktestDataSource:
    def __init__(self) -> None:
        # backend/app/services -> backend/app -> backend -> repo
        self._repo_root = Path(__file__).resolve().parents[3]
        self._data_root = self._repo_root / ".cache" / "data"
        self._daily_snapshot_dir = self._data_root / "daily_basic" / "2026"
        self._full_k_dir = self._data_root / "全部日k" / "全部日k"
        # 用户可长期沉淀的本地日截面目录（优先级高于 .cache）
        self._daily_basic_data_dir = self._data_root / "daily_basic"
        self._daily_basic_data_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = self._repo_root / "backend" / ".cache" / "backtest"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._all_in_one_dir = self._repo_root / "backend" / ".cache" / "all_in_one"

        self._symbol_file_index: Dict[str, Path] = {}
        self._hist_cache: Dict[str, List[HistBar]] = {}
        self._snapshot_cache: Dict[str, List[SnapshotRow]] = {}
        self._daily_basic_cache: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.data_warnings: List[str] = []
        self.fallback_events: List[Dict[str, str]] = []

    def _postgres_trade_dates(self, start: str, end: str) -> List[str]:
        try:
            with _conn() as conn:
                rows = conn.execute(
                    """SELECT trade_date FROM dc_available_trade_dates
                       WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date""",
                    (start.replace("-", ""), end.replace("-", "")),
                ).fetchall()
            return [self._fmt_date(str(dict(row).get("trade_date") or "")) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest calendar read failed: %s", str(exc)[:160])
            return []

    def _postgres_daily_rows(self, ymd: str, symbol: str = "") -> List[dict]:
        try:
            sql = "SELECT ts_code,trade_date,payload_json FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?"
            params: tuple = (ymd,)
            if symbol:
                code = str(symbol).strip().split(".", 1)[0].zfill(6)
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                sql += " AND ts_code=?"
                params = (ymd, f"{code}{suffix}")
            with _conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest daily read failed: %s", str(exc)[:160])
            return []

    def preload_histories(self, symbols: List[str], *, end_date: str | None = None, lookback: int | None = None) -> None:
        """Batch-load PostgreSQL history to avoid one query per factor row."""
        wanted = []
        for raw in symbols:
            code = str(raw or "").strip().split(".", 1)[0].zfill(6)
            if not code or code in self._hist_cache:
                continue
            suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
            wanted.append((code, f"{code}{suffix}"))
        if not wanted:
            return
        grouped: Dict[str, List[HistBar]] = {code: [] for code, _ in wanted}
        end_ymd = str(end_date or "").replace("-", "")[:8]
        date_clause = ""
        date_params: tuple[str, ...] = ()
        if len(end_ymd) == 8 and end_ymd.isdigit():
            limit_days = max(1, int(lookback or 0) + 5) if lookback else 0
            start_ymd = ""
            if limit_days:
                with _conn() as conn:
                    row = conn.execute(
                        """SELECT MIN(trade_date) AS start_date FROM (
                             SELECT trade_date FROM dc_available_trade_dates
                             WHERE trade_date<=?
                             ORDER BY trade_date DESC LIMIT ?
                           ) d""",
                        (end_ymd, limit_days),
                    ).fetchone()
                start_ymd = str(dict(row).get("start_date") or "") if row else ""
            if len(start_ymd) == 8 and start_ymd.isdigit():
                date_clause = " AND trade_date BETWEEN ? AND ?"
                date_params = (start_ymd, end_ymd)
            else:
                date_clause = " AND trade_date<=?"
                date_params = (end_ymd,)
        try:
            for offset in range(0, len(wanted), 500):
                batch = wanted[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                with _conn() as conn:
                    rows = conn.execute(
                        f"""SELECT ts_code,trade_date,close,volume,turnover FROM dc_stock_daily_snapshot
                            WHERE ts_code IN ({placeholders}){date_clause}
                            ORDER BY ts_code,trade_date""",
                        tuple(ts_code for _, ts_code in batch) + date_params,
                    ).fetchall()
                for raw_row in rows:
                    item = dict(raw_row)
                    code = str(item.get("ts_code") or "").split(".", 1)[0].zfill(6)
                    if code in grouped:
                        grouped[code].append(HistBar(
                            date=self._fmt_date(str(item.get("trade_date") or "")),
                            open=float(item.get("close") or 0), high=float(item.get("close") or 0),
                            low=float(item.get("close") or 0), close=float(item.get("close") or 0),
                            volume=float(item.get("volume") or 0), turnover=float(item.get("turnover") or 0),
                        ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest history preload failed: %s", str(exc)[:160])
            return
        self._hist_cache.update(grouped)

    def _daily_basic_data_path_json(self, ymd: str) -> Path:
        return self._daily_basic_data_dir / f"{ymd}.json"

    def _daily_basic_data_path_csv(self, ymd: str) -> Path:
        return self._daily_basic_data_dir / f"{ymd}.csv"

    def _load_daily_basic_from_data_dir(self, ymd: str) -> Dict[str, Dict[str, float]]:
        """
        从 .cache/data/daily_basic 读取用户沉淀的本地截面数据。
        兼容 json/csv 两种格式。
        """
        json_path = self._daily_basic_data_path_json(ymd)
        if json_path.exists():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass

        csv_path = self._daily_basic_data_path_csv(ymd)
        if not csv_path.exists():
            return {}
        out: Dict[str, Dict[str, float]] = {}
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    ts_code = str(r.get("ts_code", "")).strip()
                    symbol = ts_code.split(".")[0].zfill(6) if ts_code else str(r.get("symbol", "")).strip().zfill(6)
                    if not symbol:
                        continue
                    out[symbol] = {
                        "total_mv": float(r.get("total_mv", 0) or 0),
                        "float_mv": float(r.get("circ_mv", r.get("float_mv", 0)) or 0),
                        "pe_ttm": float(r.get("pe_ttm", 0) or 0),
                        "pb": float(r.get("pb", 0) or 0),
                        "turnover_rate": float(r.get("turnover_rate", 0) or 0),
                    }
        except Exception:
            return {}
        return out

    def _save_daily_basic_to_data_dir(
        self, ymd: str, out: Dict[str, Dict[str, float]], ts_code_by_symbol: Dict[str, str]
    ) -> None:
        """
        将从 TuShare 拉取的 daily_basic 同步落地到 .cache/data/daily_basic，后续优先本地复用。
        """
        if not out:
            return
        try:
            self._daily_basic_data_path_json(ymd).write_text(
                json.dumps(out, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

        try:
            with self._daily_basic_data_path_csv(ymd).open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["ts_code", "trade_date", "total_mv", "circ_mv", "pe_ttm", "pb", "turnover_rate"])
                for symbol, vals in out.items():
                    writer.writerow(
                        [
                            ts_code_by_symbol.get(symbol, f"{symbol}.SZ"),
                            ymd,
                            vals.get("total_mv", 0),
                            vals.get("float_mv", 0),
                            vals.get("pe_ttm", 0),
                            vals.get("pb", 0),
                            vals.get("turnover_rate", 0),
                        ]
                    )
        except Exception:
            pass

    def _load_latest_daily_basic_cache(self, target_ymd: str) -> Dict[str, Dict[str, float]]:
        """
        当某日 daily_basic 拉取失败时，回退到最近一个可用缓存（<= target_ymd）。
        """
        candidates = sorted(self._cache_dir.glob("daily_basic_*.json"), reverse=True)
        for p in candidates:
            stem = p.stem.replace("daily_basic_", "")
            if len(stem) != 8 or not stem.isdigit():
                continue
            if stem > target_ymd:
                continue
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload:
                    logger.warning(
                        "backtest daily_basic 使用历史缓存回退: target=%s fallback=%s rows=%d",
                        target_ymd,
                        stem,
                        len(payload),
                    )
                    self.fallback_events.append(
                        {
                            "type": "daily_basic_fallback",
                            "target_date": target_ymd,
                            "fallback_date": stem,
                            "rows": str(len(payload)),
                        }
                    )
                    self.data_warnings.append(
                        f"{target_ymd} daily_basic 使用 {stem} 的历史缓存回退；市值/估值过滤和打分可能存在偏差。"
                    )
                    return payload
            except Exception:
                continue
        self.data_warnings.append(
            f"{target_ymd} daily_basic 不可用且没有可回退缓存；市值/估值因子不完整。"
        )
        return {}

    @staticmethod
    def _fmt_date(ymd: str) -> str:
        s = str(ymd).replace("-", "")[:8]
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return ymd

    def _build_symbol_index(self) -> None:
        if self._symbol_file_index:
            return
        if not self._full_k_dir.exists():
            return
        for p in self._full_k_dir.glob("*.csv"):
            stem = p.stem.lower()
            code = stem[-6:] if len(stem) >= 6 else stem
            if code.isdigit():
                self._symbol_file_index[code] = p

    def get_available_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """
        优先使用 .cache/data/daily_basic/2026 按日快照作为回测交易日；若为空则退化为 000001 的历史日期。
        """
        start = self._fmt_date(start_date)
        end = self._fmt_date(end_date)
        postgres_dates = self._postgres_trade_dates(start, end)
        if not postgres_dates:
            self.data_warnings.append("PostgreSQL 中没有覆盖该区间的日线数据。")
        return postgres_dates
        out: List[str] = []
        if self._daily_snapshot_dir.exists():
            for p in self._daily_snapshot_dir.glob("*.csv"):
                ymd = p.stem
                if len(ymd) != 8 or not ymd.isdigit():
                    continue
                d = self._fmt_date(ymd)
                if start <= d <= end:
                    out.append(d)
        out = sorted(set(out))
        if out:
            return out

        if self._all_in_one_dir.exists():
            for p in self._all_in_one_dir.glob("*.json"):
                ymd = p.stem
                if len(ymd) != 8 or not ymd.isdigit():
                    continue
                d = self._fmt_date(ymd)
                if start <= d <= end:
                    out.append(d)
        out = sorted(set(out))
        if out:
            return out

        # 兜底：用 000001 的历史 K 线日期做交易日历
        bars = self.get_symbol_history("000001")
        return [b.date for b in bars if start <= b.date <= end]

    def load_snapshot(self, date_ymd: str) -> List[SnapshotRow]:
        date_iso = self._fmt_date(date_ymd)
        if date_iso in self._snapshot_cache:
            return self._snapshot_cache[date_iso]

        ymd = date_iso.replace("-", "")
        postgres_rows = self._postgres_daily_rows(ymd)
        if postgres_rows:
            rows: List[SnapshotRow] = []
            for raw in postgres_rows:
                payload = json.loads(str(raw.get("payload_json") or "{}"))
                ts_code = str(raw.get("ts_code") or payload.get("ts_code") or "")
                symbol = ts_code.split(".", 1)[0].zfill(6)
                if not symbol:
                    continue
                rows.append(SnapshotRow(
                    symbol=symbol, date=self._fmt_date(str(payload.get("trade_date") or ymd)),
                    open=float(payload.get("open") or 0), high=float(payload.get("high") or 0),
                    low=float(payload.get("low") or 0), close=float(payload.get("close") or 0),
                    prev_close=float(payload.get("pre_close") or 0), change_pct=float(payload.get("pct_chg") or 0),
                    volume=float(payload.get("vol") or 0), turnover=float(payload.get("amount") or 0),
                ))
            self._snapshot_cache[date_iso] = rows
            return rows
        self.data_warnings.append(f"{ymd} PostgreSQL 快照不可用。")
        self._snapshot_cache[date_iso] = []
        return []
        local_path = self._daily_snapshot_dir / f"{ymd}.csv"
        if local_path.exists():
            rows = self._load_snapshot_csv(local_path)
            self._snapshot_cache[date_iso] = rows
            return rows

        # 本地无按日快照时，尝试 TuShare daily 补齐
        cache_path = self._cache_dir / f"snapshot_{ymd}.json"
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                rows = [SnapshotRow(**x) for x in payload]
                self._snapshot_cache[date_iso] = rows
                return rows
            except Exception:
                pass

        all_in_one_path = self._all_in_one_dir / f"{ymd}.json"
        if all_in_one_path.exists():
            try:
                payload = json.loads(all_in_one_path.read_text(encoding="utf-8"))
                day_data = ((payload.get("data") or {}).get(ymd) or {}) if isinstance(payload, dict) else {}
                rows = []
                for symbol, vals in day_data.items():
                    if not isinstance(vals, dict):
                        continue
                    rows.append(
                        SnapshotRow(
                            symbol=str(vals.get("symbol") or symbol).zfill(6),
                            date=self._fmt_date(str(vals.get("trade_date") or ymd)),
                            open=float(vals.get("open", vals.get("price", 0)) or 0),
                            high=float(vals.get("high", vals.get("close", 0)) or 0),
                            low=float(vals.get("low", vals.get("close", 0)) or 0),
                            close=float(vals.get("close", vals.get("price", 0)) or 0),
                            prev_close=float(vals.get("prev_close", 0) or 0),
                            change_pct=float(vals.get("change_pct", 0) or 0),
                            volume=float(vals.get("volume", 0) or 0),
                            turnover=float(vals.get("turnover", 0) or 0),
                            name=str(vals.get("name") or ""),
                        )
                    )
                self._snapshot_cache[date_iso] = rows
                return rows
            except Exception:
                pass

        rows = self._fetch_snapshot_from_tushare(ymd)
        try:
            cache_path.write_text(
                json.dumps([r.__dict__ for r in rows], ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        self._snapshot_cache[date_iso] = rows
        return rows

    def _load_snapshot_csv(self, path: Path) -> List[SnapshotRow]:
        out: List[SnapshotRow] = []
        def _pick(row: dict, keys: list[str], default: str = "") -> str:
            for k in keys:
                v = row.get(k)
                if v is not None and str(v).strip() != "":
                    return str(v).strip()
            return default

        def _to_float(val: str) -> float:
            try:
                return float(val or 0)
            except Exception:
                return 0.0

        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 兼容两种列名：
                # 1) 中文: 股票代码/日期/开盘价...
                # 2) 英文: code/date/open...
                symbol = _pick(row, ["股票代码", "code"]).zfill(6)
                if not symbol:
                    continue
                raw_date = _pick(row, ["日期", "date"])
                date_val = self._fmt_date(raw_date)[:10]
                open_val = _to_float(_pick(row, ["开盘价", "open"], "0"))
                high_val = _to_float(_pick(row, ["最高价", "high"], "0"))
                low_val = _to_float(_pick(row, ["最低价", "low"], "0"))
                close_val = _to_float(_pick(row, ["收盘价", "close"], "0"))
                prev_close_val = _to_float(_pick(row, ["昨收价", "pre_close"], "0"))
                change_pct_val = _to_float(_pick(row, ["涨跌幅", "pct_chg"], "0"))
                volume_val = _to_float(_pick(row, ["成交量", "vol"], "0"))
                turnover_val = _to_float(_pick(row, ["成交额", "amount"], "0"))
                out.append(
                    SnapshotRow(
                        symbol=symbol,
                        date=date_val,
                        open=open_val,
                        high=high_val,
                        low=low_val,
                        close=close_val,
                        prev_close=prev_close_val,
                        change_pct=change_pct_val,
                        volume=volume_val,
                        turnover=turnover_val,
                        name="",
                    )
                )
        return out

    def _fetch_snapshot_from_tushare(self, ymd: str) -> List[SnapshotRow]:
        client = get_tushare_client()
        df = client.daily(
            trade_date=ymd,
            fields="ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
        )
        if df is None or df.empty:
            return []
        out: List[SnapshotRow] = []
        for _, r in df.iterrows():
            ts_code = str(r.get("ts_code", ""))
            symbol = ts_code.split(".")[0].zfill(6)
            trade_date = str(r.get("trade_date", "")).strip()
            out.append(
                SnapshotRow(
                    symbol=symbol,
                    date=self._fmt_date(trade_date),
                    open=float(r.get("open", 0) or 0),
                    high=float(r.get("high", 0) or 0),
                    low=float(r.get("low", 0) or 0),
                    close=float(r.get("close", 0) or 0),
                    prev_close=float(r.get("pre_close", 0) or 0),
                    change_pct=float(r.get("pct_chg", 0) or 0),
                    volume=float(r.get("vol", 0) or 0),
                    turnover=float(r.get("amount", 0) or 0),
                    name="",
                )
            )
        return out

    def get_symbol_history(self, symbol: str) -> List[HistBar]:
        code = str(symbol).strip().zfill(6)
        if code in self._hist_cache:
            return self._hist_cache[code]

        try:
            suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
            with _conn() as conn:
                rows = conn.execute(
                    """SELECT trade_date,close,volume,turnover FROM dc_stock_daily_snapshot
                       WHERE ts_code=? ORDER BY trade_date""",
                    (f"{code}{suffix}",),
                ).fetchall()
            postgres_bars = []
            for raw in rows:
                item = dict(raw)
                postgres_bars.append(HistBar(
                    date=self._fmt_date(str(item.get("trade_date") or "")),
                    open=float(item.get("close") or 0), high=float(item.get("close") or 0),
                    low=float(item.get("close") or 0), close=float(item.get("close") or 0),
                    volume=float(item.get("volume") or 0), turnover=float(item.get("turnover") or 0),
                ))
            if postgres_bars:
                self._hist_cache[code] = postgres_bars
                return postgres_bars
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest history read failed: %s", str(exc)[:160])

        self._hist_cache[code] = []
        return []
        self._build_symbol_index()
        path = self._symbol_file_index.get(code)
        if path is None or not path.exists():
            self._hist_cache[code] = []
            return []

        rows: List[HistBar] = []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(
                    HistBar(
                        date=str(row.get("日期", ""))[:10],
                        open=float(row.get("开盘价", 0) or 0),
                        high=float(row.get("最高价", 0) or 0),
                        low=float(row.get("最低价", 0) or 0),
                        close=float(row.get("收盘价", 0) or 0),
                        volume=float(row.get("成交量", 0) or 0),
                        turnover=float(row.get("成交额", 0) or 0),
                    )
                )
        rows.sort(key=lambda x: x.date)
        self._hist_cache[code] = rows
        return rows

    def get_hist_window(self, symbol: str, end_date_iso: str, lookback: int) -> List[HistBar]:
        bars = self.get_symbol_history(symbol)
        if not bars:
            return []
        d = end_date_iso[:10]
        vals = [b for b in bars if b.date <= d]
        if lookback <= 0:
            return vals[-1:] if vals else []
        return vals[-lookback:]

    def get_price_on_date(self, symbol: str, date_iso: str, field: str = "close") -> Optional[float]:
        bars = self.get_symbol_history(symbol)
        if not bars:
            return None
        for b in reversed(bars):
            if b.date == date_iso[:10]:
                return float(getattr(b, field, 0) or 0)
            if b.date < date_iso[:10]:
                return None
        return None

    def get_daily_basic_map(self, date_ymd: str) -> Dict[str, Dict[str, float]]:
        """
        TuShare daily_basic 截面缓存，键为 6 位 symbol。
        """
        date_iso = self._fmt_date(date_ymd)
        ymd = date_iso.replace("-", "")
        if ymd in self._daily_basic_cache:
            return self._daily_basic_cache[ymd]

        try:
            with _conn() as conn:
                rows = conn.execute(
                    "SELECT ts_code,payload_json FROM dc_tushare_raw_records WHERE api_name='daily_basic' AND trade_date=?",
                    (ymd,),
                ).fetchall()
            postgres_payload: Dict[str, Dict[str, float]] = {}
            for raw in rows:
                item = dict(raw)
                payload = json.loads(str(item.get("payload_json") or "{}"))
                symbol = str(item.get("ts_code") or payload.get("ts_code") or "").split(".", 1)[0].zfill(6)
                if symbol:
                    postgres_payload[symbol] = {
                        "total_mv": float(payload.get("total_mv") or 0),
                        "float_mv": float(payload.get("circ_mv") or 0),
                        "pe_ttm": float(payload.get("pe_ttm") or 0),
                        "pb": float(payload.get("pb") or 0),
                        "turnover_rate": float(payload.get("turnover_rate") or 0),
                    }
            if postgres_payload:
                self._daily_basic_cache[ymd] = postgres_payload
                return postgres_payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest daily_basic read failed: %s", str(exc)[:160])
        self.data_warnings.append(f"{ymd} PostgreSQL daily_basic 不可用。")
        self._daily_basic_cache[ymd] = {}
        return {}

        # 1) 用户长期本地目录优先（.cache/data/daily_basic）
        data_dir_payload = self._load_daily_basic_from_data_dir(ymd)
        if data_dir_payload:
            logger.info("backtest daily_basic trade_date=%s 使用 .cache/data/daily_basic 本地文件 rows=%d", ymd, len(data_dir_payload))
            self._daily_basic_cache[ymd] = data_dir_payload
            return data_dir_payload

        # 2) 进程缓存目录（backend/.cache/backtest）
        cache_path = self._cache_dir / f"daily_basic_{ymd}.json"
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                self._daily_basic_cache[ymd] = payload
                logger.info("backtest daily_basic trade_date=%s 使用 .cache 文件 rows=%d", ymd, len(payload))
                return payload
            except Exception:
                pass

        all_in_one_path = self._all_in_one_dir / f"{ymd}.json"
        if all_in_one_path.exists():
            try:
                payload = json.loads(all_in_one_path.read_text(encoding="utf-8"))
                day_data = ((payload.get("data") or {}).get(ymd) or {}) if isinstance(payload, dict) else {}
                out = {}
                for symbol, vals in day_data.items():
                    if not isinstance(vals, dict):
                        continue
                    out[str(vals.get("symbol") or symbol).zfill(6)] = {
                        "total_mv": float(vals.get("total_mv", 0) or 0),
                        "float_mv": float(vals.get("float_mv", 0) or 0),
                        "pe_ttm": float(vals.get("pe_ttm", 0) or 0),
                        "pb": float(vals.get("pb", 0) or 0),
                        "turnover_rate": float(vals.get("turnover_rate", 0) or 0),
                    }
                if out:
                    self._daily_basic_cache[ymd] = out
                    return out
            except Exception:
                pass

        # 3) TuShare 拉取（仅在本地无数据时触发）
        client = get_tushare_client()
        try:
            df = client.daily_basic_resilient(
                trade_date=ymd,
                ts_code="",
                fields="ts_code,total_mv,circ_mv,pe_ttm,pb,turnover_rate",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "backtest daily_basic 拉取失败 trade_date=%s，尝试历史缓存回退 | %s",
                ymd,
                str(e)[:200],
            )
            fallback = self._load_latest_daily_basic_cache(ymd)
            self._daily_basic_cache[ymd] = fallback
            return fallback
        out: Dict[str, Dict[str, float]] = {}
        ts_code_by_symbol: Dict[str, str] = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                ts_code = str(r.get("ts_code", ""))
                symbol = ts_code.split(".")[0].zfill(6)
                ts_code_by_symbol[symbol] = ts_code
                out[symbol] = {
                    "total_mv": float(r.get("total_mv", 0) or 0),
                    "float_mv": float(r.get("circ_mv", 0) or 0),
                    "pe_ttm": float(r.get("pe_ttm", 0) or 0),
                    "pb": float(r.get("pb", 0) or 0),
                    "turnover_rate": float(r.get("turnover_rate", 0) or 0),
                }
        try:
            cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self._save_daily_basic_to_data_dir(ymd, out, ts_code_by_symbol)
        logger.info("backtest daily_basic trade_date=%s rows=%d", ymd, len(out))
        self._daily_basic_cache[ymd] = out
        return out

    @staticmethod
    def should_rebalance(freq: str, trade_date_iso: str, prev_rebalance_date: Optional[str]) -> bool:
        if prev_rebalance_date is None:
            return True
        if freq == "daily":
            return True
        cur = datetime.fromisoformat(trade_date_iso[:10])
        prev = datetime.fromisoformat(prev_rebalance_date[:10])
        if freq == "weekly":
            return cur.isocalendar()[1] != prev.isocalendar()[1] or cur.year != prev.year
        if freq == "monthly":
            return (cur.year, cur.month) != (prev.year, prev.month)
        return True
