"""可插拔文本切片：为文档 / 数据集 / 知识库提供统一的切片能力。

扩展新策略只需三步：
1. 继承 Chunker 并实现 split()；
2. 在 CHUNKERS 注册表中登记；
3. 在 settings.yaml 的 rag.chunker.strategy 中启用。

所有策略共享 max_chars / overlap 参数；HeadingChunker 等结构化策略
在单节超长时用内部的 FixedWindowChunker（window 属性）做二次切分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", flags=re.MULTILINE)


@dataclass
class Section:
    title: str
    body: str


class Chunker:
    name = "base"

    def __init__(self, max_chars: int = 1500, overlap: int = 150):
        self.max_chars = max_chars
        self.overlap = overlap

    @property
    def window(self) -> "FixedWindowChunker":
        """定长窗口切片器，供超长内容的二次切分复用。"""
        return FixedWindowChunker(self.max_chars, self.overlap)

    def split(self, text: str) -> list[str]:
        raise NotImplementedError


class FixedWindowChunker(Chunker):
    """定长滑窗：按字符数硬切，优先在换行处回退，块间保留重叠。"""

    name = "fixed_window"

    def split(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= self.max_chars:
            return [text]
        pieces: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + self.max_chars, len(text))
            if end < len(text):
                newline = text.rfind("\n", start + self.max_chars // 2, end)
                if newline != -1:
                    end = newline + 1
            pieces.append(text[start:end].strip())
            if end >= len(text):
                break
            start = max(end - self.overlap, start + 1)
        return [piece for piece in pieces if piece]


class HeadingChunker(Chunker):
    """结构优先：按 Markdown 标题成块，单节超长再用窗口二次切分。"""

    name = "heading"

    def sections(self, text: str) -> list[Section]:
        """按标题拆分，返回 (标题, 正文)；标题行本身不进入正文。"""
        matches = list(_HEADING_RE.finditer(text))
        if not matches:
            body = text.strip()
            return [Section("", body)] if body else []

        result: list[Section] = []
        preamble = text[: matches[0].start()].strip()
        if preamble:
            result.append(Section(matches[0].group(2).strip(), preamble))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end() : end].strip()
            if body:
                result.append(Section(match.group(2).strip(), body))
        return result

    def split(self, text: str) -> list[str]:
        pieces: list[str] = []
        for section in self.sections(text):
            header = f"## {section.title}\n" if section.title else ""
            body = section.body
            if len(body) <= self.max_chars:
                pieces.append(header + body)
            else:
                pieces.extend(header + piece for piece in self.window.split(body))
        return pieces


CHUNKERS: dict[str, type[Chunker]] = {
    cls.name: cls for cls in (FixedWindowChunker, HeadingChunker)
}


def build_chunker(cfg) -> Chunker:
    """按配置构造切片器；cfg 需具备 strategy / max_chars / overlap 属性。"""
    cls = CHUNKERS.get(cfg.strategy)
    if cls is None:
        raise ValueError(f"未知切片策略：{cfg.strategy}，可选：{', '.join(CHUNKERS)}")
    return cls(max_chars=cfg.max_chars, overlap=cfg.overlap)


def chunk_text(text: str, max_chars: int = 1500, overlap: int = 150) -> list[str]:
    """便捷函数：定长窗口切片。"""
    return FixedWindowChunker(max_chars, overlap).split(text)
