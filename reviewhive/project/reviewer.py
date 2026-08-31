"""项目级评审编排：静态扫描 → 并发深审 → 合并去重。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from pathlib import Path

from reviewhive.config import ProjectReviewConfig, Settings
from reviewhive.core.pipeline import HiveDeps, ReviewPipeline
from reviewhive.core.schema import (
    SEVERITIES,
    AgentResult,
    Finding,
    ReviewInput,
    ReviewReport,
    StreamEvent,
)
from reviewhive.core.store import SessionStore
from reviewhive.project.graph import ImportGraph
from reviewhive.project.interfaces import render_context
from reviewhive.project.report import ProjectReport, ScannedFile
from reviewhive.project.scanner import (
    FileSignals,
    prioritize,
    scan_project,
    score_file,
    static_findings,
)
from reviewhive.skills.context import ReviewWorkspace

logger = logging.getLogger(__name__)


class ProjectFilePipeline(ReviewPipeline):
    """覆写 _intake：在主文件后追加项目上下文文件。"""

    def __init__(self, deps: HiveDeps, store: SessionStore, extra_files: dict[str, str] | None = None):
        super().__init__(deps, store)
        self.extra_files = extra_files or {}

    def _intake(self, review_input: ReviewInput, report: ReviewReport) -> ReviewWorkspace:
        workspace = super()._intake(review_input, report)
        for name, content in self.extra_files.items():
            workspace.add_file(name, content)
        return workspace


def merge_findings(all_findings: list[Finding]) -> list[Finding]:
    """确定性去重：精确指纹 + 同文件相似度合并。"""
    seen: dict[str, Finding] = {}
    for finding in all_findings:
        key = hashlib.sha1(
            f"{finding.file}|{finding.category}|{finding.title}|{finding.lines}".encode()
        ).hexdigest()[:12]
        if key not in seen:
            seen[key] = finding
    deduped = list(seen.values())

    merged: list[Finding] = []
    used: set[int] = set()
    for i, fi in enumerate(deduped):
        if i in used:
            continue
        for j, fj in enumerate(deduped[i + 1 :], start=i + 1):
            if j in used:
                continue
            if fi.file != fj.file or fi.category != fj.category:
                continue
            if _lines_overlap(fi.lines, fj.lines) or _jaccard(fi.title, fj.title) >= 0.6:
                if SEVERITIES.index(fi.severity) > SEVERITIES.index(fj.severity):
                    deduped[i] = fj
                    fi = fj
                used.add(j)
        merged.append(fi)

    merged.sort(key=lambda f: (SEVERITIES.index(f.severity), -f.confidence))
    return merged


def _lines_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        a_lines = _parse_line_range(a)
        b_lines = _parse_line_range(b)
        return a_lines[0] <= b_lines[1] and b_lines[0] <= a_lines[1]
    except Exception:
        return False


def _parse_line_range(s: str) -> tuple[int, int]:
    parts = s.replace("-", ",").split(",")
    nums = [int(p) for p in parts if p.strip().isdigit()]
    if not nums:
        return (0, 0)
    return (nums[0], nums[-1])


def _jaccard(a: str, b: str) -> float:
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


class ProjectReviewer:
    def __init__(self, deps: HiveDeps, store: SessionStore):
        self.deps = deps
        self.store = store

    async def run(
        self,
        root: Path,
        emit,
        *,
        scan_only: bool = False,
        max_files: int | None = None,
        min_score: int | None = None,
        concurrency: int | None = None,
    ) -> ProjectReport:
        cfg = self.deps.settings.project
        max_files = max_files or cfg.max_files
        min_score = min_score if min_score is not None else cfg.min_score
        concurrency = concurrency or cfg.concurrency

        session_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        report = ProjectReport(session_id=session_id, root=str(root))

        await emit(StreamEvent(type="phase", data={"phase": "scan"}))

        signals = scan_project(root, cfg)
        by_path = {sig.path: sig for sig in signals}
        graph = ImportGraph.build(root, signals)

        review_files, skipped_files = prioritize(signals, min_score, max_files)

        scan_entries: list[ScannedFile] = []
        for sig in signals:
            decision = "review" if sig in review_files else "skip"
            sig_briefs = [s.detail for s in sig.signals[:3]]
            scan_entries.append(ScannedFile(
                path=sig.path, score=sig.score, loc=sig.loc,
                signals=sig_briefs, decision=decision,
            ))
        report.scan = scan_entries
        report.scanned = len(signals)
        report.reviewed = len(review_files)
        report.skipped = len(skipped_files)

        await emit(StreamEvent(type="phase", data={
            "phase": "scan_done",
            "scanned": len(signals),
            "to_review": len(review_files),
            "skipped": len(skipped_files),
        }))

        all_findings: list[Finding] = []
        if cfg.emit_scanner_findings:
            for sig in signals:
                all_findings.extend(static_findings(sig))

        if scan_only:
            report.findings = all_findings
            report.status = "done"
            report.summary = f"静态扫描完成：{len(signals)} 文件，{len(all_findings)} 个静态发现。"
            report.duration_ms = int((time.monotonic() - started) * 1000)
            return report

        if not review_files:
            report.findings = all_findings
            report.status = "done"
            report.summary = f"扫描 {len(signals)} 文件，无文件达到深审阈值（min_score={min_score}）。"
            report.duration_ms = int((time.monotonic() - started) * 1000)
            return report

        sem = asyncio.Semaphore(concurrency)

        async def _review_one(sig: FileSignals) -> list[Finding]:
            async with sem:
                ctx_md = render_context(sig, graph, by_path, cfg.context_limit)
                extra_files = {"_project_context.md": ctx_md}
                pipeline = ProjectFilePipeline(self.deps, self.store, extra_files)

                review_input = ReviewInput(
                    code=sig.source,
                    filename=sig.path,
                    language="python",
                )
                file_session_id = f"{session_id}-{sig.path.replace('/', '_')}"[:24]

                async def file_emit(event: StreamEvent) -> None:
                    await emit(StreamEvent(type="file_review", data={
                        "file": sig.path, "event": event.type, **event.data,
                    }))

                file_report = await pipeline.run(review_input, file_emit, session_id=file_session_id)
                for finding in file_report.findings:
                    if not finding.file:
                        finding.file = sig.path
                return file_report.findings

        tasks = [_review_one(sig) for sig in review_files]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        for sig, outcome in zip(review_files, outcomes):
            if isinstance(outcome, Exception):
                logger.warning("深审失败 %s: %s", sig.path, outcome)
                await emit(StreamEvent(type="error", data={"file": sig.path, "message": str(outcome)}))
            else:
                all_findings.extend(outcome)

        merged = merge_findings(all_findings)

        await emit(StreamEvent(type="phase", data={"phase": "aggregate"}))
        try:
            if merged:
                agent_result = AgentResult(agent="project", findings=merged)
                final_findings, summary = await self.deps.orchestrator.aggregate([agent_result])
            else:
                final_findings, summary = [], "未发现需要报告的问题。"
        except Exception as exc:
            logger.warning("跨文件聚合失败: %s", exc)
            final_findings = merged
            summary = f"聚合失败，保留原始发现 {len(merged)} 条。"

        report.findings = sorted(final_findings, key=lambda f: (SEVERITIES.index(f.severity), -f.confidence))
        report.summary = summary
        report.status = "done"
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report
