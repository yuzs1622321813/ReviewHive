"""AST 静态扫描与规则评分：不使用任何模型，纯标准库。"""
from __future__ import annotations

import ast
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from reviewhive.config import ProjectReviewConfig
from reviewhive.core.schema import Finding

SENSITIVE_KEYWORDS: dict[str, int] = {
    "auth": 2, "login": 2, "password": 2, "passwd": 2, "secret": 2,
    "credential": 2, "crypto": 2, "encrypt": 2, "decrypt": 2, "sign": 1,
    "token": 1, "session": 1, "permission": 2, "admin": 1, "payment": 3,
    "sql": 1, "database": 1, "migrate": 1, "db": 1,
}
KEYWORD_SCORE_CAP = 6

DANGEROUS_CALLS: dict[str, tuple[int, str, str]] = {
    "eval": (8, "动态代码执行", "high"),
    "exec": (8, "动态代码执行", "high"),
    "compile": (4, "动态编译代码", "medium"),
    "__import__": (6, "动态导入", "medium"),
    "os.system": (7, "shell 命令执行", "high"),
    "os.popen": (6, "shell 命令执行", "high"),
    "subprocess.call": (5, "子进程调用", "high"),
    "subprocess.run": (5, "子进程调用", "high"),
    "subprocess.Popen": (5, "子进程调用", "high"),
    "pickle.load": (5, "不安全反序列化", "high"),
    "pickle.loads": (5, "不安全反序列化", "high"),
    "marshal.loads": (5, "不安全反序列化", "high"),
    "shelve.open": (4, "基于 pickle 的持久化", "medium"),
    "tempfile.mktemp": (3, "竞态临时文件", "medium"),
    "hashlib.md5": (2, "弱哈希算法", "medium"),
    "hashlib.sha1": (2, "弱哈希算法", "medium"),
    "yaml.load": (4, "不安全 YAML 反序列化", "high"),
}

CONDITIONAL_CALLS: dict[str, tuple[int, str, str]] = {
    "random.random": (3, "安全上下文使用非密码学随机数", "medium"),
    "random.randint": (3, "安全上下文使用非密码学随机数", "medium"),
    "random.choice": (3, "安全上下文使用非密码学随机数", "medium"),
}

HARDCODED_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passwd|password|credential)\b\s*=\s*['\"][A-Za-z0-9+/=_.\-]{8,}['\"]"
)

AUTH_KEYWORDS = {"auth", "login", "password", "passwd", "secret", "credential", "crypto", "encrypt", "decrypt", "token", "session", "permission"}


@dataclass
class ImportRef:
    module: str
    names: list[str]
    level: int
    lineno: int


@dataclass
class FunctionInfo:
    name: str
    lineno: int
    end_lineno: int
    args: str
    returns: str
    decorators: list[str]
    doc: str
    is_async: bool
    is_method: bool


@dataclass
class ClassInfo:
    name: str
    lineno: int
    bases: list[str]
    decorators: list[str]
    doc: str
    methods: list[FunctionInfo]


@dataclass
class Signal:
    kind: str
    detail: str
    lineno: int = 0
    weight: int = 0


@dataclass
class FileSignals:
    path: str
    source: str
    tree: ast.Module | None = None
    loc: int = 0
    imports: list[ImportRef] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    max_nesting: int = 0
    parse_error: str = ""
    score: int = 0


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
        return node.attr
    return ""


def _extract_args(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parts: list[str] = []
    args = func_node.args
    for i, arg in enumerate(args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        annotation = ""
        if arg.annotation:
            try:
                annotation = f": {ast.unparse(arg.annotation)}"
            except Exception:
                annotation = ""
        default_idx = i - (len(args.args) - len(args.defaults))
        default = " = ..." if default_idx >= 0 else ""
        parts.append(f"{arg.arg}{annotation}{default}")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        parts.append(arg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _extract_returns(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    if func_node.returns:
        try:
            return ast.unparse(func_node.returns)
        except Exception:
            pass
    return ""


def _max_nesting_depth(node: ast.AST, current: int = 0) -> int:
    nesting_nodes = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.ExceptHandler)
    max_depth = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, nesting_nodes):
            child_depth = _max_nesting_depth(child, current + 1)
            max_depth = max(max_depth, child_depth)
        else:
            child_depth = _max_nesting_depth(child, current)
            max_depth = max(max_depth, child_depth)
    return max_depth


def _has_safe_loader(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "Loader":
            loader_name = _dotted_name(kw.value) if isinstance(kw.value, (ast.Name, ast.Attribute)) else ""
            if "Safe" in loader_name:
                return True
    return False


def _scan_calls(tree: ast.Module, signals: list[Signal], has_auth_keyword: bool) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted_name(node.func) if isinstance(node.func, (ast.Name, ast.Attribute)) else ""
        if not name:
            continue

        if name.startswith("subprocess."):
            has_shell_true = any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            )
            if has_shell_true:
                signals.append(Signal(kind="dangerous_call", detail=f"{name}(shell=True) — shell 注入风险", lineno=node.lineno, weight=7))
                continue

        if name in DANGEROUS_CALLS:
            if name == "yaml.load" and _has_safe_loader(node):
                continue
            weight, desc, severity = DANGEROUS_CALLS[name]
            signals.append(Signal(kind="dangerous_call", detail=f"{name} — {desc}", lineno=node.lineno, weight=weight))
            continue

        if name in CONDITIONAL_CALLS and has_auth_keyword:
            weight, desc, severity = CONDITIONAL_CALLS[name]
            signals.append(Signal(kind="dangerous_call", detail=f"{name} — {desc}", lineno=node.lineno, weight=weight))


def _scan_exceptions(tree: ast.Module, signals: list[Signal]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        is_bare = node.type is None
        is_broad = isinstance(node.type, ast.Name) and node.type.id == "Exception"
        if not (is_bare or is_broad):
            continue
        body = node.body
        if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue)):
            signals.append(Signal(kind="exception_swallow", detail="吞异常: except + pass", lineno=node.lineno, weight=2))


def _scan_secrets(source: str, signals: list[Signal]) -> None:
    for match in HARDCODED_SECRET_RE.finditer(source):
        lineno = source[:match.start()].count("\n") + 1
        signals.append(Signal(kind="secret", detail=f"硬编码密钥: {match.group(1)}", lineno=lineno, weight=5))


def scan_file(root: Path, path: Path) -> FileSignals:
    rel = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return FileSignals(path=rel, source="", parse_error=str(exc))

    loc = len(source.splitlines())
    sig = FileSignals(path=rel, source=source, loc=loc)

    try:
        tree = ast.parse(source, filename=str(path))
        sig.tree = tree
    except SyntaxError as exc:
        sig.parse_error = str(exc)
        sig.signals.append(Signal(kind="parse_error", detail=f"语法错误: {exc}", weight=2))
        sig.score = score_file(sig)
        return sig

    lower_path = rel.lower()
    lower_source = source.lower()
    has_auth_keyword = False
    keyword_score = 0
    for kw, weight in SENSITIVE_KEYWORDS.items():
        if kw in lower_path or re.search(rf"\b{re.escape(kw)}\b", lower_source):
            keyword_score += weight
            if kw in AUTH_KEYWORDS:
                has_auth_keyword = True
    if keyword_score > 0:
        matched = [kw for kw in SENSITIVE_KEYWORDS if kw in lower_path or re.search(rf"\b{re.escape(kw)}\b", lower_source)]
        sig.signals.append(Signal(kind="keyword", detail=f"关键词: {', '.join(matched[:5])}", weight=min(keyword_score, KEYWORD_SCORE_CAP)))

    _scan_secrets(source, sig.signals)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    sig.imports.append(ImportRef(module=alias.name, names=[alias.name], level=0, lineno=node.lineno))
            else:
                module = node.module or ""
                names = [alias.name for alias in node.names]
                sig.imports.append(ImportRef(module=module, names=names, level=node.level, lineno=node.lineno))

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_method = False
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef) and node in list(ast.iter_child_nodes(parent)):
                    is_method = True
                    break
            end_lineno = getattr(node, "end_lineno", node.lineno) or node.lineno
            doc = ast.get_docstring(node) or ""
            decorators = []
            for dec in node.decorator_list:
                try:
                    decorators.append(ast.unparse(dec))
                except Exception:
                    decorators.append("")
            func_info = FunctionInfo(
                name=node.name,
                lineno=node.lineno,
                end_lineno=end_lineno,
                args=_extract_args(node),
                returns=_extract_returns(node),
                decorators=decorators,
                doc=doc,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                is_method=is_method,
            )
            if is_method:
                for cls_node in ast.walk(tree):
                    if isinstance(cls_node, ast.ClassDef):
                        for child in ast.iter_child_nodes(cls_node):
                            if child is node:
                                break
            sig.functions.append(func_info)

            nesting = _max_nesting_depth(node)
            sig.max_nesting = max(sig.max_nesting, nesting)
            if nesting >= 5:
                sig.signals.append(Signal(kind="complexity", detail=f"嵌套深度 {nesting}", lineno=node.lineno, weight=3))
            elif nesting >= 4:
                sig.signals.append(Signal(kind="complexity", detail=f"嵌套深度 {nesting}", lineno=node.lineno, weight=2))

            func_lines = end_lineno - node.lineno
            if func_lines > 80:
                sig.signals.append(Signal(kind="complexity", detail=f"长函数 {func_lines} 行", lineno=node.lineno, weight=1))

            arg_count = len(node.args.args)
            if node.args.vararg:
                arg_count += 1
            if node.args.kwarg:
                arg_count += 1
            arg_count += len(node.args.kwonlyargs)
            if arg_count > 6:
                sig.signals.append(Signal(kind="complexity", detail=f"多参数 {arg_count} 个", lineno=node.lineno, weight=1))

        if isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            decorators = []
            for dec in node.decorator_list:
                try:
                    decorators.append(ast.unparse(dec))
                except Exception:
                    decorators.append("")
            bases = []
            for base in node.bases:
                try:
                    bases.append(ast.unparse(base))
                except Exception:
                    bases.append("")
            methods = [f for f in sig.functions if f.is_method and any(
                node in (p for p in ast.walk(tree) if isinstance(p, ast.ClassDef))
                for p in [node]
            )]
            sig.classes.append(ClassInfo(
                name=node.name,
                lineno=node.lineno,
                bases=bases,
                decorators=decorators,
                doc=doc,
                methods=[],
            ))

    _scan_calls(tree, sig.signals, has_auth_keyword)
    _scan_exceptions(tree, sig.signals)

    if len(sig.functions) > 15:
        sig.signals.append(Signal(kind="complexity", detail=f"函数数量 {len(sig.functions)}", weight=1))

    sig.score = score_file(sig)
    return sig


def score_file(sig: FileSignals) -> int:
    return sum(s.weight for s in sig.signals)


def scan_project(root: Path, cfg: ProjectReviewConfig) -> list[FileSignals]:
    root = root.resolve()
    if root.is_file():
        return [scan_file(root.parent, root)]

    exclude_set = set(cfg.exclude)
    results: list[FileSignals] = []
    py_files: list[Path] = []

    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if any(part in exclude_set or part.startswith(".") for part in parts[:-1]):
            continue
        if path.name.startswith("."):
            continue
        py_files.append(path)

    py_files.sort(key=lambda p: p.relative_to(root).as_posix())

    for path in py_files[:cfg.max_scan_files]:
        try:
            sig = scan_file(root, path)
            results.append(sig)
        except Exception as exc:
            rel = path.relative_to(root).as_posix()
            results.append(FileSignals(path=rel, source="", parse_error=str(exc)))

    return results


def prioritize(
    signals: list[FileSignals],
    min_score: int,
    max_files: int,
) -> tuple[list[FileSignals], list[FileSignals]]:
    ranked = sorted(signals, key=lambda s: (-s.score, s.path))
    review: list[FileSignals] = []
    skipped: list[FileSignals] = []
    for sig in ranked:
        if sig.score >= min_score and len(review) < max_files:
            review.append(sig)
        else:
            skipped.append(sig)
    return review, skipped


def static_findings(sig: FileSignals) -> list[Finding]:
    findings: list[Finding] = []
    for signal in sig.signals:
        if signal.kind not in ("dangerous_call", "secret"):
            continue
        severity = "high" if signal.kind == "dangerous_call" else "high"
        if signal.weight <= 3:
            severity = "medium"
        findings.append(Finding(
            id=uuid.uuid4().hex[:8],
            agent="scanner",
            severity=severity,
            category="static-analysis",
            file=sig.path,
            lines=str(signal.lineno) if signal.lineno else "",
            title=signal.detail,
            description=signal.detail,
            suggestion=f"请人工审查 {sig.path}:{signal.lineno} 处的 {signal.kind} 问题。",
            confidence=1.0,
        ))
    return findings
