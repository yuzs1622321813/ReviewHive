"""主 Agent：评审规划（动态调度决策）与结果汇总（去重、定级、总结）。"""
from __future__ import annotations

import json
import logging

from reviewhive.core.schema import (
    SEVERITIES,
    AgentPlan,
    AgentResult,
    Finding,
    ImageAttachment,
)
from reviewhive.models.llm import LLMClient
from reviewhive.skills.context import ReviewWorkspace

logger = logging.getLogger(__name__)

_PLAN_PROMPT = """你是 ReviewHive 的主控 Agent（Orchestrator）。请为一次代码评审制定调度计划。

可用的子 Agent：
- security：安全漏洞评审
- performance：性能反模式评审
- style：编码规范与可维护性评审
- test：测试缺口与单测建议
- vision：多模态图片解读（仅当用户附带图片且值得解读时启用）

请只输出一个 JSON 对象：
{"language": "代码主要语言", "sub_agents": ["要调度的子 Agent 代号"], "focus_points": ["3-6 个本次评审重点"], "vision_required": true/false}

要求：
1. 常规代码评审至少包含 security、performance、style；
2. focus_points 必须结合代码实际内容（如具体类/方法/风险），不要写套话；
3. 没有图片时 vision_required 必须为 false。"""

_AGGREGATE_PROMPT = """你是 ReviewHive 的主控 Agent。下面是各子 Agent 的代码评审发现（JSON 数组）。

请完成：
1. 合并重复/高度相似的发现，保留证据更充分的一条；
2. 校准 severity（critical 仅限可被直接利用或必然引发事故的问题）；
3. 修正缺失字段，保证 file/title/description/suggestion 完整；
4. 总数不超过 30 条，按重要性取舍。

只输出一个 JSON 对象：
{"findings": [与输入同结构的 finding 数组], "summary": "120 字以内的整体结论：质量评价 + 最关键风险 + 建议优先级"}"""


class Orchestrator:
    def __init__(self, llm: LLMClient, configured_agents: list[str], vision_enabled: bool):
        self.llm = llm
        self.configured_agents = configured_agents
        self.vision_enabled = vision_enabled

    async def plan(self, workspace: ReviewWorkspace, images: list[ImageAttachment]) -> AgentPlan:
        fallback = AgentPlan(
            language="java" if any(name.endswith(".java") for name in workspace.names()) else "unknown",
            sub_agents=list(self.configured_agents),
            focus_points=[],
            vision_required=bool(images) and self.vision_enabled,
        )
        material = workspace.combined(limit=6000)
        diff_head = workspace.diff[:2000]
        user_content = (
            f"提交的文件：{', '.join(workspace.names()) or '（无）'}\n"
            f"{'附带的图片：' + ', '.join(img.name for img in images) if images else '没有附带图片'}\n"
            f"代码内容（可能截断）：\n{material}\n"
            + (f"Diff（可能截断）：\n{diff_head}" if diff_head else "")
        )
        try:
            data = await self.llm.chat_json(
                [
                    {"role": "system", "content": _PLAN_PROMPT},
                    {"role": "user", "content": user_content},
                ]
            )
            plan = AgentPlan(
                language=str(data.get("language", fallback.language)),
                sub_agents=[name for name in data.get("sub_agents", []) if isinstance(name, str)],
                focus_points=[str(point) for point in data.get("focus_points", [])][:6],
                vision_required=bool(data.get("vision_required", False)),
            )
        except Exception as exc:
            logger.warning("规划失败，使用默认计划: %s", exc)
            return fallback

        plan.sub_agents = [name for name in plan.sub_agents if name in self.configured_agents or name == "vision"]
        if not plan.sub_agents:
            plan.sub_agents = list(self.configured_agents)
        if images and self.vision_enabled and "vision" not in plan.sub_agents and plan.vision_required:
            plan.sub_agents.append("vision")
        return plan

    async def aggregate(self, results: list[AgentResult]) -> tuple[list[Finding], str]:
        findings: list[Finding] = [finding for result in results for finding in result.findings]
        if not findings:
            notes = "；".join(result.notes for result in results if result.notes)
            return [], f"各子 Agent 未发现需要报告的问题。{notes}".strip()

        try:
            compact = [finding.model_dump() for finding in findings]
            data = await self.llm.chat_json(
                [
                    {"role": "system", "content": _AGGREGATE_PROMPT},
                    {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
                ],
                max_tokens=8192,
            )
            merged: list[Finding] = []
            for item in data.get("findings", []):
                if not isinstance(item, dict):
                    continue
                try:
                    finding = Finding(**{k: v for k, v in item.items() if k in Finding.model_fields})
                except Exception:
                    continue
                if finding.severity not in SEVERITIES:
                    finding.severity = "medium"
                merged.append(finding)
            summary = str(data.get("summary", "") or "")
            if merged:
                return merged[:30], summary or _fallback_summary(merged)
        except Exception as exc:
            logger.warning("汇总失败，使用原始发现: %s", exc)
        return findings, _fallback_summary(findings)


def _fallback_summary(findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [f"{level} {count} 项" for level, count in counts.items()]
    return f"共发现 {len(findings)} 个问题（{'、'.join(parts)}），汇总模型不可用，已按原始结果输出。"
