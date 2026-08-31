"""接口签名渲染与项目上下文生成的单元测试。"""
from __future__ import annotations

import ast

from reviewhive.project.graph import ImportGraph
from reviewhive.project.interfaces import render_context, render_signatures
from reviewhive.project.scanner import (
    ClassInfo,
    FileSignals,
    FunctionInfo,
    Signal,
    scan_file,
)
from pathlib import Path


def _make_sig(
    path: str = "app/service.py",
    source: str = "",
    classes: list[ClassInfo] | None = None,
    functions: list[FunctionInfo] | None = None,
    signals: list[Signal] | None = None,
) -> FileSignals:
    tree = ast.parse(source) if source else None
    return FileSignals(
        path=path,
        source=source,
        tree=tree,
        classes=classes or [],
        functions=functions or [],
        signals=signals or [],
    )


class TestRenderSignatures:
    def test_class_declaration(self):
        src = "class UserService:\n    def get_user(self): pass\n"
        sig = _make_sig(source=src, classes=[
            ClassInfo(name="UserService", lineno=1, bases=[], decorators=[], doc="", methods=[]),
        ], functions=[
            FunctionInfo(name="get_user", lineno=2, end_lineno=2, args="", returns="",
                         decorators=[], doc="", is_async=False, is_method=True),
        ])
        out = render_signatures(sig)
        assert "class UserService" in out
        assert "def get_user" in out

    def test_private_method_excluded(self):
        src = "class Svc:\n    def _helper(self): pass\n    def __init__(self): pass\n"
        sig = _make_sig(source=src, classes=[
            ClassInfo(name="Svc", lineno=1, bases=[], decorators=[], doc="", methods=[]),
        ], functions=[
            FunctionInfo(name="_helper", lineno=2, end_lineno=2, args="", returns="",
                         decorators=[], doc="", is_async=False, is_method=True),
            FunctionInfo(name="__init__", lineno=3, end_lineno=3, args="", returns="",
                         decorators=[], doc="", is_async=False, is_method=True),
        ])
        out = render_signatures(sig)
        assert "_helper" not in out
        assert "__init__" in out

    def test_private_class_excluded(self):
        sig = _make_sig(classes=[
            ClassInfo(name="_Internal", lineno=1, bases=[], decorators=[], doc="", methods=[]),
        ])
        out = render_signatures(sig)
        assert "_Internal" not in out

    def test_line_number_annotation(self):
        sig = _make_sig(functions=[
            FunctionInfo(name="process", lineno=15, end_lineno=20, args="x: int", returns="bool",
                         decorators=[], doc="", is_async=False, is_method=False),
        ])
        out = render_signatures(sig)
        assert "# L15" in out
        assert "x: int" in out
        assert "-> bool" in out

    def test_default_value_omitted(self):
        sig = _make_sig(functions=[
            FunctionInfo(name="f", lineno=1, end_lineno=1, args="x: int = ..., y: str = ...",
                         returns="", decorators=[], doc="", is_async=False, is_method=False),
        ])
        out = render_signatures(sig)
        assert "= ..." in out

    def test_async_function(self):
        sig = _make_sig(functions=[
            FunctionInfo(name="fetch", lineno=5, end_lineno=10, args="url: str", returns="dict",
                         decorators=[], doc="", is_async=True, is_method=False),
        ])
        out = render_signatures(sig)
        assert "async def fetch" in out

    def test_top_level_private_excluded(self):
        sig = _make_sig(functions=[
            FunctionInfo(name="_internal", lineno=1, end_lineno=2, args="", returns="",
                         decorators=[], doc="", is_async=False, is_method=False),
        ])
        out = render_signatures(sig)
        assert "_internal" not in out

    def test_limit_truncation(self):
        sig = _make_sig(functions=[
            FunctionInfo(name=f"func_{i}", lineno=i, end_lineno=i + 1, args="", returns="",
                         decorators=[], doc="", is_async=False, is_method=False)
            for i in range(100)
        ])
        out = render_signatures(sig, limit=200)
        assert len(out) <= 210
        assert "...(截断)" in out


class TestRenderContext:
    def test_risk_signals_section(self):
        sig = _make_sig(signals=[
            Signal(kind="dangerous_call", detail="eval — 动态代码执行", lineno=8, weight=8),
        ])
        graph = ImportGraph()
        out = render_context(sig, graph, {}, limit=4000)
        assert "本文件静态风险信号" in out
        assert "eval" in out

    def test_imported_modules_section(self, tmp_path: Path):
        target = _make_sig(path="pkg/api.py")
        dep = _make_sig(
            path="pkg/models.py",
            source="class User: pass\n",
            classes=[ClassInfo(name="User", lineno=1, bases=[], decorators=[], doc="", methods=[])],
        )
        graph = ImportGraph()
        graph.forward["pkg/api.py"] = {"pkg/models.py"}
        by_path = {"pkg/api.py": target, "pkg/models.py": dep}
        out = render_context(target, graph, by_path, limit=4000)
        assert "本文件导入的模块接口" in out
        assert "User" in out

    def test_importers_section(self):
        target = _make_sig(path="pkg/models.py")
        caller = _make_sig(
            path="pkg/api.py",
            source="def get_user(): pass\n",
            functions=[FunctionInfo(name="get_user", lineno=1, end_lineno=2, args="", returns="",
                                    decorators=[], doc="", is_async=False, is_method=False)],
        )
        graph = ImportGraph()
        graph.reverse["pkg/models.py"] = {"pkg/api.py"}
        by_path = {"pkg/models.py": target, "pkg/api.py": caller}
        out = render_context(target, graph, by_path, limit=4000)
        assert "导入本文件的模块接口" in out
        assert "get_user" in out

    def test_context_limit_truncation(self):
        sig = _make_sig(signals=[
            Signal(kind="dangerous_call", detail=f"signal_{'x' * 80}", lineno=i, weight=5)
            for i in range(5)
        ])
        graph = ImportGraph()
        out = render_context(sig, graph, {}, limit=300)
        assert len(out) <= 310
        assert "...(截断)" in out
