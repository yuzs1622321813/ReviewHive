"""项目评审报告模型与终端渲染的单元测试。"""
from __future__ import annotations

from reviewhive.core.schema import Finding
from reviewhive.project.report import ProjectReport, ScannedFile, format_report


class TestProjectReport:
    def test_severity_counts_empty(self):
        report = ProjectReport()
        counts = report.severity_counts()
        assert all(v == 0 for v in counts.values())

    def test_severity_counts_with_findings(self):
        report = ProjectReport(findings=[
            Finding(severity="critical", title="a"),
            Finding(severity="high", title="b"),
            Finding(severity="high", title="c"),
            Finding(severity="low", title="d"),
        ])
        counts = report.severity_counts()
        assert counts["critical"] == 1
        assert counts["high"] == 2
        assert counts["low"] == 1
        assert counts["medium"] == 0


class TestFormatReport:
    def test_basic_output(self):
        report = ProjectReport(
            root="/tmp/test_project",
            scanned=10,
            reviewed=3,
            skipped=7,
            duration_ms=1500,
            status="done",
        )
        out = format_report(report)
        assert "/tmp/test_project" in out
        assert "10" in out
        assert "3" in out
        assert "7" in out

    def test_no_findings(self):
        report = ProjectReport(root="/tmp/proj", scanned=5, reviewed=2, skipped=3)
        out = format_report(report)
        assert "发现: 无" in out

    def test_with_findings(self):
        report = ProjectReport(
            root="/tmp/proj",
            scanned=5,
            reviewed=2,
            skipped=3,
            findings=[
                Finding(severity="high", title="SQL 注入风险", file="db.py", lines="10-15", category="security"),
                Finding(severity="medium", title="未使用变量", file="utils.py", lines="20", category="style"),
            ],
        )
        out = format_report(report)
        assert "SQL 注入风险" in out
        assert "db.py" in out
        assert "未使用变量" in out
        assert "HIGH" in out
        assert "MEDIUM" in out

    def test_scan_detail_review_marker(self):
        report = ProjectReport(
            root="/tmp/proj",
            scanned=3,
            reviewed=1,
            skipped=2,
            scan=[
                ScannedFile(path="auth.py", score=8, loc=100, signals=["eval"], decision="review"),
                ScannedFile(path="utils.py", score=1, loc=50, signals=[], decision="skip"),
            ],
        )
        out = format_report(report)
        assert ">>" in out
        assert "auth.py" in out
        assert "utils.py" in out

    def test_summary_section(self):
        report = ProjectReport(root="/tmp/proj", summary="整体质量良好，无高风险问题。")
        out = format_report(report)
        assert "整体质量良好" in out
