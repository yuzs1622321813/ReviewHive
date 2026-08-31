"""reviewhive CLI：serve / ingest / download-data / health。"""
from __future__ import annotations

import asyncio
import json

import typer

from reviewhive.config import load_settings

app = typer.Typer(help="ReviewHive：本地多 Agent 代码评审平台", no_args_is_help=True)


@app.command()
def serve(
    config: str | None = typer.Option(None, help="settings.yaml 路径"),
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """启动 Web 服务与 API。"""
    import uvicorn

    from reviewhive.api.app import create_app

    settings = load_settings(config)
    uvicorn.run(
        create_app(settings),
        host=host or settings.app.host,
        port=port or settings.app.port,
    )


@app.command()
def ingest(config: str | None = typer.Option(None, help="settings.yaml 路径")) -> None:
    """把 data/corpus 与 data/downloads 中的语料写入 Qdrant + Elasticsearch。"""
    from reviewhive.models.embedding import build_embedder
    from reviewhive.rag.ingest import ingest as run_ingest
    from reviewhive.rag.keywordstore import ESStore
    from reviewhive.rag.vectorstore import QdrantStore

    settings = load_settings(config)
    typer.echo("加载嵌入模型（首次可能较慢）…")
    embedder = build_embedder(settings.models.embedding)
    vectorstore = QdrantStore(settings.rag.qdrant_url, settings.rag.collection)
    keywordstore = ESStore(settings.rag.es_url, settings.rag.es_index)
    stats = run_ingest(settings, embedder, vectorstore, keywordstore)
    typer.echo(f"入库完成：{json.dumps(stats, ensure_ascii=False)}")


@app.command("download-data")
def download_data(
    config: str | None = typer.Option(None, help="settings.yaml 路径"),
    limit: int = typer.Option(500, help="每个数据集最多转换的样本数"),
) -> None:
    """下载开源代码评审/缺陷数据集（需要 pip install 'reviewhive[datasets]'）。"""
    from reviewhive.data.datasets import download_all

    settings = load_settings(config)
    stats = download_all(settings.app.data_dir, limit=limit)
    typer.echo(f"下载完成：{json.dumps(stats, ensure_ascii=False)}")


@app.command("fetch-docs")
def fetch_docs(config: str | None = typer.Option(None, help="settings.yaml 路径")) -> None:
    """下载官方文档语料（OWASP 安全指南、阿里 Java 规范 / p3c）到 data/docs/。"""
    from pathlib import Path

    from reviewhive.data.docs_fetch import fetch_all

    settings = load_settings(config)
    stats = fetch_all(Path(settings.app.data_dir) / "docs")
    typer.echo(f"文档下载完成：{json.dumps(stats, ensure_ascii=False)}")
    typer.echo("接下来运行 reviewhive ingest 把它们切片入库。")


@app.command()
def health(config: str | None = typer.Option(None, help="settings.yaml 路径")) -> None:
    """检查主 LLM / VL / Qdrant / Elasticsearch 是否可达。"""
    import httpx

    settings = load_settings(config)
    checks = {
        "llm": settings.models.llm.base_url,
        "vision": settings.models.vision.base_url if settings.models.vision.enabled else None,
        "qdrant": settings.rag.qdrant_url,
        "elasticsearch": settings.rag.es_url,
    }

    async def probe(name: str, base_url: str | None) -> tuple[str, bool]:
        if not base_url:
            return name, False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                if name in ("llm", "vision"):
                    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
                    resp = await client.get(root.rstrip("/") + "/health")
                else:
                    resp = await client.get(base_url)
                return name, resp.status_code < 500
        except httpx.HTTPError:
            return name, False

    results = asyncio.run(asyncio.gather(*(probe(name, url) for name, url in checks.items())))
    for name, ok in results:
        typer.echo(f"{'✓' if ok else '✗'} {name}")


@app.command()
def review(
    path: str = typer.Argument(..., help="项目目录或单个 .py 文件"),
    config: str | None = typer.Option(None, help="settings.yaml 路径"),
    scan_only: bool = typer.Option(False, "--scan-only", help="仅静态扫描，零 LLM"),
    max_files: int | None = typer.Option(None, help="深审文件上限"),
    min_score: int | None = typer.Option(None, help="最低风险分"),
    concurrency: int | None = typer.Option(None, help="并行深审数"),
    output: str | None = typer.Option(None, "--output", "-o", help="JSON 报告路径"),
    fail_on: str | None = typer.Option(None, help="存在该级别发现时 exit 1（如 high）"),
) -> None:
    """对项目目录进行代码评审：AST 静态扫描 + 可选 LLM 深审。"""
    import sys
    from pathlib import Path as PathLib

    from reviewhive.project.report import ProjectReport, format_report
    from reviewhive.project.scanner import scan_project, static_findings
    from reviewhive.project.graph import ImportGraph
    from reviewhive.project.scanner import prioritize

    root = PathLib(path).resolve()
    if not root.exists():
        typer.echo(f"路径不存在: {path}", err=True)
        raise typer.Exit(1)

    settings = load_settings(config)

    if scan_only:
        from reviewhive.project.report import ScannedFile

        cfg = settings.project
        signals = scan_project(root, cfg)
        by_path = {sig.path: sig for sig in signals}
        graph = ImportGraph.build(root, signals)
        review_files, skipped_files = prioritize(signals, cfg.min_score, cfg.max_files)

        scan_entries = []
        all_findings = []
        for sig in signals:
            decision = "review" if sig in review_files else "skip"
            sig_briefs = [s.detail for s in sig.signals[:3]]
            scan_entries.append(ScannedFile(
                path=sig.path, score=sig.score, loc=sig.loc,
                signals=sig_briefs, decision=decision,
            ))
            if cfg.emit_scanner_findings:
                all_findings.extend(static_findings(sig))

        report = ProjectReport(
            session_id="scan-only",
            root=str(root),
            scanned=len(signals),
            reviewed=len(review_files),
            skipped=len(skipped_files),
            scan=scan_entries,
            findings=all_findings,
            summary=f"静态扫描完成：{len(signals)} 文件，{len(all_findings)} 个静态发现。",
            status="done",
        )
    else:
        from reviewhive.core.pipeline import HiveDeps
        from reviewhive.core.store import SessionStore
        from reviewhive.project.reviewer import ProjectReviewer
        from reviewhive.core.schema import StreamEvent

        deps = HiveDeps(settings)
        store = SessionStore(settings.app.db_path)
        reviewer = ProjectReviewer(deps, store)

        async def _run_review():
            async def emit(event: StreamEvent) -> None:
                if event.type == "phase":
                    phase = event.data.get("phase", "")
                    if phase == "scan":
                        typer.echo("Phase 1: 静态扫描中…")
                    elif phase == "scan_done":
                        typer.echo(
                            f"  扫描 {event.data.get('scanned', 0)} 文件，"
                            f"深审 {event.data.get('to_review', 0)}，"
                            f"跳过 {event.data.get('skipped', 0)}"
                        )
                    elif phase == "aggregate":
                        typer.echo("Phase 3: 跨文件聚合…")
                elif event.type == "error":
                    typer.echo(f"  错误: {event.data.get('message', '')}", err=True)

            return await reviewer.run(
                root, emit,
                scan_only=False,
                max_files=max_files,
                min_score=min_score,
                concurrency=concurrency,
            )

        report = asyncio.run(_run_review())

    typer.echo(format_report(report))

    if output:
        PathLib(output).write_text(report.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"\nJSON 报告已写入: {output}")

    if fail_on:
        from reviewhive.core.schema import SEVERITIES
        threshold = SEVERITIES.index(fail_on) if fail_on in SEVERITIES else -1
        if threshold >= 0:
            for finding in report.findings:
                if SEVERITIES.index(finding.severity) <= threshold:
                    raise typer.Exit(1)


if __name__ == "__main__":
    app()
