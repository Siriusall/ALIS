from app.services.node_activity import get_node_daily_activities


def test_get_node_daily_activities_structure():
    result = get_node_daily_activities(limit_days=3)
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result.get("schema_version") == "alis_node_daily_activities_v1"
    days = result.get("days")
    assert isinstance(days, list)
    if days:
        first_day = days[0]
        assert "date" in first_day
        assert "db_node" in first_day
        assert "compute_node" in first_day
        assert "app_node" in first_day
        assert isinstance(first_day["db_node"]["tasks"], list)
        assert isinstance(first_day["compute_node"]["tasks"], list)
        assert isinstance(first_day["app_node"]["tasks"], list)
