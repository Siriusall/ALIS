from __future__ import annotations

from contextlib import nullcontext

from app.services import official_policy_data as service


def test_official_index_only_keeps_same_host_policy_links() -> None:
    source = service.OfficialSource("official.test", "测试部门", "https://example.gov.cn/policy/")
    html = """
      <a href="/policy/a.html">关于扩大新能源汽车消费的通知</a>
      <a href="https://other.example/a.html">关于煤炭保供的通知</a>
      <a href="/policy/b.html">联系我们</a>
    """
    assert service._source_links(source, html) == [
        ("https://example.gov.cn/policy/a.html", "关于扩大新能源汽车消费的通知"),
    ]


def test_sync_retains_source_url_publish_time_and_fetch_time(monkeypatch) -> None:
    source = service.OfficialSource("official.test", "测试部门", "https://example.gov.cn/policy/")
    saved: list[dict[str, str]] = []
    pages = {
        source.index_url: '<a href="/policy/a.html">关于钢铁产能压减的通知</a>',
        "https://example.gov.cn/policy/a.html": "<title>关于钢铁产能压减的通知</title><p>2026年7月21日 发布</p>",
    }
    monkeypatch.setattr(service, "_request_text", lambda url: pages[url])
    monkeypatch.setattr(service, "_save_documents", lambda rows: saved.extend(rows) or len(rows))
    monkeypatch.setattr(service, "_now", lambda: "2026-07-23T09:00:00")

    result = service.sync_official_policy_documents(sources=(source,))

    assert result["status"] == "ready"
    assert result["documents_written"] == 1
    assert saved[0]["url"] == "https://example.gov.cn/policy/a.html"
    assert saved[0]["publish_time"] == "2026-07-21T00:00:00"
    assert saved[0]["available_time"] == "2026-07-23T09:00:00"


def test_sync_stops_new_documents_at_per_source_budget(monkeypatch) -> None:
    source = service.OfficialSource("official.test", "测试部门", "https://example.gov.cn/policy/")
    links = "".join(f'<a href="/policy/{index}.html">关于行业产能调整的通知{index}</a>' for index in range(8))
    fetched: list[str] = []
    monkeypatch.setattr(service, "_request_text", lambda url: fetched.append(url) or (links if url == source.index_url else "<p>2026年7月21日 政策通知</p>"))
    monkeypatch.setattr(service, "_save_documents", lambda rows: len(rows))
    monkeypatch.setattr(service, "_now", lambda: "2026-07-23T09:00:00")

    result = service.sync_official_policy_documents(sources=(source,))

    assert result["documents_written"] == service.MAX_NEW_DOCUMENTS_PER_SOURCE_PER_CYCLE
    assert result["sources"][0]["attempted_new"] == service.MAX_NEW_DOCUMENTS_PER_SOURCE_PER_CYCLE
    assert len(fetched) == 1 + service.MAX_NEW_DOCUMENTS_PER_SOURCE_PER_CYCLE


def test_policy_materialization_keeps_official_evidence(monkeypatch) -> None:
    evidence = []

    class Cursor:
        def fetchall(self):
            return [{
                "source_code": "official.miit", "announcement_id": "a", "title": "钢铁产能压减通知",
                "publish_time": "2026-07-21T00:00:00", "url": "https://example.gov/a",
                "raw_text": "推进钢铁行业压减产能", "available_time": "2026-07-22T08:00:00",
            }]

    class Conn:
        def execute(self, *_args):
            return Cursor()

    monkeypatch.setattr(service, "_conn", lambda: nullcontext(Conn()))
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: evidence.extend(rows) or len(rows))
    values: list[dict] = []
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: values.extend(rows) or len(rows))

    result = service.materialize_official_policy_supply()

    assert result["values"] == 1
    assert evidence[0]["industry_name"] == "普钢"
    assert values[0]["value"] == 25.0
    assert values[0]["available_time"] == "2026-07-22T08:00:00"


def test_capacity_materialization_is_labelled_as_macro_proxy(monkeypatch) -> None:
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows

    class Conn:
        def execute(self, sql, *_args):
            if "dc_announcement_raw" in sql:
                return Cursor([{
                    "announcement_id": "nbs-1", "title": "2026年二季度全国工业产能利用率",
                    "publish_time": "2026-07-15T00:00:00", "url": "https://example.gov/nbs",
                    "raw_text": "二季度全国工业产能利用率为74.9%。", "available_time": "2026-07-15T10:00:00",
                }])
            return Cursor([{"industry": "普钢"}, {"industry": "汽车整车"}])

    monkeypatch.setattr(service, "_conn", lambda: nullcontext(Conn()))
    definitions: list[dict] = []
    values: list[dict] = []
    monkeypatch.setattr(service, "_upsert_industry_metric_definitions", lambda rows: definitions.extend(rows) or len(rows))
    monkeypatch.setattr(service, "_upsert_industry_metric_values", lambda rows: values.extend(rows) or len(rows))

    result = service.materialize_official_capacity_utilization()

    assert result["values"] == 2
    assert {row["value"] for row in values} == {74.9}
    assert all("宏观代理" in row["metric_name"] for row in definitions)
