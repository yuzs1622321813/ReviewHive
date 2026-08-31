"""merge_findings 去重逻辑与辅助函数的单元测试。"""
from __future__ import annotations

from reviewhive.core.schema import Finding
from reviewhive.project.reviewer import (
    _jaccard,
    _lines_overlap,
    _parse_line_range,
    merge_findings,
)


class TestLinesOverlap:
    def test_overlapping_ranges(self):
        assert _lines_overlap("10-20", "15-25") is True

    def test_non_overlapping(self):
        assert _lines_overlap("10-20", "30-40") is False

    def test_adjacent_ranges(self):
        assert _lines_overlap("10-20", "20-30") is True

    def test_single_line(self):
        assert _lines_overlap("10", "10-15") is True

    def test_empty_strings(self):
        assert _lines_overlap("", "10-20") is False
        assert _lines_overlap("10-20", "") is False

    def test_contained_range(self):
        assert _lines_overlap("10-30", "15-20") is True


class TestParseLineRange:
    def test_range(self):
        assert _parse_line_range("10-20") == (10, 20)

    def test_single(self):
        assert _parse_line_range("15") == (15, 15)

    def test_empty(self):
        assert _parse_line_range("") == (0, 0)


class TestJaccard:
    def test_identical(self):
        assert _jaccard("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert _jaccard("abc", "xyz") == 0.0

    def test_partial_overlap(self):
        score = _jaccard("the quick brown fox", "the quick red fox")
        assert 0.5 < score < 1.0

    def test_empty_string(self):
        assert _jaccard("", "hello") == 0.0
        assert _jaccard("hello", "") == 0.0


class TestMergeFindings:
    def test_exact_dedup(self):
        f1 = Finding(file="a.py", category="security", title="SQL 注入", lines="10-20", severity="high")
        f2 = Finding(file="a.py", category="security", title="SQL 注入", lines="10-20", severity="high")
        result = merge_findings([f1, f2])
        assert len(result) == 1

    def test_same_file_category_lines_overlap(self):
        f1 = Finding(file="a.py", category="security", title="SQL 注入", lines="10-20", severity="high", confidence=0.9)
        f2 = Finding(file="a.py", category="security", title="SQL 注入风险", lines="15-25", severity="medium", confidence=0.8)
        result = merge_findings([f1, f2])
        assert len(result) == 1
        assert result[0].severity == "high"

    def test_same_file_category_jaccard_merge(self):
        f1 = Finding(file="a.py", category="security", title="SQL 注入风险 用户输入未过滤", lines="10-20", severity="high", confidence=0.9)
        f2 = Finding(file="a.py", category="security", title="SQL 注入风险 用户输入未过滤", lines="30-40", severity="medium", confidence=0.7)
        result = merge_findings([f1, f2])
        assert len(result) == 1

    def test_different_files_not_merged(self):
        f1 = Finding(file="a.py", category="security", title="SQL 注入", lines="10-20", severity="high")
        f2 = Finding(file="b.py", category="security", title="SQL 注入", lines="10-20", severity="high")
        result = merge_findings([f1, f2])
        assert len(result) == 2

    def test_different_category_not_merged(self):
        f1 = Finding(file="a.py", category="security", title="注入风险", lines="10-20", severity="high")
        f2 = Finding(file="a.py", category="performance", title="注入风险", lines="10-20", severity="high")
        result = merge_findings([f1, f2])
        assert len(result) == 2

    def test_sort_order(self):
        findings = [
            Finding(file="a.py", category="style", title="low1", severity="low", confidence=0.9),
            Finding(file="b.py", category="security", title="crit1", severity="critical", confidence=0.8),
            Finding(file="c.py", category="security", title="high1", severity="high", confidence=0.9),
            Finding(file="d.py", category="security", title="high2", severity="high", confidence=0.7),
        ]
        result = merge_findings(findings)
        assert result[0].severity == "critical"
        assert result[1].severity == "high"
        assert result[1].confidence >= result[2].confidence
        assert result[-1].severity == "low"

    def test_empty_input(self):
        assert merge_findings([]) == []

    def test_higher_severity_wins_on_overlap(self):
        f1 = Finding(file="a.py", category="sec", title="问题A", lines="10-20", severity="medium", confidence=0.9)
        f2 = Finding(file="a.py", category="sec", title="问题B", lines="15-25", severity="critical", confidence=0.8)
        result = merge_findings([f1, f2])
        assert len(result) == 1
        assert result[0].severity == "critical"
