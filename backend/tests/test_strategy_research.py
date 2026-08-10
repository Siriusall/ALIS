from app.services import strategy_research


def test_network_research_deduplicates_crossref_and_openalex_and_caches(monkeypatch):
    cache = {}
    monkeypatch.setattr(strategy_research, "get_json", lambda key: cache.get(key))
    monkeypatch.setattr(strategy_research, "set_json", lambda key, payload, ttl: cache.update({key: payload}) or True)

    def fetch(url: str):
        if "crossref" in url:
            return {"message": {"items": [
                {"DOI": "10.1/ABC", "title": ["Paper A"], "is-referenced-by-count": 10},
                {"DOI": "10.2/DEF", "title": ["Paper B"], "is-referenced-by-count": 5},
            ]}}
        return {"results": [
            {"doi": "https://doi.org/10.1/abc", "title": "Paper A", "publication_year": 2020, "cited_by_count": 12},
            {"doi": "https://doi.org/10.3/ghi", "title": "Paper C", "publication_year": 2021, "cited_by_count": 7},
        ]}

    first = strategy_research.search_strategy_research(
        "selection_or_allocation_residual", fetch_json=fetch,
    )
    second = strategy_research.search_strategy_research(
        "selection_or_allocation_residual", fetch_json=lambda _url: (_ for _ in ()).throw(AssertionError("cache expected")),
    )

    assert first["status"] == "ready"
    assert [item["doi"] for item in first["sources"]] == ["10.1/abc", "10.3/ghi", "10.2/def"]
    assert first["research_signature"]
    assert second["cache"] == "hit"
    assert second["research_signature"] == first["research_signature"]
