"""AST 静态扫描 + 规则评分 + 静态发现的单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from reviewhive.config import ProjectReviewConfig
from reviewhive.project.scanner import (
    prioritize,
    scan_file,
    scan_project,
    score_file,
    static_findings,
)


def _write(root: Path, rel: str, source: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


_cfg = ProjectReviewConfig()


class TestKeywordDetection:
    def test_auth_filename_triggers_keyword_signal(self, tmp_path: Path):
        f = _write(tmp_path, "auth.py", "x = 1\n")
        sig = scan_file(tmp_path, f)
        kinds = [s.kind for s in sig.signals]
        assert "keyword" in kinds

    def test_crypto_filename_weight_at_least_2(self, tmp_path: Path):
        f = _write(tmp_path, "crypto.py", "x = 1\n")
        sig = scan_file(tmp_path, f)
        kw = [s for s in sig.signals if s.kind == "keyword"]
        assert kw and kw[0].weight >= 2

    def test_neutral_file_no_keyword_signal(self, tmp_path: Path):
        f = _write(tmp_path, "utils.py", "x = 1\n")
        sig = scan_file(tmp_path, f)
        kinds = [s.kind for s in sig.signals]
        assert "keyword" not in kinds


class TestDangerousCalls:
    def test_eval_detected(self, tmp_path: Path):
        f = _write(tmp_path, "main.py", "eval('1+1')\n")
        sig = scan_file(tmp_path, f)
        dangerous = [s for s in sig.signals if s.kind == "dangerous_call" and "eval" in s.detail]
        assert len(dangerous) == 1
        assert dangerous[0].weight == 8

    def test_os_system_detected(self, tmp_path: Path):
        f = _write(tmp_path, "main.py", "import os\nos.system('ls')\n")
        sig = scan_file(tmp_path, f)
        dangerous = [s for s in sig.signals if "os.system" in s.detail]
        assert len(dangerous) == 1
        assert dangerous[0].weight == 7

    def test_subprocess_shell_true(self, tmp_path: Path):
        src = "import subprocess\nsubprocess.check_output('ls', shell=True)\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        shell_signals = [s for s in sig.signals if "shell=True" in s.detail]
        assert len(shell_signals) == 1
        assert shell_signals[0].weight == 7

    def test_subprocess_run_flagged_as_dangerous(self, tmp_path: Path):
        src = "import subprocess\nsubprocess.run('ls')\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        sub_signals = [s for s in sig.signals if "subprocess.run" in s.detail]
        assert len(sub_signals) == 1

    def test_yaml_load_without_safe_loader(self, tmp_path: Path):
        src = "import yaml\nyaml.load(data)\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        yaml_signals = [s for s in sig.signals if "yaml.load" in s.detail]
        assert len(yaml_signals) == 1

    def test_yaml_load_with_safe_loader_not_flagged(self, tmp_path: Path):
        src = "import yaml\nyaml.load(data, Loader=SafeLoader)\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        yaml_signals = [s for s in sig.signals if "yaml.load" in s.detail]
        assert len(yaml_signals) == 0


class TestConditionalDanger:
    def test_random_in_auth_context(self, tmp_path: Path):
        src = "import random\ntoken = random.random()\n"
        f = _write(tmp_path, "auth_login.py", src)
        sig = scan_file(tmp_path, f)
        random_signals = [s for s in sig.signals if "random" in s.detail]
        assert len(random_signals) >= 1

    def test_random_without_auth_context_not_flagged(self, tmp_path: Path):
        src = "import random\nx = random.random()\n"
        f = _write(tmp_path, "utils.py", src)
        sig = scan_file(tmp_path, f)
        random_signals = [s for s in sig.signals if "random.random" in s.detail]
        assert len(random_signals) == 0


class TestHardcodedSecrets:
    def test_api_key_pattern(self, tmp_path: Path):
        src = 'api_key = "abcdefgh1234"\n'
        f = _write(tmp_path, "config.py", src)
        sig = scan_file(tmp_path, f)
        secrets = [s for s in sig.signals if s.kind == "secret"]
        assert len(secrets) == 1

    def test_short_value_not_flagged(self, tmp_path: Path):
        src = 'api_key = "abc"\n'
        f = _write(tmp_path, "config.py", src)
        sig = scan_file(tmp_path, f)
        secrets = [s for s in sig.signals if s.kind == "secret"]
        assert len(secrets) == 0


class TestExceptionSwallow:
    def test_bare_except_pass(self, tmp_path: Path):
        src = "try:\n    x = 1\nexcept:\n    pass\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        swallow = [s for s in sig.signals if s.kind == "exception_swallow"]
        assert len(swallow) == 1

    def test_specific_exception_not_flagged(self, tmp_path: Path):
        src = "try:\n    x = 1\nexcept ValueError:\n    pass\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        swallow = [s for s in sig.signals if s.kind == "exception_swallow"]
        assert len(swallow) == 0


class TestComplexity:
    def test_deep_nesting(self, tmp_path: Path):
        src = "def f():\n    if True:\n        if True:\n            if True:\n                if True:\n                    pass\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        complexity = [s for s in sig.signals if s.kind == "complexity" and "嵌套" in s.detail]
        assert len(complexity) >= 1
        assert any(s.weight >= 2 for s in complexity)

    def test_long_function(self, tmp_path: Path):
        body = "\n".join(f"    x{i} = {i}" for i in range(90))
        src = f"def f():\n{body}\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        long_func = [s for s in sig.signals if "长函数" in s.detail]
        assert len(long_func) == 1

    def test_many_arguments(self, tmp_path: Path):
        src = "def f(a, b, c, d, e, f, g): pass\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        many_args = [s for s in sig.signals if "多参数" in s.detail]
        assert len(many_args) == 1


class TestParseError:
    def test_syntax_error_flagged(self, tmp_path: Path):
        f = _write(tmp_path, "bad.py", "def f(\n")
        sig = scan_file(tmp_path, f)
        assert sig.parse_error != ""
        assert sig.score >= 2


class TestScoreAndPrioritize:
    def test_score_is_sum_of_weights(self, tmp_path: Path):
        src = 'eval("x")\napi_key = "abcdefgh1234"\n'
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        expected = sum(s.weight for s in sig.signals)
        assert sig.score == expected

    def test_prioritize_splits_by_score(self, tmp_path: Path):
        from reviewhive.project.scanner import FileSignals, Signal

        signals = [
            FileSignals(path="a.py", source="", signals=[Signal(kind="k", detail="", weight=5)], score=5),
            FileSignals(path="b.py", source="", signals=[Signal(kind="k", detail="", weight=2)], score=2),
            FileSignals(path="c.py", source="", signals=[Signal(kind="k", detail="", weight=8)], score=8),
        ]
        review, skipped = prioritize(signals, min_score=3, max_files=10)
        assert [s.path for s in review] == ["c.py", "a.py"]
        assert [s.path for s in skipped] == ["b.py"]

    def test_prioritize_respects_max_files(self):
        from reviewhive.project.scanner import FileSignals, Signal

        signals = [
            FileSignals(path=f"f{i}.py", source="", score=10,
                        signals=[Signal(kind="k", detail="", weight=10)])
            for i in range(5)
        ]
        review, skipped = prioritize(signals, min_score=1, max_files=2)
        assert len(review) == 2
        assert len(skipped) == 3


class TestScanProject:
    def test_excludes_venv(self, tmp_path: Path):
        _write(tmp_path, "main.py", "x = 1\n")
        _write(tmp_path, ".venv/lib/site.py", "eval('x')\n")
        results = scan_project(tmp_path, _cfg)
        paths = [r.path for r in results]
        assert "main.py" in paths
        assert not any(".venv" in p for p in paths)

    def test_excludes_pycache(self, tmp_path: Path):
        _write(tmp_path, "main.py", "x = 1\n")
        _write(tmp_path, "__pycache__/cached.py", "eval('x')\n")
        results = scan_project(tmp_path, _cfg)
        paths = [r.path for r in results]
        assert not any("__pycache__" in p for p in paths)

    def test_single_file_mode(self, tmp_path: Path):
        f = _write(tmp_path, "solo.py", "eval('1')\n")
        results = scan_project(f, _cfg)
        assert len(results) == 1
        assert results[0].path == "solo.py"

    def test_empty_directory(self, tmp_path: Path):
        results = scan_project(tmp_path, _cfg)
        assert results == []


class TestStaticFindings:
    def test_dangerous_call_produces_finding(self, tmp_path: Path):
        f = _write(tmp_path, "main.py", "eval('x')\n")
        sig = scan_file(tmp_path, f)
        findings = static_findings(sig)
        assert len(findings) >= 1
        assert all(f.agent == "scanner" for f in findings)
        assert all(f.category == "static-analysis" for f in findings)

    def test_secret_produces_finding(self, tmp_path: Path):
        f = _write(tmp_path, "main.py", 'api_key = "abcdefgh1234"\n')
        sig = scan_file(tmp_path, f)
        findings = static_findings(sig)
        assert len(findings) >= 1

    def test_complexity_does_not_produce_finding(self, tmp_path: Path):
        body = "\n".join(f"    x{i} = {i}" for i in range(90))
        src = f"def f():\n{body}\n"
        f = _write(tmp_path, "main.py", src)
        sig = scan_file(tmp_path, f)
        complexity_signals = [s for s in sig.signals if s.kind == "complexity"]
        assert len(complexity_signals) >= 1
        findings = static_findings(sig)
        assert len(findings) == 0

    def test_keyword_does_not_produce_finding(self, tmp_path: Path):
        f = _write(tmp_path, "auth.py", "x = 1\n")
        sig = scan_file(tmp_path, f)
        kw_signals = [s for s in sig.signals if s.kind == "keyword"]
        assert len(kw_signals) >= 1
        findings = static_findings(sig)
        assert len(findings) == 0
