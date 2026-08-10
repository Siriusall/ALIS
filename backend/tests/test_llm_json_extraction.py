import pytest

from app.services.llm_review_assistant import _extract_json


def test_extract_json_accepts_object_surrounded_by_reasoning_and_extra_braces():
    text = '说明中的{无效括号}\n```json\n{"summary":"完成","items":[]}\n```\n尾注'

    assert _extract_json(text) == {"summary": "完成", "items": []}


def test_extract_json_rejects_truncated_object():
    with pytest.raises(ValueError, match="llm_response_not_json"):
        _extract_json('{"summary":"未完成"')
