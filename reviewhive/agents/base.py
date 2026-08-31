"""子 Agent 基类：以 JSON 协议驱动技能循环，最终产出结构化 findings。"""
from __future__ import annotations

from typing import Awaitable, Callable

from reviewhive.agents.profiles import AgentProfile
from reviewhive.core.schema import (
    SEVERITIES,
    AgentResult,
    Finding,
    RetrievedChunk,
    StreamEvent,
)
from reviewhive.models.llm import LLMClient, extract_json
from reviewhive.observability import INPUT_VALUE, KIND_AGENT, OUTPUT_VALUE, span
from reviewhive.skills.context import SkillContext
from reviewhive.skills.registry import SkillRegistry

Emit = Callable[[StreamEvent], Awaitable[None]]

_FINDER_FIELDS = set(Finding.model_fields)

_OUTPUT_CONTRACT = """输出契约——每一轮只输出一个 JSON 对象，禁止输出 JSON 以外的任何内容：
1) 需要调用技能获取信息：{"action": "skill", "skill": "<技能名>", "arguments": {...}}
2) 信息充分，给出结论：{"action": "final", "findings": [...], "notes": "..."}

finding 对象字段：
{"severity": "critical|high|medium|low|info", "category": "问题类别", "file": "文件名", \
"lines": "行号或区间", "title": "一句话标题", "description": "问题描述与证据", \
"suggestion": "修改建议", "code_snippet": "相关代码片段", "references": ["引用的知识库chunk id"], \
"confidence": 0.0~1.0}"""


class SubAgent:
    def __init__(
        self,
        profile: AgentProfile,
        llm: LLMClient,
        registry: SkillRegistry,
        max_loops: int = 4,
    ):
        self.profile = profile
        self.llm = llm
        self.registry = registry
        self.max_loops = max_loops

    def _system_prompt(self) -> str:
        principles = "\n".join(f"- {item}" for item in self.profile.principles)
        return (
            f"你是 ReviewHive 代码评审团队中的「{self.profile.title}」（代号 {self.profile.name}）。\n"
            f"使命：{self.profile.goal}\n"
            f"评审原则：\n{principles}\n\n"
            f"{self.registry.render_prompt()}\n\n"
            f"{_OUTPUT_CONTRACT}"
        )

    def _task_brief(self, ctx: SkillContext, focus_points: list[str], context_chunks: list[RetrievedChunk]) -> str:
        parts: list[str] = ["待评审材料："]
        combined = ctx.workspace.combined(limit=12000)
        parts.append(combined if combined else "（无代码，仅有附件）")
        if ctx.workspace.diff and len(ctx.workspace.diff) <= 4000:
            parts.append(f"\nDiff：\n{ctx.workspace.diff}")
        if focus_points:
            parts.append("\n主 Agent 关注点：" + "；".join(focus_points))
        if context_chunks:
            kb = "\n\n".join(
                f"[{hit.chunk.id}] ({hit.chunk.kind})\n{hit.chunk.display_text()[:900]}"
                for hit in context_chunks[:4]
            )
            parts.append(f"\n主 Agent 预检索的知识库上下文（可用 references 引用其 id）：\n{kb}")
        images = ctx.extra.get("images", [])
        if images:
            parts.append("\n用户附带图片：" + ", ".join(img.name for img in images))
        memory_notes = ctx.extra.get("memory_notes", "")
        if memory_notes:
            parts.append(f"\n{memory_notes}")
        parts.append("\n请开始评审。若信息不足，先调用技能补充信息。")
        return "\n".join(parts)

    async def run(
        self,
        ctx: SkillContext,
        focus_points: list[str],
        context_chunks: list[RetrievedChunk],
        emit: Emit,
    ) -> AgentResult:
        input_summary = f"focus={';'.join(focus_points[:3])}" if focus_points else "no-focus"
        with span(f"agent.{self.profile.name}", KIND_AGENT, {"agent.title": self.profile.title, INPUT_VALUE: input_summary}) as agent_span:
            result = await self._run_inner(ctx, focus_points, context_chunks, emit)
            if agent_span:
                agent_span.set_attribute(OUTPUT_VALUE, f"findings={len(result.findings)}")
            return result

    async def _run_inner(
        self,
        ctx: SkillContext,
        focus_points: list[str],
        context_chunks: list[RetrievedChunk],
        emit: Emit,
    ) -> AgentResult:
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._task_brief(ctx, focus_points, context_chunks)},
        ]
        await emit(StreamEvent(type="agent_start", data={"agent": self.profile.name}))

        for round_no in range(1, self.max_loops + 2):
            force_final = round_no > self.max_loops
            if force_final:
                messages.append({"role": "user", "content": "技能调用次数已达上限，请直接输出 final JSON。"})
            raw = await self.llm.chat(messages, json_mode=True)
            messages.append({"role": "assistant", "content": raw})
            try:
                decision = extract_json(raw)
            except ValueError:
                messages.append({"role": "user", "content": "输出不是合法 JSON，请严格遵守输出契约重试。"})
                continue

            action = str(decision.get("action", "")).strip()
            if action == "final" or force_final:
                findings = _parse_findings(decision.get("findings", []), self.profile.name, ctx)
                notes = str(decision.get("notes", "") or "")
                await emit(
                    StreamEvent(
                        type="agent_done",
                        data={"agent": self.profile.name, "findings": len(findings)},
                    )
                )
                return AgentResult(agent=self.profile.name, findings=findings, notes=notes)

            if action == "skill":
                skill_name = str(decision.get("skill", ""))
                arguments = decision.get("arguments") or {}
                await emit(
                    StreamEvent(
                        type="skill_call",
                        data={"agent": self.profile.name, "skill": skill_name, "arguments": arguments, "round": round_no},
                    )
                )
                result = await self.registry.execute(skill_name, arguments, ctx)
                messages.append({"role": "user", "content": f"技能 {skill_name} 的执行结果：\n{result[:6000]}"})
                continue

            messages.append({"role": "user", "content": f"未知 action：{action}。请遵守输出契约。"})

        await emit(StreamEvent(type="agent_done", data={"agent": self.profile.name, "findings": 0}))
        return AgentResult(agent=self.profile.name, findings=[], notes="达到最大轮次仍未输出结论")


def _parse_findings(raw: object, agent_name: str, ctx: SkillContext) -> list[Finding]:
    findings: list[Finding] = []
    if not isinstance(raw, list):
        return findings
    default_file = next(iter(ctx.workspace.names()), "")
    for item in raw:
        if not isinstance(item, dict):
            continue
        filtered = {key: value for key, value in item.items() if key in _FINDER_FIELDS}
        try:
            finding = Finding(**filtered)
        except Exception:
            continue
        finding.agent = agent_name
        if finding.severity not in SEVERITIES:
            finding.severity = "medium"
        if not finding.file:
            finding.file = default_file
        findings.append(finding)
    return findings
