"""贯穿全链路的数据结构：知识库块、评审输入/计划/发现项/报告。"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
SEVERITIES: list[str] = ["critical", "high", "medium", "low", "info"]


class KBChunk(BaseModel):
    """知识库中的一个检索单元（评审样例 / 漏洞模式 / 最佳实践）。"""

    id: str = ""
    source: str = ""
    kind: Literal["review_example", "vulnerability", "best_practice"] = "best_practice"
    title: str = ""
    content: str = ""
    code: str = ""
    language: str = ""

    def display_text(self) -> str:
        parts = [self.title, self.content]
        if self.code:
            parts.append(self.code)
        return "\n".join(part for part in parts if part)

    def fingerprint(self) -> str:
        raw = f"{self.source}|{self.kind}|{self.title}|{self.display_text()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def ensure_id(self) -> "KBChunk":
        if not self.id:
            self.id = self.fingerprint()
        return self


class RetrievedChunk(BaseModel):
    chunk: KBChunk
    score: float = 0.0
    sources: list[str] = Field(default_factory=list)  # vector / keyword


class ImageAttachment(BaseModel):
    name: str = "image.png"
    mime: str = "image/png"
    data_b64: str = ""


class ReviewInput(BaseModel):
    code: str = ""
    diff: str = ""
    filename: str = ""
    language: str = ""
    images: list[ImageAttachment] = Field(default_factory=list)

    def primary_text(self) -> str:
        return self.code or self.diff


class AgentPlan(BaseModel):
    """主 Agent 对一次评审任务的调度计划（混合编排的动态部分）。"""

    language: str = "java"
    sub_agents: list[str] = Field(default_factory=list)
    focus_points: list[str] = Field(default_factory=list)
    vision_required: bool = False


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    agent: str = ""
    severity: Severity = "medium"
    category: str = ""
    file: str = ""
    lines: str = ""
    title: str = ""
    description: str = ""
    suggestion: str = ""
    code_snippet: str = ""
    references: list[str] = Field(default_factory=list)  # 引用的知识库 chunk id
    confidence: float = 0.8


class AgentResult(BaseModel):
    agent: str
    findings: list[Finding] = Field(default_factory=list)
    notes: str = ""


class ReviewReport(BaseModel):
    session_id: str = ""
    created_at: float = Field(default_factory=time.time)
    language: str = ""
    plan: AgentPlan = Field(default_factory=AgentPlan)
    context_refs: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    duration_ms: int = 0
    status: Literal["running", "done", "failed"] = "running"

    def severity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {level: 0 for level in SEVERITIES}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


class StreamEvent(BaseModel):
    """推送给前端的 SSE 事件。"""

    type: str  # phase | plan | agent_start | skill_call | agent_done | report | error
    data: dict[str, Any] = Field(default_factory=dict)
