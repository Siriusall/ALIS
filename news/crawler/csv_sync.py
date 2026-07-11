from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT_DIR / "backend" / "cache" / "stock_factors.csv"

FACTOR_COLUMNS = [
    "code", "name", "market", "sector",
    "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
    "gross_margin", "net_margin", "roe", "roic",
    "ocf_to_profit", "free_cashflow",
    "debt_ratio", "interest_debt", "cash_coverage",
    "dividend_yield", "payout_ratio", "buyback_amount",
    "profit_volatility_5y", "cyclicality",
    "pe", "pb", "ps", "valuation_percentile",
    "fund_holding", "northbound", "etf_holding", "research_count",
    "crawled_at",
]


def _ensure_dir() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)


def _csv_exists() -> bool:
    return CSV_PATH.exists() and CSV_PATH.stat().st_size > 0


def _build_row(stock: dict[str, str], factor_row: dict[str, Any]) -> dict[str, Any]:
    """Merge stock metadata + factor data into a flat CSV row dict."""
    row: dict[str, Any] = {
        "code": stock.get("code", ""),
        "name": stock.get("name", stock.get("code", "")),
        "market": stock.get("market", ""),
        "sector": stock.get("industry") or stock.get("sector") or "",
    }
    for col in FACTOR_COLUMNS:
        if col in row:
            continue
        val = factor_row.get(col)
        row[col] = val
    return row


def append_stock_to_csv(stock: dict[str, str], factor_row: dict[str, Any]) -> None:
    """Append one stock's factor row to the CSV. Creates file + header if not exists."""
    _ensure_dir()
    row = _build_row(stock, factor_row)
    is_new = not _csv_exists()

    with open(str(CSV_PATH), "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FACTOR_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def export_all_to_csv(stocks: list[dict[str, str]], factors: list[dict[str, Any]]) -> None:
    """Full export: write all stocks to CSV (used for initial snapshot)."""
    _ensure_dir()
    with open(str(CSV_PATH), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FACTOR_COLUMNS)
        writer.writeheader()
        for stock, factor in zip(stocks, factors):
            writer.writerow(_build_row(stock, factor))
