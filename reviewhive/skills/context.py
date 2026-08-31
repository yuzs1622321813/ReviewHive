"""技能执行上下文：待评审代码工作区 + 检索/多模态依赖。"""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewhive.models.vision import VisionClient
from reviewhive.rag.retriever import HybridRetriever


class ReviewWorkspace:
    """一次评审会话中提交的代码（文件名 -> 内容）与 diff。"""

    def __init__(self, diff: str = ""):
        self.files: dict[str, str] = {}
        self.diff = diff

    def add_file(self, name: str, content: str) -> None:
        self.files[name or f"file_{len(self.files) + 1}.txt"] = content

    def names(self) -> list[str]:
        return list(self.files)

    def read(self, name: str) -> str | None:
        return self.files.get(name)

    def combined(self, limit: int = 40000) -> str:
        parts: list[str] = []
        total = 0
        for name, content in self.files.items():
            block = f"--- {name} ---\n{content}\n"
            if total + len(block) > limit:
                parts.append("...（其余内容超长已截断）")
                break
            parts.append(block)
            total += len(block)
        return "".join(parts)


@dataclass
class SkillContext:
    workspace: ReviewWorkspace
    retriever: HybridRetriever | None = None
    vision: VisionClient | None = None
    extra: dict = field(default_factory=dict)
