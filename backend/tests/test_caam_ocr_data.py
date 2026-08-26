from __future__ import annotations

from app.services import caam_ocr_data as service


def test_sales_links_only_keeps_monthly_caam_operating_releases() -> None:
    html = """
      <a href="/chn/5/cate_39/con_1.html"><span>2026年5月汽车工业产销情况简析</span></a>
      <a href="/chn/5/cate_39/con_2.html"><span>2026年5月新能源汽车产销情况简析</span></a>
      <a href="/chn/5/cate_68/con_3.html"><span>关于召开汽车论坛的通知</span></a>
      <a href="https://other.example/con_4.html"><span>2026年5月汽车工业产销情况</span></a>
    """

    assert service._sales_links("http://www.caam.org.cn/", html) == [
        ("http://www.caam.org.cn/chn/5/cate_39/con_1.html", "2026年5月汽车工业产销情况简析"),
        ("http://www.caam.org.cn/chn/5/cate_39/con_2.html", "2026年5月新能源汽车产销情况简析"),
    ]


def test_caam_document_id_is_stable_across_site_sections() -> None:
    assert service._document_id("http://www.caam.org.cn/chn/5/cate_39/con_5237222.html") == "caam-con-5237222"
    assert service._document_id("http://www.caam.org.cn/chn/4/cate_32/con_5237222.html") == "caam-con-5237222"


def test_article_images_only_keeps_caam_file_evidence() -> None:
    html = """
      <img src="http://www.caam.org.cn/logo.png">
      <div class="fourBox"><img src="http://file.caam.org.cn/2026/07/report.png"></div>
      </div><!-- right --><img src="https://other.example/ad.png">
    """

    assert service._article_images("http://www.caam.org.cn/page", html) == [
        "http://file.caam.org.cn/2026/07/report.png"
    ]


def test_linux_ocr_uses_tesseract_tsv(monkeypatch) -> None:
    payload = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\t汽车\n"
        "5\t1\t1\t1\t1\t2\t10\t0\t10\t10\t80\t销量\n"
    ).encode("utf-8")

    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service.shutil, "which", lambda command: "/usr/bin/tesseract" if command == "tesseract" else None)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: type("Completed", (), {"stdout": payload})(),
    )

    assert service._ocr_image(b"image") == [{
        "text": "汽车 销量",
        "confidence": 0.85,
        "engine": "tesseract_chi_sim_eng",
    }]


def test_parse_monthly_observations_keeps_ocr_confidence_and_exact_period() -> None:
    lines = [
        {"confidence": 0.5, "text": "2026年5月，汽车产销分别完成 261.6万辆和 262.9万辆，环比增长。"},
        {"confidence": 0.5, "text": "2026年1-5月，汽车产销分别完成 1223.5万辆和 1220.7万辆。"},
        {"confidence": 0.6, "text": "2026年5月，新能源汽车产销分别完成155.4万辆和149.6万辆。"},
        {"confidence": 0.7, "text": "2026年5月，汽车国内销量170万辆。"},
        {"confidence": 0.8, "text": "2026年5月，汽车出口93万辆。"},
    ]

    observation_date, rows = service._parse_monthly_observations("2026年5月汽车工业产销情况简析", lines)
    values = {row["field_code"]: row for row in rows}

    assert observation_date == "2026-05-31"
    assert values["vehicle_production_10k"]["value"] == 261.6
    assert values["vehicle_sales_10k"]["value"] == 262.9
    assert values["new_energy_vehicle_sales_10k"]["value"] == 149.6
    assert values["vehicle_domestic_sales_10k"]["value"] == 170.0
    assert values["vehicle_export_10k"]["confidence"] == 0.8
    assert 1220.7 not in {row["value"] for row in rows}


def test_cross_document_assessment_never_calls_ocr_structured_reconciled() -> None:
    rows = [
        {"observation_date": "2026-05-31", "field_code": "sales", "document_id": "a", "value": 262.9},
        {"observation_date": "2026-05-31", "field_code": "sales", "document_id": "b", "value": 262.9},
        {"observation_date": "2026-05-31", "field_code": "export", "document_id": "a", "value": 93.0},
        {"observation_date": "2026-04-30", "field_code": "sales", "document_id": "c", "value": 250.0},
        {"observation_date": "2026-04-30", "field_code": "sales", "document_id": "d", "value": 280.0},
    ]

    result = service._cross_document_assessment(rows)

    assert result[("2026-05-31", "sales")] == "cross_document_consistent_pending_structured"
    assert result[("2026-05-31", "export")] == "pending_structured_reconciliation"
    assert result[("2026-04-30", "sales")] == "cross_document_conflict"
    assert "structured_reconciled" not in set(result.values())


def test_miit_auto_parser_extracts_official_monthly_fields() -> None:
    text = """
      2026年1月，我国汽车产销分别完成245万辆和234.6万辆。
      1月，乘用车产销分别完成206.2万辆和198.8万辆。
      1月，商用车产销分别完成38.8万辆和35.9万辆。
      1月，新能源汽车产销分别完成104.1万辆和94.5万辆。
      1月，汽车整车出口68.1万辆。
    """

    observation_date, rows = service._parse_miit_auto_observations("2026年1月汽车工业经济运行情况", text)
    values = {row["field_code"]: row["value"] for row in rows}

    assert observation_date == "2026-01-31"
    assert values["vehicle_production_10k"] == 245.0
    assert values["vehicle_sales_10k"] == 234.6
    assert values["new_energy_vehicle_sales_10k"] == 94.5
    assert values["vehicle_export_10k"] == 68.1
