"""技能注册表：统一登记、自省（供系统提示词渲染）与执行。"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from reviewhive.observability import KIND_TOOL, span
from reviewhive.skills.context import SkillContext

Handler = Callable[[dict[str, Any], SkillContext], Any]


@dataclass
class Skill:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Handler
    tags: list[str] = field(default_factory=list)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": skill.name, "description": skill.description, "parameters": skill.parameters}
            for skill in self._skills.values()
        ]

    def render_prompt(self) -> str:
        lines = ["可用技能："]
        for skill in self._skills.values():
            params = ", ".join(
                f"{pname}: {pspec.get('type', 'any')}" for pname, pspec in skill.parameters.get("properties", {}).items()
            )
            lines.append(f"- {skill.name}({params})：{skill.description}")
        return "\n".join(lines)

    async def execute(self, name: str, arguments: dict[str, Any], ctx: SkillContext) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return f"错误：未知技能 {name}，可用技能：{', '.join(self._skills)}"
        with span(f"skill.{name}", KIND_TOOL, {"arguments": json.dumps(arguments, ensure_ascii=False)[:500]}):
            try:
                result = skill.handler(arguments or {}, ctx)
                if inspect.isawaitable(result):
                    result = await result
                return str(result)
            except Exception as exc:
                return f"技能 {name} 执行失败：{exc}"
