"""CLI review 命令的集成测试（仅 scan-only 模式，不需要 LLM）。"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from reviewhive.cli import app

runner = CliRunner()


def _write(root: Path, rel: str, source: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


class TestScanOnly:
    def test_basic_output(self, tmp_path: Path):
        _write(tmp_path, "main.py", "x = 1\n")
        result = runner.invoke(app, ["review", str(tmp_path), "--scan-only"])
        assert result.exit_code == 0
        assert "扫描" in result.output

    def test_json_output(self, tmp_path: Path):
        _write(tmp_path, "main.py", "x = 1\n")
        out_file = tmp_path / "report.json"
        result = runner.invoke(app, ["review", str(tmp_path), "--scan-only", "-o", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert "scanned" in data
        assert "findings" in data

    def test_fail_on_high_with_finding(self, tmp_path: Path):
        _write(tmp_path, "main.py", "eval('x')\n")
        result = runner.invoke(app, ["review", str(tmp_path), "--scan-only", "--fail-on", "high"])
        assert result.exit_code == 1

    def test_fail_on_critical_without_critical(self, tmp_path: Path):
        _write(tmp_path, "main.py", "x = 1\n")
        result = runner.invoke(app, ["review", str(tmp_path), "--scan-only", "--fail-on", "critical"])
        assert result.exit_code == 0

    def test_nonexistent_path(self):
        result = runner.invoke(app, ["review", "/nonexistent/path/xyz"])
        assert result.exit_code != 0

    def test_scan_with_dangerous_code(self, tmp_path: Path):
        _write(tmp_path, "risky.py", "import os\nos.system('rm -rf /')\n")
        result = runner.invoke(app, ["review", str(tmp_path), "--scan-only"])
        assert result.exit_code == 0
        assert "发现" in result.output
