from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.data_platform.db import _read_conn as data_conn
from app.core.a_share_fees import is_exchange_etf
from app.services.backtest_data_source import BacktestDataSource
from app.services.defensive_assets import defensive_asset_series, defensive_asset_series_batch
from app.services.point_in_time import normalize_explicit_timestamp, signal_available_time


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _live_observation_cutoff() -> str:
    """Use the actual read time for a live console valuation, never a fake close clock."""
    return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class PositionMarketDataNotReadyError(RuntimeError):
    """A live position cannot be valued from an exact market-data slice."""

    def __init__(self, symbols: Iterable[str], as_of: str, details: dict[str, str] | None = None):
        self.symbols = sorted({str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip()})
        self.as_of = str(as_of or "").replace("-", "")[:8]
        self.details = dict(details or {})
        detail = ", ".join(self.symbols) or "unknown"
        super().__init__(
            f"position_market_data_not_ready: trade_date={self.as_of or 'unknown'} symbols={detail}; "
            "missing exact-session market data must block valuation"
        )


def current_live_valuation_trade_date() -> str:
    """Resolve today's valuation session from the authoritative exchange calendar."""
    from app.data_platform.services.market_calendar import target_trade_date

    with data_conn() as conn:
        return target_trade_date(conn)


def missing_exact_position_valuations(
    positions: Iterable[dict[str, Any]],
    valuations: dict[str, dict[str, Any]],
    *,
    as_of: str,
) -> dict[str, str]:
    """Return holdings which were not valued from an exact-session close."""
    target = str(as_of or "").replace("-", "")[:8]
    missing: dict[str, str] = {}
    for position in positions:
        if int(position.get("quantity") or 0) <= 0:
            continue
        symbol = str(position.get("symbol") or "").strip()
        if not symbol:
            continue
        valuation = valuations.get(symbol) or {}
        source = str(valuation.get("price_source") or "")
        actual_date = str(valuation.get("trade_date") or "").replace("-", "")[:8]
        if source not in {"stock_daily_point_in_time_close", "fund_daily_point_in_time_close"}:
            missing[symbol] = str(valuation.get("observation_error") or source or "market_slice_missing")
        elif not target or actual_date != target or _number(valuation.get("price")) <= 0:
            missing[symbol] = "exact_trade_date_or_close_missing"
    return missing


def _etf_ts_code(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    if code.endswith((".SH", ".SZ")):
        return code
    return f"{code}.SH" if code.startswith("5") else f"{code}.SZ"


def point_in_time_position_market_series(
    *,
    symbol: str,
    as_of: str,
    fallback_price: float = 0.0,
    lookback: int = 120,
    data_source: BacktestDataSource | None = None,
    observation_cutoff: str | None = None,
    exact_date: bool = False,
) -> dict[str, Any]:
    """Resolve one position's point-in-time close and return history."""
    compact_date = str(as_of or "").replace("-", "")[:8]
    normalized_symbol = str(symbol or "").strip()
    history_limit = max(20, min(252, int(lookback or 120)))
    strict_cutoff = normalize_explicit_timestamp(observation_cutoff) if observation_cutoff else ""
    if observation_cutoff and not strict_cutoff:
        return {
            "price": _number(fallback_price),
            "returns": [],
            "price_source": "position_cost_fallback",
            "trade_date": compact_date,
            "observation_cutoff": "",
            "observation_error": "invalid_observation_cutoff",
        }
    if exact_date and compact_date and not strict_cutoff:
        # A current valuation still needs an explicit, deterministic clock even
        # when the caller is a read-only account/report path rather than a
        # frozen decision.  This prevents the non-strict history reader below
        # from relabeling a prior close as the target session.
        strict_cutoff = signal_available_time(compact_date)
    # Non-formal read-only pages may still ask for a date without an explicit
    # clock. Formal callers always pass the captured decision cutoff. Keep the
    # compatibility default conservative and identical for stocks and ETFs.
    effective_cutoff = strict_cutoff or (
        f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}T23:59:59+08:00"
        if len(compact_date) == 8 else ""
    )

    if is_exchange_etf(normalized_symbol) and compact_date:
        ts_code = _etf_ts_code(normalized_symbol)
        try:
            bar, returns = defensive_asset_series(
                ts_code,
                compact_date,
                limit=history_limit,
                available_as_of_time=effective_cutoff,
                exact_date=bool(strict_cutoff),
            )
        except Exception:  # noqa: BLE001 - a read-only valuation must degrade safely
            bar, returns = None, []
        close = _number((bar or {}).get("close"))
        actual_trade_date = str((bar or {}).get("trade_date") or "").replace("-", "")[:8]
        if close > 0 and (not strict_cutoff or actual_trade_date == compact_date):
            return {
                "price": close,
                "returns": returns,
                "price_source": "fund_daily_point_in_time_close",
                "trade_date": str((bar or {}).get("trade_date") or compact_date),
                "observation_cutoff": effective_cutoff,
            }
        if strict_cutoff:
            return {
                "price": _number(fallback_price),
                "returns": [],
                "price_source": "position_cost_fallback",
                "trade_date": compact_date,
                "observation_cutoff": strict_cutoff,
                "observation_error": (
                    "strict_fund_daily_trade_date_missing"
                    if bar and actual_trade_date != compact_date
                    else "strict_fund_daily_slice_missing"
                ),
            }

    # Current account valuation must use the quoted, unadjusted close.  An
    # adj_factor belongs to historical return reconstruction; multiplying a
    # live holding's market price by it turns (for example) a CNY 5.43 quote
    # into a fictitious CNY 136.65 position value.  Keep adjusted prices only
    # for the return series used by risk diagnostics.
    if compact_date:
        rows: list[dict[str, Any]] | list[Any] | None = []
        try:
            from app.data_platform.services.observation_clock import load_snapshot_versions
            with data_conn() as conn:
                code = normalized_symbol.split(".", 1)[0].zfill(6)
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                versioned = load_snapshot_versions(
                    conn,
                    cutoff=effective_cutoff,
                    ts_codes=[f"{code}{suffix}"],
                    trade_date_end=compact_date,
                    limit=history_limit,
                )
                if versioned is not None:
                    rows = []
                    for item in versioned:
                        try:
                            payload = json.loads(item.get("payload_json") or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            payload = {}
                        if isinstance(payload, dict):
                            rows.append({
                                "trade_date": item.get("trade_date"),
                                "close": payload.get("close"),
                                "adj_factor": payload.get("adj_factor"),
                            })
                elif strict_cutoff:
                    # A formal valuation may not widen from immutable
                    # versions to the mutable projection, even in a local
                    # compatibility database.  The projection has no
                    # observation-clock proof.
                    rows = None
                else:
                    rows = conn.execute(
                        """SELECT trade_date, close, adj_factor
                             FROM dc_stock_daily_snapshot
                            WHERE ts_code=? AND trade_date<=? AND close IS NOT NULL
                            ORDER BY trade_date DESC LIMIT ?""",
                        (f"{code}{suffix}", compact_date, history_limit),
                    ).fetchall()
            if rows:
                ordered = sorted(
                    (dict(row) for row in rows),
                    key=lambda row: str(row.get("trade_date") or "").replace("-", ""),
                )
                target_close = _number(ordered[-1].get("close")) if ordered else 0.0
                if strict_cutoff and (
                    not ordered
                    or str(ordered[-1].get("trade_date") or "").replace("-", "")[:8] != compact_date
                    or target_close <= 0
                ):
                    return {
                        "price": _number(fallback_price),
                        "returns": [],
                        "price_source": "position_cost_fallback",
                        "trade_date": compact_date,
                        "observation_cutoff": strict_cutoff,
                        "observation_error": (
                            "strict_snapshot_trade_date_missing"
                            if ordered and str(ordered[-1].get("trade_date") or "").replace("-", "")[:8] != compact_date
                            else "strict_snapshot_target_close_missing"
                        ),
                    }
                valid_pairs = [
                    (
                        _number(row.get("close")),
                        _number(row.get("adj_factor"), 1.0),
                    )
                    for row in ordered
                    if _number(row.get("close")) > 0
                ]
                if valid_pairs:
                    raw_closes = [pair[0] for pair in valid_pairs]
                    adjusted_closes = [pair[0] * pair[1] for pair in valid_pairs]
                    return {
                        "price": raw_closes[-1],
                        "returns": [
                            adjusted_closes[index] / adjusted_closes[index - 1] - 1.0
                            for index in range(1, len(adjusted_closes))
                            if adjusted_closes[index - 1] > 0
                        ],
                        "price_source": "stock_daily_point_in_time_close",
                        "trade_date": compact_date,
                        "observation_cutoff": effective_cutoff,
                    }
        except Exception:  # noqa: BLE001 - valuation must retain safe fallback
            pass

        # On PostgreSQL an absent observation-clock version table or an empty
        # strict result is not evidence that the current projection is safe.
        # Do not widen a formal cutoff through BacktestDataSource; the caller's
        # execution checks will turn this explicit fallback into a block.
        if strict_cutoff and not rows:
            return {
                "price": _number(fallback_price),
                "returns": [],
                "price_source": "position_cost_fallback",
                "trade_date": compact_date,
                "observation_cutoff": strict_cutoff,
                "observation_error": (
                    "strict_snapshot_versions_missing"
                    if rows is None
                    else "strict_snapshot_trade_date_missing"
                ),
            }

    ds = data_source or BacktestDataSource()
    try:
        bars = ds.get_hist_window(normalized_symbol, compact_date, history_limit) if compact_date else []
    except Exception:  # noqa: BLE001 - cost fallback keeps read-only pages available
        bars = []
    closes = [_number(row.close) for row in bars if _number(row.close) > 0]
    returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    if closes and not strict_cutoff:
        return {
            "price": closes[-1],
            "returns": returns,
            "price_source": "stock_daily_point_in_time_close",
            "trade_date": compact_date,
            "observation_cutoff": effective_cutoff,
        }
    return {
        "price": _number(fallback_price),
        "returns": [],
        "price_source": "position_cost_fallback",
        "trade_date": compact_date,
        "observation_cutoff": strict_cutoff or effective_cutoff,
        "observation_error": "market_slice_missing",
    }


def build_position_market_series_cache(
    positions: Iterable[dict[str, Any]],
    *,
    as_of: str,
    lookback: int = 120,
    data_source: BacktestDataSource | None = None,
    observation_cutoff: str | None = None,
    exact_date: bool = True,
) -> dict[str, dict[str, Any]]:
    """Resolve each live holding once for a read-only aggregate request."""
    if data_source is not None:
        source = data_source
        result: dict[str, dict[str, Any]] = {}
        for position in positions:
            if int(position.get("quantity") or 0) <= 0:
                continue
            symbol = str(position.get("symbol") or "").strip()
            if not symbol or symbol in result:
                continue
            result[symbol] = point_in_time_position_market_series(
                symbol=symbol,
                as_of=as_of,
                fallback_price=_number(position.get("price")),
                lookback=lookback,
                data_source=source,
                observation_cutoff=observation_cutoff,
                exact_date=exact_date,
            )
        return result

    compact_date = str(as_of or "").replace("-", "")[:8]
    history_limit = max(20, min(252, int(lookback or 120)))
    strict_cutoff = normalize_explicit_timestamp(observation_cutoff) if observation_cutoff else ""
    if observation_cutoff and not strict_cutoff:
        return {
            str(position.get("symbol") or "").strip(): {
                "price": _number(position.get("price")),
                "returns": [],
                "price_source": "position_cost_fallback",
                "trade_date": compact_date,
                "observation_cutoff": "",
                "observation_error": "invalid_observation_cutoff",
            }
            for position in positions
            if int(position.get("quantity") or 0) > 0 and str(position.get("symbol") or "").strip()
        }
    exact_mode = bool(exact_date and compact_date)
    effective_cutoff = strict_cutoff or (_live_observation_cutoff() if exact_mode else "")
    live_positions = [
        position for position in positions if int(position.get("quantity") or 0) > 0
    ]
    result = {}
    etf_positions = [
        position for position in live_positions
        if is_exchange_etf(str(position.get("symbol") or "").strip())
    ]
    etf_codes = {
        _etf_ts_code(str(position.get("symbol") or "").strip()): str(position.get("symbol") or "").strip()
        for position in etf_positions
    }
    stock_positions = [
        position for position in live_positions
        if str(position.get("symbol") or "").strip() not in result
        and not is_exchange_etf(str(position.get("symbol") or "").strip())
    ]
    stock_codes = []
    symbol_by_code: dict[str, str] = {}
    for position in stock_positions:
        symbol = str(position.get("symbol") or "").strip()
        code = symbol.split(".", 1)[0].zfill(6)
        suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
        ts_code = f"{code}{suffix}"
        if ts_code not in symbol_by_code:
            symbol_by_code[ts_code] = symbol
            stock_codes.append(ts_code)
    if compact_date and (etf_codes or stock_codes):
        placeholders = ",".join("?" for _ in stock_codes)
        try:
            with data_conn() as conn:
                if etf_codes:
                    series_by_code = defensive_asset_series_batch(
                        etf_codes.keys(), compact_date, limit=history_limit, connection=conn,
                        available_as_of_time=effective_cutoff or observation_cutoff,
                        exact_date=exact_mode,
                    )
                    for code, symbol in etf_codes.items():
                        bar, returns = series_by_code.get(code, (None, []))
                        close = _number((bar or {}).get("close"))
                        actual_trade_date = str((bar or {}).get("trade_date") or "").replace("-", "")[:8]
                        if close > 0 and (not exact_mode or actual_trade_date == compact_date):
                            result[symbol] = {
                                "price": close,
                                "returns": returns,
                                "price_source": "fund_daily_point_in_time_close",
                                "trade_date": str((bar or {}).get("trade_date") or compact_date),
                                "observation_cutoff": effective_cutoff,
                            }
                if stock_codes:
                    from app.data_platform.services.observation_clock import load_snapshot_versions
                    versioned_rows = load_snapshot_versions(
                        conn,
                        cutoff=(
                            effective_cutoff
                            if exact_mode
                            else f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}T23:59:59+08:00"
                        ),
                        ts_codes=stock_codes,
                        trade_date_end=compact_date,
                        # A global LIMIT lets a dense symbol consume the
                        # result set and starve other holdings.  Bound the
                        # history independently for each requested security.
                        per_ts_code_limit=history_limit,
                    )
                    if versioned_rows is not None:
                        rows = []
                        for item in versioned_rows:
                            try:
                                payload = json.loads(item.get("payload_json") or "{}")
                            except (TypeError, ValueError, json.JSONDecodeError):
                                payload = {}
                            if isinstance(payload, dict):
                                rows.append({
                                    "ts_code": item.get("ts_code"),
                                    "trade_date": item.get("trade_date"),
                                    "close": payload.get("close"),
                                    "adj_factor": payload.get("adj_factor"),
                                })
                    elif exact_mode:
                        rows = None
                    else:
                        rows = None
                else:
                    rows = []
                if stock_codes and rows is None and not exact_mode:
                    rows = conn.execute(
                        f"""WITH ranked AS (
                               SELECT ts_code,trade_date,close,adj_factor,
                                      ROW_NUMBER() OVER (
                                          PARTITION BY ts_code ORDER BY trade_date DESC
                                      ) AS series_rank
                                 FROM dc_stock_daily_snapshot
                                WHERE ts_code IN ({placeholders})
                                  AND trade_date<=?
                                  AND adj_factor IS NOT NULL
                           )
                           SELECT ts_code,trade_date,close,adj_factor
                             FROM ranked
                            WHERE series_rank<=?
                            ORDER BY ts_code,trade_date""",
                        (*stock_codes, compact_date, history_limit),
                    ).fetchall()
                if stock_codes:
                    grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in stock_codes}
                    for row in rows or []:
                        grouped.setdefault(str(dict(row).get("ts_code") or ""), []).append(dict(row))
                    for ts_code, symbol in symbol_by_code.items():
                        raw_rows = sorted(
                            grouped.get(ts_code) or [],
                            key=lambda row: str(row.get("trade_date") or "").replace("-", ""),
                        )
                        target_close = _number(raw_rows[-1].get("close")) if raw_rows else 0.0
                        if exact_mode and (
                            not raw_rows
                            or str(raw_rows[-1].get("trade_date") or "").replace("-", "")[:8] != compact_date
                            or target_close <= 0
                        ):
                            continue
                        raw_closes = [
                            _number(row.get("close"))
                            for row in raw_rows
                            if _number(row.get("close")) > 0
                        ]
                        adjusted_closes = [
                            _number(row.get("close")) * _number(row.get("adj_factor"), 1.0)
                            for row in raw_rows
                            if _number(row.get("close")) > 0
                        ]
                        if raw_closes:
                            result[symbol] = {
                                "price": raw_closes[-1],
                                "returns": [
                                    adjusted_closes[index] / adjusted_closes[index - 1] - 1.0
                                    for index in range(1, len(adjusted_closes))
                                    if adjusted_closes[index - 1] > 0
                                ],
                                "price_source": "stock_daily_point_in_time_close",
                                "trade_date": compact_date,
                                "observation_cutoff": effective_cutoff,
                            }
        except Exception:  # noqa: BLE001 - preserve per-position safe fallback
            pass

    for position in live_positions:
        symbol = str(position.get("symbol") or "").strip()
        if not symbol or symbol in result:
            continue
        result[symbol] = point_in_time_position_market_series(
            symbol=symbol,
            as_of=as_of,
            fallback_price=_number(position.get("price")),
            lookback=history_limit,
            observation_cutoff=effective_cutoff or observation_cutoff,
            exact_date=exact_mode,
        )
    return result
