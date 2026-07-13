from app.services import industry_event_engine


def test_review_linked_events_only_reviews_actionable_negative_links(monkeypatch):
    monkeypatch.setattr(industry_event_engine, "list_events", lambda limit: {"items": [
        {"id": 1, "event_type": "earnings", "thesis_link": {"thesis_id": 8}, "follow_up": {"requires_thesis_review": True}},
        {"id": 2, "event_type": "earnings", "thesis_link": {"thesis_id": 9}, "follow_up": {"requires_thesis_review": False}},
        {"id": 3, "event_type": "earnings", "thesis_link": None, "follow_up": {"requires_thesis_review": True}},
    ]})
    from app.services import thesis_store
    monkeypatch.setattr(thesis_store, "review_thesis", lambda thesis_id: {"id": thesis_id, "ts_code": "000001.SZ", "derived_status": "watch", "derived_confidence": 50})

    result = industry_event_engine.review_linked_event_theses()

    assert result["candidates"] == 1
    assert result["reviewed"] == [{"thesis_id": 8, "ts_code": "000001.SZ", "event_id": 1, "event_type": "earnings", "status": "watch", "confidence": 50}]
