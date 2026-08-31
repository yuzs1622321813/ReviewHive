from reviewhive.skills.builtin import build_standard_registry, build_vision_registry
from reviewhive.skills.context import ReviewWorkspace, SkillContext
from reviewhive.skills.registry import Skill, SkillRegistry


def _make_registry() -> SkillRegistry:
    registry = SkillRegistry()

    def echo(args: dict, ctx: SkillContext) -> str:
        return f"echo:{args.get('text', '')}"

    registry.register(
        Skill(
            name="echo",
            description="回显参数",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=echo,
        )
    )
    return registry


async def test_execute_sync_handler():
    registry = _make_registry()
    ctx = SkillContext(workspace=ReviewWorkspace())
    assert await registry.execute("echo", {"text": "hi"}, ctx) == "echo:hi"


async def test_unknown_skill_message():
    registry = _make_registry()
    ctx = SkillContext(workspace=ReviewWorkspace())
    result = await registry.execute("not_exist", {}, ctx)
    assert "未知技能" in result


async def test_handler_exception_returned_as_text():
    registry = _make_registry()

    def boom(args: dict, ctx: SkillContext) -> str:
        raise RuntimeError("boom")

    registry.register(Skill(name="boom", description="x", parameters={"type": "object", "properties": {}}, handler=boom))
    ctx = SkillContext(workspace=ReviewWorkspace())
    result = await registry.execute("boom", {}, ctx)
    assert "执行失败" in result


def test_render_prompt_lists_skills():
    registry = _make_registry()
    prompt = registry.render_prompt()
    assert "echo" in prompt and "text" in prompt


def test_builtin_registries():
    standard = build_standard_registry()
    assert set(standard.names()) == {"list_files", "read_file", "grep_code", "search_kb", "analyze_diff"}
    vision = build_vision_registry()
    assert "read_image" in vision.names()
    assert "read_image" not in standard.names()


async def test_grep_code_skill():
    from reviewhive.skills.builtin import _skill_grep_code

    workspace = ReviewWorkspace()
    workspace.add_file("A.java", "int a = 1;\n// TODO fix\n")
    ctx = SkillContext(workspace=workspace)
    skill = _skill_grep_code()
    result = skill.handler({"pattern": "TODO"}, ctx)
    assert "A.java:2" in result
