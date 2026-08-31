import pytest

from reviewhive.models.llm import extract_json


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '好的，结果如下：\n```json\n{"action": "final", "findings": []}\n```\n完毕。'
    assert extract_json(text) == {"action": "final", "findings": []}


def test_json_embedded_in_prose():
    text = '经过分析，我决定调用技能 {"action": "skill", "skill": "grep_code", "arguments": {"pattern": "TODO"}} 来补充信息。'
    data = extract_json(text)
    assert data["skill"] == "grep_code"


def test_nested_braces():
    text = 'x {"a": {"b": [1, 2]}, "c": "}"} y'
    assert extract_json(text)["a"]["b"] == [1, 2]


def test_array_json():
    assert extract_json("prefix [1, 2, 3] suffix") == [1, 2, 3]


def test_invalid_raises():
    with pytest.raises(ValueError):
        extract_json("完全没有 JSON 的回答")
