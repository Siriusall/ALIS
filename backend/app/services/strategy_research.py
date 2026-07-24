from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.data_platform.redis_store import get_json, set_json


RESEARCH_QUERIES = {
    "market_exposure": "China A-share market timing regime risk factor portfolio",
    "transaction_costs": "portfolio turnover transaction costs rank buffer trading",
    "execution_constraints": "A-share liquidity capacity portfolio execution constraints",
    "selection_or_allocation_residual": "China A-share value profitability low volatility reversal factor",
    "unknown": "China A-share factor timing market states portfolio",
}


def _fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "ALIS autonomous research/1.0 (personal research system)"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS research APIs
        return json.loads(response.read().decode("utf-8"))


def _doi(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw.removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def search_strategy_research(
    primary_cause: str,
    *,
    fetch_json: Callable[[str], dict[str, Any]] = _fetch_json,
    force: bool = False,
) -> dict[str, Any]:
    """Search Crossref and OpenAlex, cache results, and return frozen metadata only."""
    cause = str(primary_cause or "unknown")
    query = RESEARCH_QUERIES.get(cause, RESEARCH_QUERIES["unknown"])
    cache_key = f"alis:strategy-research:v1:{cause}"
    if not force:
        cached = get_json(cache_key)
        if isinstance(cached, dict) and cached.get("sources"):
            return {**cached, "cache": "hit"}
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    endpoints = [
        (
            "crossref",
            f"https://api.crossref.org/works?query={quote(query)}&rows=12&select=DOI,title,published,is-referenced-by-count",
        ),
        (
            "openalex",
            f"https://api.openalex.org/works?search={quote(query)}&per-page=12&select=doi,title,publication_year,cited_by_count",
        ),
    ]
    for provider, url in endpoints:
        try:
            payload = fetch_json(url)
            items = (payload.get("message") or {}).get("items") if provider == "crossref" else payload.get("results")
            for item in items or []:
                title_raw = item.get("title")
                title = str(title_raw[0] if isinstance(title_raw, list) and title_raw else title_raw or "").strip()
                doi = _doi(item.get("DOI") if provider == "crossref" else item.get("doi"))
                if not title or not doi:
                    continue
                sources.append({
                    "provider": provider,
                    "title": title,
                    "doi": doi,
                    "publication_year": item.get("publication_year"),
                    "cited_by_count": int(item.get("is-referenced-by-count") or item.get("cited_by_count") or 0),
                    "query": query,
                })
        except Exception as exc:  # noqa: BLE001 - research outage must not stop trading core
            errors.append(f"{provider}:{type(exc).__name__}:{exc}")
    unique: dict[str, dict[str, Any]] = {}
    for item in sources:
        current = unique.get(item["doi"])
        if current is None or int(item.get("cited_by_count") or 0) > int(current.get("cited_by_count") or 0):
            unique[item["doi"]] = item
    ranked = sorted(unique.values(), key=lambda item: (-int(item.get("cited_by_count") or 0), item["doi"]))[:12]
    signature_payload = [{"doi": item["doi"], "title": item["title"]} for item in ranked]
    signature = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = {
        "schema_version": "alis_strategy_network_research_v1",
        "status": "ready" if ranked else "unavailable",
        "primary_cause": cause,
        "query": query,
        "sources": ranked,
        "research_signature": signature,
        "errors": errors,
        "searched_at": datetime.now().astimezone().isoformat(),
        "cache": "miss",
    }
    if ranked:
        set_json(cache_key, result, ttl=7 * 24 * 60 * 60)
    return result
