"""接口签名提取与项目上下文渲染：供深度评审阶段注入工作区。"""
from __future__ import annotations

from reviewhive.project.graph import ImportGraph
from reviewhive.project.scanner import FileSignals


def render_signatures(sig: FileSignals, limit: int = 2000) -> str:
    lines: list[str] = [f"## {sig.path}"]

    constants: list[str] = []
    if sig.tree:
        import ast
        for node in ast.iter_child_nodes(sig.tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        constants.append(target.id)
    if constants:
        lines.append(f"CONSTANTS: {', '.join(constants[:10])}")

    for cls in sig.classes:
        if cls.name.startswith("_"):
            continue
        bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
        lines.append(f"class {cls.name}{bases_str}  # L{cls.lineno}")
        if cls.doc:
            doc_line = cls.doc.split("\n")[0][:80]
            lines.append(f'    """{doc_line}"""')
        for method in _get_class_methods(sig, cls.name):
            if method.name.startswith("_") and method.name != "__init__":
                continue
            async_prefix = "async " if method.is_async else ""
            returns = f" -> {method.returns}" if method.returns else ""
            lines.append(f"    {async_prefix}def {method.name}({method.args}){returns}  # L{method.lineno}")

    top_level_funcs = [f for f in sig.functions if not f.is_method]
    for func in top_level_funcs:
        if func.name.startswith("_"):
            continue
        async_prefix = "async " if func.is_async else ""
        returns = f" -> {func.returns}" if func.returns else ""
        lines.append(f"{async_prefix}def {func.name}({func.args}){returns}  # L{func.lineno}")

    result = "\n".join(lines)
    if len(result) > limit:
        result = result[:limit] + "\n...(截断)"
    return result


def _get_class_methods(sig: FileSignals, class_name: str) -> list:
    methods = []
    if sig.tree:
        import ast
        for node in ast.walk(sig.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for f in sig.functions:
                            if f.name == child.name and f.lineno == child.lineno:
                                methods.append(f)
                                break
                break
    return methods


def render_context(
    target: FileSignals,
    graph_: ImportGraph,
    by_path: dict[str, FileSignals],
    limit: int,
) -> str:
    sections: list[str] = ["# 项目上下文（静态扫描自动生成，非人工提交）"]

    risk_signals = [s for s in target.signals if s.kind in ("dangerous_call", "secret")]
    if risk_signals:
        sections.append("## 本文件静态风险信号")
        for s in risk_signals[:5]:
            line_info = f"L{s.lineno}" if s.lineno else ""
            sections.append(f"- {line_info} [{s.kind}] {s.detail}")

    imported = graph_.imported_by(target.path)
    if imported:
        sections.append("## 本文件导入的模块接口")
        remaining = limit - sum(len(s) for s in sections)
        per_file = max(200, remaining // max(len(imported), 1))
        for path in imported[:6]:
            if path in by_path:
                sig_block = render_signatures(by_path[path], limit=per_file)
                sections.append(sig_block)

    importers = graph_.importers_of(target.path)
    if importers:
        sections.append("## 导入本文件的模块接口")
        remaining = limit - sum(len(s) for s in sections)
        per_file = max(200, remaining // max(len(importers), 1))
        for path in importers[:6]:
            if path in by_path:
                sig_block = render_signatures(by_path[path], limit=per_file)
                sections.append(sig_block)

    result = "\n\n".join(sections)
    if len(result) > limit:
        result = result[:limit] + "\n...(截断)"
    return result
