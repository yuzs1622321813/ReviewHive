"""内置技能：工作区读取、代码搜索、知识库检索、diff 分析、图片解读。"""
from __future__ import annotations

import asyncio
import base64
import difflib
import re

from reviewhive.skills.context import SkillContext
from reviewhive.skills.registry import Skill, SkillRegistry

_MAX_GREP_MATCHES = 40
_MAX_KB_CHARS = 4200


# 工厂函数：创建 list_files 技能，列出工作区中所有已提交的代码文件及其字符数
def _skill_list_files() -> Skill:
    # handler：遍历工作区文件列表，输出文件名和大小；工作区为空时返回提示
    def handler(args: dict, ctx: SkillContext) -> str:
        names = ctx.workspace.names()
        if not names:
            return "工作区为空（未提交代码文件）"
        lines = [f"- {name}（{len(content)} 字符）" for name, content in ctx.workspace.files.items()]
        return "\n".join(lines)

    return Skill(
        name="list_files",
        description="列出本次提交评审的所有代码文件及大小",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


# 工厂函数：创建 read_file 技能，按文件名读取工作区中的代码文件并返回带行号的内容
def _skill_read_file() -> Skill:
    # handler：根据 path 参数查找文件，不存在时用 difflib 猜测相似文件名；存在则逐行加行号返回
    def handler(args: dict, ctx: SkillContext) -> str:
        name = str(args.get("path", "")).strip()
        content = ctx.workspace.read(name)
        if content is None:
            close = difflib.get_close_matches(name, ctx.workspace.names(), n=1)
            hint = f"，是否指 {close[0]}？" if close else ""
            return f"文件不存在：{name}{hint}。可用 list_files 查看。"
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(content.splitlines(), start=1)]
        return "\n".join(numbered)

    return Skill(
        name="read_file",
        description="读取工作区中某个文件的完整内容（带行号）",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件名"}},
            "required": ["path"],
        },
        handler=handler,
    )


# 工厂函数：创建 grep_code 技能，在工作区代码中按正则表达式搜索匹配行
def _skill_grep_code() -> Skill:
    # handler：编译正则在指定文件或全部文件中逐行匹配，返回 文件:行号: 内容，最多 40 条
    def handler(args: dict, ctx: SkillContext) -> str:
        pattern = str(args.get("pattern", ""))
        target = str(args.get("file", "") or "")
        if not pattern:
            return "缺少参数 pattern（正则表达式）"
        try:
            regex = re.compile(pattern, flags=re.IGNORECASE)
        except re.error as exc:
            return f"非法正则：{exc}"
        files = [target] if target else ctx.workspace.names()
        matches: list[str] = []
        for name in files:
            content = ctx.workspace.read(name)
            if content is None:
                continue
            for lineno, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{name}:{lineno}: {line.strip()[:180]}")
                    if len(matches) >= _MAX_GREP_MATCHES:
                        break
            if len(matches) >= _MAX_GREP_MATCHES:
                break
        return "\n".join(matches) if matches else "无匹配"

    return Skill(
        name="grep_code",
        description="在工作区代码中按正则搜索，返回 文件:行号: 内容",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "file": {"type": "string", "description": "可选，限定文件名"},
            },
            "required": ["pattern"],
        },
        handler=handler,
    )


# 工厂函数：创建 search_kb 技能，通过混合检索器（向量+BM25+重排）查询评审知识库
def _skill_search_kb() -> Skill:
    # handler：将 query 和可选的 kind 传给 HybridRetriever，在线程池中执行检索，格式化返回 chunk id、类型和截断后的内容
    async def handler(args: dict, ctx: SkillContext) -> str:
        if ctx.retriever is None:
            return "知识库不可用（检索器未初始化）"
        query = str(args.get("query", "")).strip()
        if not query:
            return "缺少参数 query"
        kind = args.get("kind") or None
        hits = await asyncio.to_thread(ctx.retriever.retrieve, query, 5, kind)
        if not hits:
            return "知识库中未检索到相关内容"
        blocks: list[str] = []
        for hit in hits:
            text = hit.chunk.display_text()
            if len(text) > _MAX_KB_CHARS // 5:
                text = text[: _MAX_KB_CHARS // 5] + "...（截断）"
            blocks.append(f"[{hit.chunk.id}] ({hit.chunk.kind} | {hit.chunk.source})\n{text}")
        return "\n\n".join(blocks)

    return Skill(
        name="search_kb",
        description="混合检索评审知识库（向量+BM25+重排），引用结果时请使用方括号中的 chunk id",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题，如：SimpleDateFormat 线程安全"},
                "kind": {"type": "string", "description": "可选：review_example | vulnerability | best_practice"},
            },
            "required": ["query"],
        },
        handler=handler,
    )


# 工厂函数：创建 analyze_diff 技能，解析工作区中的 unified diff，提取变更文件和 hunk 位置
def _skill_analyze_diff() -> Skill:
    # handler：扫描 diff 提取文件行（+++/---）和 hunk 行（@@），输出摘要；diff 超过 3000 字符则截断
    def handler(args: dict, ctx: SkillContext) -> str:
        diff = ctx.workspace.diff
        if not diff:
            return "本次提交未包含 diff"
        files: list[str] = []
        hunks: list[str] = []
        for line in diff.splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                files.append(line)
            elif line.startswith("@@"):
                hunks.append(line)
        summary = [f"变更文件行数: {len(files)}", f"hunk 数: {len(hunks)}"]
        detail = "\n".join(files + hunks)
        if len(diff) <= 3000:
            return "\n".join(summary) + "\n\n" + diff
        return "\n".join(summary) + "\n\n" + detail[:3000] + "...（diff 过长已截断）"

    return Skill(
        name="analyze_diff",
        description="解析 unified diff：列出变更文件与 hunk 位置，快速定位评审范围",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


# 工厂函数：创建 read_image 技能，调用多模态模型解读用户上传的架构图或截图
def _skill_read_image() -> Skill:
    # handler：按文件名从 extra["images"] 中查找图片，base64 解码后调用 VisionClient.describe 回答用户问题
    async def handler(args: dict, ctx: SkillContext) -> str:
        if ctx.vision is None or not ctx.vision.enabled:
            return "多模态能力未启用"
        name = str(args.get("image", "")).strip()
        question = str(args.get("question", "请描述这张图，重点说明与代码评审相关的信息：模块、调用关系、数据流。"))
        attachment = next((img for img in ctx.extra.get("images", []) if img.name == name), None)
        if attachment is None:
            available = ", ".join(img.name for img in ctx.extra.get("images", [])) or "无"
            return f"未找到图片 {name}。可用图片：{available}"
        try:
            return await ctx.vision.describe(base64.b64decode(attachment.data_b64), question, attachment.mime)
        except Exception as exc:
            return f"图片解读失败：{exc}"

    return Skill(
        name="read_image",
        description="调用本地 Qwen3-VL 解读用户上传的架构图/截图，回答关于图片的问题",
        parameters={
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "图片文件名"},
                "question": {"type": "string", "description": "针对图片的问题"},
            },
            "required": ["image"],
        },
        handler=handler,
    )


# 构建标准技能注册表：包含 list_files、read_file、grep_code、search_kb、analyze_diff，供常规评审 Agent 使用
def build_standard_registry() -> SkillRegistry:
    registry = SkillRegistry()
    for skill in (_skill_list_files(), _skill_read_file(), _skill_grep_code(), _skill_search_kb(), _skill_analyze_diff()):
        registry.register(skill)
    return registry


# 构建视觉技能注册表：包含 list_files、read_image、search_kb，供 vision Agent 解读图片时使用
def build_vision_registry() -> SkillRegistry:
    registry = SkillRegistry()
    for skill in (_skill_list_files(), _skill_read_image(), _skill_search_kb()):
        registry.register(skill)
    return registry
