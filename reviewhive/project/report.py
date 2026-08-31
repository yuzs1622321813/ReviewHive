"""项目级评审报告模型与终端渲染。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from reviewhive.core.schema import Finding, SEVERITIES


class ScannedFile(BaseModel):
    path: str
    score: int = 0
    loc: int = 0
    signals: list[str] = Field(default_factory=list)
    decision: str = "skip"  # "review" | "skip"


class ProjectReport(BaseModel):
    session_id: str = ""
    root: str = ""
    scanned: int = 0
    reviewed: int = 0
    skipped: int = 0
    scan: list[ScannedFile] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    duration_ms: int = 0
    status: str = "running"  # running | done | failed

    def severity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {level: 0 for level in SEVERITIES}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts


_SEVERITY_ICONS = {"critical": "!!", "high": "! ", "medium": "~ ", "low": "  ", "info": "  "}


def format_report(report: ProjectReport) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"ReviewHive 项目评审报告  —  {report.root}")
    lines.append("=" * 60)
    lines.append(f"扫描: {report.scanned} 文件 | 深审: {report.reviewed} | 跳过: {report.skipped} | 耗时: {report.duration_ms / 1000:.1f}s")
    lines.append("")

    counts = report.severity_counts()
    parts = [f"{level}: {counts[level]}" for level in SEVERITIES if counts[level] > 0]
    if parts:
        lines.append(f"发现: {' | '.join(parts)}")
    else:
        lines.append("发现: 无")
    lines.append("")

    if report.findings:
        lines.append("-" * 60)
        lines.append("发现详情")
        lines.append("-" * 60)
        for i, finding in enumerate(report.findings, 1):
            icon = _SEVERITY_ICONS.get(finding.severity, "  ")
            file_loc = finding.file
            if finding.lines:
                file_loc += f":{finding.lines}"
            lines.append(f"  [{icon}] {finding.severity.upper():8s}  {finding.title}")
            lines.append(f"           {file_loc}  ({finding.category})")
            if finding.description and finding.description != finding.title:
                desc = finding.description[:200]
                lines.append(f"           {desc}")
            if finding.suggestion:
                lines.append(f"           建议: {finding.suggestion[:150]}")
            lines.append("")

    if report.summary:
        lines.append("-" * 60)
        lines.append("总结")
        lines.append("-" * 60)
        lines.append(report.summary)
        lines.append("")

    if report.scan:
        lines.append("-" * 60)
        lines.append("扫描明细")
        lines.append("-" * 60)
        for sf in report.scan:
            marker = ">>" if sf.decision == "review" else "  "
            sig_brief = ", ".join(sf.signals[:3]) if sf.signals else "-"
            lines.append(f"  {marker} {sf.path:40s}  score={sf.score:<3d}  loc={sf.loc:<5d}  [{sig_brief}]")

    return "\n".join(lines)
