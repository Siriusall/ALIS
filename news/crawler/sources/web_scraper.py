from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

from config import OCR_LANG, REQUEST_DELAY, TESSERACT_CMD
from playwright.sync_api import Browser, Page, sync_playwright

F10_FINANCE_URL = (
    "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html"
    "?type=web&code={market}{code}&color=b#/cwfx"
)
F10_HOLDER_URL = (
    "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html"
    "?type=web&code={market}{code}&color=b#/gdyj"
)
QUOTE_URL = "https://quote.eastmoney.com/{market}{code}.html"
SINA_QUOTE_URL = "https://finance.sina.com.cn/realstock/company/{market}{code}/nc.shtml"
THS_QUOTE_URL = "https://stockpage.10jqka.com.cn/{code}/"
SZSE_LIST_URL = "https://www.szse.cn/market/product/stock/list/index.html"
EASTMONEY_STOCK_LIST_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "http://push2.eastmoney.com/api/qt/clist/get",
    "https://80.push2.eastmoney.com/api/qt/clist/get",
    "http://80.push2.eastmoney.com/api/qt/clist/get",
]


def market_prefix(code: str) -> str:
    return "SH" if code.startswith(("5", "6", "9")) else "SZ"


@contextmanager
def browser_page(headless: bool = True) -> Iterator[Page]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            yield page
        finally:
            browser.close()


def _goto(page: Page, url: str, wait_ms: int = 5000) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(wait_ms)
    text = page.inner_text("body")
    if len(text.strip()) < 200:
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
    time.sleep(REQUEST_DELAY)
    return text


def scrape_szse_stocks(page: Page, max_pages: int = 200, limit: int | None = None) -> list[dict[str, str]]:
    stocks: list[dict[str, str]] = []
    seen: set[str] = set()
    _goto(page, SZSE_LIST_URL, wait_ms=4000)
    if limit:
        max_pages = min(max_pages, 5)

    for _ in range(max_pages):
        text = page.inner_text("body")
        for line in text.splitlines():
            line = line.strip()
            m = re.match(
                r"^(?:主板|创业板|科创板)\s+(\d{6})\s+(.+?)\s+\d{4}-\d{2}-\d{2}\s+",
                line,
            )
            if not m:
                continue
            code, name = m.group(1), m.group(2).strip()
            if code in seen:
                continue
            seen.add(code)
            industry = ""
            parts = re.split(r"\s{2,}|\t", line)
            for part in parts:
                if re.match(r"^[A-Z]\s", part):
                    industry = part.split()[-1] if part.split() else part
                    break
            stocks.append({
                "code": code,
                "name": name,
                "market": "SZ",
                "industry": industry or "深圳A股",
            })
            if limit and len(stocks) >= limit:
                return stocks

        next_link = page.query_selector("a.next:not(.disabled), li.next:not(.disabled) a")
        if not next_link:
            break
        try:
            next_link.click()
            page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            break
    return stocks


def scrape_sse_stocks(page: Page) -> list[dict[str, str]]:
    stocks: list[dict[str, str]] = []
    url = "http://www.sse.com.cn/assortment/stock/list/share/"
    _goto(page, url, wait_ms=5000)
    text = page.inner_text("body")
    for line in text.splitlines():
        m = re.search(r"\b([69]\d{5})\b\s+([\u4e00-\u9fffA-Za-z0-9\u3400-\u9fff·\s]+?)\s", line)
        if m:
            code, name = m.group(1), m.group(2).strip()
            stocks.append({
                "code": code,
                "name": name,
                "market": "SH",
                "industry": "上海A股",
            })
    if stocks:
        return stocks

    # fallback: parse embedded table rows from page html
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        if len(cells) >= 2 and re.fullmatch(r"[69]\d{5}", cells[0]):
            stocks.append({
                "code": cells[0],
                "name": cells[1],
                "market": "SH",
                "industry": cells[-1] if len(cells) > 2 else "上海A股",
            })
    return stocks


def scrape_fixed_hs_stock_universe(limit: int | None = None) -> list[dict[str, str]]:
    stocks: list[dict[str, str]] = []
    seen: set[str] = set()
    page_no = 1
    page_size = 1000

    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        while True:
            params = {
                "pn": page_no,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f100",
            }
            last_error: Exception | None = None
            for url in EASTMONEY_STOCK_LIST_URLS:
                try:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            else:
                assert last_error is not None
                raise last_error
            data = resp.json().get("data") or {}
            rows = data.get("diff") or []
            if not rows:
                break

            for row in rows:
                code = str(row.get("f12") or "").strip()
                name = str(row.get("f14") or "").strip()
                if not re.fullmatch(r"[036]\d{5}", code):
                    continue
                if code in seen:
                    continue
                seen.add(code)
                stocks.append({
                    "code": code,
                    "name": name or code,
                    "market": market_prefix(code),
                    "industry": str(row.get("f100") or "沪深A股").strip() or "沪深A股",
                })
                if limit and len(stocks) >= limit:
                    return stocks

            total = int(data.get("total") or 0)
            if page_no * page_size >= total:
                break
            page_no += 1

    stocks.sort(key=lambda item: item["code"])
    return stocks


def scrape_fixed_hs_stock_universe_with_page(page: Page, limit: int | None = None) -> list[dict[str, str]]:
    stocks: list[dict[str, str]] = []
    seen: set[str] = set()
    page_no = 1
    page_size = 1000

    while True:
        params = {
            "pn": page_no,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f100",
        }
        url = f"https://push2.eastmoney.com/api/qt/clist/get?{urlencode(params)}"
        print(f"[INFO] loading stock universe page {page_no}", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(300)
        text = page.inner_text("body")
        json_start = text.find("{")
        json_end = text.rfind("}")
        if json_start < 0 or json_end < json_start:
            break
        data = json.loads(text[json_start:json_end + 1]).get("data") or {}
        rows = data.get("diff") or []
        if not rows:
            break

        for row in rows:
            code = str(row.get("f12") or "").strip()
            name = str(row.get("f14") or "").strip()
            if not re.fullmatch(r"[036]\d{5}", code):
                continue
            if code in seen:
                continue
            seen.add(code)
            stocks.append({
                "code": code,
                "name": name or code,
                "market": market_prefix(code),
                "industry": str(row.get("f100") or "沪深A股").strip() or "沪深A股",
            })
            if limit and len(stocks) >= limit:
                return stocks

        total = int(data.get("total") or 0)
        if page_no * page_size >= total:
            break
        page_no += 1

    stocks.sort(key=lambda item: item["code"])
    return stocks


def scrape_stock_universe(page: Page, limit: int | None = None) -> list[dict[str, str]]:
    try:
        stocks = scrape_fixed_hs_stock_universe_with_page(page, limit=limit)
        if stocks:
            return stocks[:limit] if limit else stocks
    except Exception:
        stocks = scrape_fixed_hs_stock_universe(limit=limit)
        if stocks:
            return stocks[:limit] if limit else stocks

    merged: dict[str, dict[str, str]] = {}
    for item in scrape_szse_stocks(page, limit=limit):
        merged[item["code"]] = item
        if limit and len(merged) >= limit:
            return list(merged.values())[:limit]

    for item in scrape_sse_stocks(page):
        merged[item["code"]] = item
        if limit and len(merged) >= limit:
            return list(merged.values())[:limit]

    stocks = list(merged.values())
    if limit:
        stocks = stocks[:limit]
    return stocks


def scrape_stock_factors(page: Page, code: str, market: str | None = None) -> dict[str, Any]:
    from sources.f10_parser import parse_f10_finance, parse_f10_holder, parse_quote_header

    market = market or market_prefix(code)
    finance_text = scrape_with_ocr_fallback(
        page,
        F10_FINANCE_URL.format(market=market, code=code),
        wait_ms=6000,
    )
    holder_text = scrape_with_ocr_fallback(
        page,
        F10_HOLDER_URL.format(market=market, code=code),
        wait_ms=5000,
    )
    quote_text = scrape_with_ocr_fallback(
        page,
        QUOTE_URL.format(market=market.lower(), code=code),
        wait_ms=3000,
    )
    sina_quote_url = SINA_QUOTE_URL.format(market=market.lower(), code=code)
    ths_quote_url = THS_QUOTE_URL.format(code=code)
    sina_quote_text = scrape_with_ocr_fallback(
        page,
        sina_quote_url,
        wait_ms=3000,
    )
    ths_quote_text = scrape_with_ocr_fallback(
        page,
        ths_quote_url,
        wait_ms=3000,
    )

    finance = parse_f10_finance(finance_text)
    holder = parse_f10_holder(holder_text)
    quote = parse_quote_header("\n".join([quote_text, sina_quote_text, ths_quote_text, finance_text]))
    if _quote_needs_ocr(quote):
        sina_quote_ocr = scrape_with_ocr_fallback(
            page,
            sina_quote_url,
            wait_ms=3000,
            force_ocr=True,
            screenshot_name=f"_ocr_{code}_sina_quote.png",
        )
        ths_quote_ocr = scrape_with_ocr_fallback(
            page,
            ths_quote_url,
            wait_ms=3000,
            force_ocr=True,
            screenshot_name=f"_ocr_{code}_ths_quote.png",
        )
        quote = parse_quote_header(
            "\n".join([quote_text, sina_quote_text, ths_quote_text, sina_quote_ocr, ths_quote_ocr, finance_text])
        )

    row = {
        "code": code,
        "revenue_yoy": finance.get("revenue_yoy"),
        "deduct_profit_yoy": finance.get("deduct_profit_yoy"),
        "cagr_3y": finance.get("cagr_3y"),
        "gross_margin": finance.get("gross_margin"),
        "net_margin": finance.get("net_margin"),
        "roe": finance.get("roe"),
        "roic": finance.get("roic"),
        "ocf_to_profit": finance.get("ocf_to_profit"),
        "free_cashflow": finance.get("free_cashflow"),
        "debt_ratio": finance.get("debt_ratio"),
        "interest_debt": finance.get("interest_debt"),
        "cash_coverage": finance.get("cash_coverage"),
        "dividend_yield": quote.get("dividend_yield"),
        "payout_ratio": finance.get("payout_ratio"),
        "buyback_amount": finance.get("buyback_amount"),
        "profit_volatility_5y": finance.get("profit_volatility_5y"),
        "cyclicality": finance.get("cyclicality"),
        "pe": quote.get("pe"),
        "pb": quote.get("pb"),
        "ps": quote.get("ps"),
        "valuation_percentile": quote.get("valuation_percentile"),
        "fund_holding": holder.get("fund_holding"),
        "northbound": holder.get("northbound"),
        "etf_holding": holder.get("etf_holding"),
        "research_count": holder.get("research_count"),
    }
    return row


def _quote_needs_ocr(quote: dict[str, Any]) -> bool:
    return any(quote.get(key) is None for key in ("pe", "pb", "ps", "dividend_yield"))


def scrape_with_ocr_fallback(
    page: Page,
    url: str,
    wait_ms: int = 5000,
    *,
    min_text_len: int = 200,
    force_ocr: bool = False,
    screenshot_name: str = "_ocr_tmp.png",
) -> str:
    text = _goto(page, url, wait_ms=wait_ms)
    if len(text.strip()) >= min_text_len and not force_ocr:
        return text
    screenshot = screenshot_name
    page.screenshot(path=screenshot, full_page=True)
    try:
        import pytesseract
        from PIL import Image

        if TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        ocr_text = pytesseract.image_to_string(Image.open(screenshot), lang=OCR_LANG)
        if ocr_text:
            return f"{text}\n{ocr_text}"
        return text
    except Exception:  # noqa: BLE001
        return text
