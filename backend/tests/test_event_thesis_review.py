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


def test_automated_review_moves_followup_to_human_in_review_and_keeps_source_evidence():
    status, action, evidence = industry_event_engine._followup_with_thesis_review(
        {"status": "urgent", "next_action": "立即复核"},
        {"thesis_id": 8, "status": "watch", "confidence": 50},
        {"api_name": "forecast", "record_id": 11},
    )

    assert status == "in_review"
    assert "人工确认" in action
    assert evidence["api_name"] == "forecast"
    assert evidence["automated_thesis_review"]["thesis_id"] == 8


def test_industry_history_anchors_are_chronological_bounded_and_keep_latest():
    rows = [{"trade_date": f"20240{month}01", "available_time": f"2024-0{month}-01T15:00:00"} for month in range(1, 7)]

    anchors = industry_event_engine._history_anchor_rows(rows, anchor_sessions=2, max_points=2)

    assert [item["trade_date"] for item in anchors] == ["20240501", "20240601"]
