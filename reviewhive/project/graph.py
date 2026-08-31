"""模块解析与导入图：best-effort，解析失败只记录不报错。"""
from __future__ import annotations

from pathlib import Path

from reviewhive.project.scanner import FileSignals


class ImportGraph:
    forward: dict[str, set[str]]
    reverse: dict[str, set[str]]

    def __init__(self) -> None:
        self.forward = {}
        self.reverse = {}

    @classmethod
    def build(cls, root: Path, signals: list[FileSignals]) -> "ImportGraph":
        graph = cls()
        module_map: dict[str, list[str]] = {}

        for sig in signals:
            mod_name = _path_to_module(sig.path)
            module_map.setdefault(mod_name, []).append(sig.path)
            if sig.path.endswith("/__init__.py"):
                pkg = mod_name.rsplit(".", 1)[0] if "." in mod_name else mod_name.replace(".__init__", "")
                if pkg and pkg != mod_name:
                    module_map.setdefault(pkg, []).append(sig.path)

        for sig in signals:
            src = sig.path
            mod_name = _path_to_module(src)
            parts = mod_name.split(".")
            if mod_name.endswith(".__init__"):
                package_parts = parts[:-1]
            else:
                package_parts = parts[:-1]

            for imp in sig.imports:
                targets = _resolve_import(imp, package_parts, module_map, src)
                for target in targets:
                    if target == src:
                        continue
                    graph.forward.setdefault(src, set()).add(target)
                    graph.reverse.setdefault(target, set()).add(src)

        return graph

    def imported_by(self, path: str) -> list[str]:
        return sorted(self.forward.get(path, set()))

    def importers_of(self, path: str) -> list[str]:
        return sorted(self.reverse.get(path, set()))


def _path_to_module(path: str) -> str:
    mod = path.replace("/", ".").replace("\\", ".")
    if mod.endswith(".py"):
        mod = mod[:-3]
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    return mod


def _resolve_import(
    imp,
    package_parts: list[str],
    module_map: dict[str, list[str]],
    src: str,
) -> list[str]:
    targets: list[str] = []

    if imp.level > 0:
        base_parts = package_parts[:]
        strip = imp.level - 1
        if strip > len(base_parts):
            return []
        if strip > 0:
            base_parts = base_parts[:-strip]
        base = ".".join(base_parts)

        if imp.module:
            candidate = f"{base}.{imp.module}" if base else imp.module
        else:
            candidate = base

        if not candidate:
            return []

        if candidate in module_map:
            targets.extend(module_map[candidate])
        else:
            for name in imp.names:
                sub = f"{candidate}.{name}"
                if sub in module_map:
                    targets.extend(module_map[sub])

        if not targets:
            for mod_key, paths in module_map.items():
                if mod_key.endswith(f".{candidate}") or mod_key == candidate:
                    targets.extend(paths)

    else:
        module = imp.module
        if module in module_map:
            targets.extend(module_map[module])
        else:
            for name in imp.names:
                sub = f"{module}.{name}"
                if sub in module_map:
                    targets.extend(module_map[sub])

        if not targets:
            candidates = []
            for mod_key in module_map:
                if mod_key.endswith(f".{module}"):
                    candidates.append(mod_key)
            if len(candidates) == 1:
                targets.extend(module_map[candidates[0]])
            elif len(candidates) > 1:
                src_top = src.split("/")[0]
                for c in candidates:
                    if c.startswith(src_top):
                        targets.extend(module_map[c])
                        break

    return list(set(targets))
