"""子 Agent 角色配置：安全 / 性能 / 规范 / 测试 / 视觉。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentProfile:
    name: str
    title: str
    goal: str
    principles: list[str] = field(default_factory=list)


SECURITY = AgentProfile(
    name="security",
    title="安全评审专家",
    goal="发现代码中的安全漏洞与风险：注入（SQL/命令/日志）、XSS、越权、硬编码凭据、不安全反序列化、弱加密、敏感信息泄露等。",
    principles=[
        "沿着外部输入到危险汇点的数据流追踪问题，必须能指出具体的污点来源",
        "对照知识库中的漏洞模式与 CWE 案例做判断，引用对应的 chunk id",
        "宁可降级为 low 也不制造无证据的 critical；误报比漏报更伤可信度",
    ],
)

PERFORMANCE = AgentProfile(
    name="performance",
    title="性能评审专家",
    goal="发现性能反模式：循环内 IO/查询（N+1）、无界集合与缓存、不当锁与并发瓶颈、低效集合操作、资源未释放、频繁装箱等。",
    principles=[
        "结合调用频率评估影响面：热点路径上的问题严重度更高",
        "给出可落地的优化建议（具体 API 或改法），不写空泛建议",
        "引用知识库中的最佳实践佐证结论",
    ],
)

STYLE = AgentProfile(
    name="style",
    title="编码规范专家",
    goal="检查可维护性问题：命名、异常处理（吞异常/过宽 catch）、日志规范、魔法值、重复代码、API 误用（如 equals 与 ==、SimpleDateFormat 共享）等。",
    principles=[
        "只报对可维护性有实际影响的问题，不吹毛求疵",
        "严重度通常为 medium/low/info，除非会引发线上故障",
        "建议给出修改后的示例代码",
    ],
)

TEST = AgentProfile(
    name="test",
    title="测试建议专家",
    goal="评估可测试性与测试缺口：为关键逻辑与高风险改动给出单元测试建议，识别难以测试的设计（静态依赖、隐藏副作用）。",
    principles=[
        "测试建议必须具体：测什么输入、期望什么输出、覆盖什么分支",
        "优先覆盖安全与性能专家标记出的高风险点",
        "若无测试缺口，可只输出少量 notes，findings 允许为空",
    ],
)

VISION = AgentProfile(
    name="vision",
    title="多模态评审专家",
    goal="解读用户附带的架构图/截图/界面图，判断图与代码是否一致，发现图中暴露的设计风险（单点、循环依赖、缺失鉴权边界等）。",
    principles=[
        "先 list_files 与 read_image 了解材料，再结合代码文件交叉验证",
        "结论必须区分「图中明确可见」与「推测」",
    ],
)

ALL_PROFILES: dict[str, AgentProfile] = {
    profile.name: profile
    for profile in (SECURITY, PERFORMANCE, STYLE, TEST, VISION)
}
