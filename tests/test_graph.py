"""导入图构建与查询的单元测试。"""
from __future__ import annotations

from pathlib import Path

from reviewhive.project.graph import ImportGraph, _path_to_module
from reviewhive.project.scanner import FileSignals, ImportRef, scan_file


def _write(root: Path, rel: str, source: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


def _scan_all(root: Path) -> list[FileSignals]:
    results = []
    for py in sorted(root.rglob("*.py")):
        results.append(scan_file(root, py))
    return results


class TestPathToModule:
    def test_simple(self):
        assert _path_to_module("pkg/auth.py") == "pkg.auth"

    def test_init(self):
        assert _path_to_module("pkg/__init__.py") == "pkg"

    def test_nested(self):
        assert _path_to_module("a/b/c.py") == "a.b.c"


class TestImportGraphBuild:
    def test_absolute_import(self, tmp_path: Path):
        _write(tmp_path, "pkg/__init__.py", "")
        _write(tmp_path, "pkg/models.py", "")
        _write(tmp_path, "pkg/auth.py", "import pkg.models\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        assert "pkg/models.py" in graph.imported_by("pkg/auth.py")

    def test_from_import(self, tmp_path: Path):
        _write(tmp_path, "pkg/__init__.py", "")
        _write(tmp_path, "pkg/auth.py", "def login(): pass\n")
        _write(tmp_path, "pkg/api.py", "from pkg.auth import login\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        assert "pkg/auth.py" in graph.imported_by("pkg/api.py")

    def test_relative_import(self, tmp_path: Path):
        _write(tmp_path, "pkg/__init__.py", "")
        _write(tmp_path, "pkg/models.py", "Base = object\n")
        _write(tmp_path, "pkg/utils.py", "from .models import Base\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        assert "pkg/models.py" in graph.imported_by("pkg/utils.py")

    def test_importers_of(self, tmp_path: Path):
        _write(tmp_path, "pkg/__init__.py", "")
        _write(tmp_path, "pkg/models.py", "")
        _write(tmp_path, "pkg/auth.py", "import pkg.models\n")
        _write(tmp_path, "pkg/utils.py", "from .models import Base\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        importers = graph.importers_of("pkg/models.py")
        assert "pkg/auth.py" in importers
        assert "pkg/utils.py" in importers

    def test_third_party_import_no_edge(self, tmp_path: Path):
        _write(tmp_path, "main.py", "import os\nimport json\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        assert graph.imported_by("main.py") == []

    def test_self_import_ignored(self, tmp_path: Path):
        _write(tmp_path, "pkg/__init__.py", "import pkg\n")
        signals = _scan_all(tmp_path)
        graph = ImportGraph.build(tmp_path, signals)
        assert "pkg/__init__.py" not in graph.imported_by("pkg/__init__.py")

    def test_empty_project(self, tmp_path: Path):
        graph = ImportGraph.build(tmp_path, [])
        assert graph.forward == {}
        assert graph.reverse == {}
