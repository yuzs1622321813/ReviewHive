"""文档语料加载：把 data/docs 下的 md/pdf 转为 KBChunk，切片委托给可插拔的 Chunker。

目录约定：data/docs/<kind>/...（kind ∈ vulnerability | review_example，
其余目录一律视为 best_practice）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from reviewhive.core.schema import KBChunk
from reviewhive.rag.chunking import Chunker, HeadingChunker

logger = logging.getLogger(__name__)

_KIND_DIRS = {"vulnerability", "review_example"}
_MIN_SECTION_CHARS = 20
_MAX_TITLE_CHARS = 120


def kind_for(path: Path) -> str:
    for part in path.parts[:-1]:
        if part in _KIND_DIRS:
            return part
    return "best_practice"


def load_documents(docs_dir: Path, chunker: Chunker) -> list[KBChunk]:
    """把 docs 目录下的 md/pdf 转为 KBChunk 列表（已完成切分）。"""
    chunks: list[KBChunk] = []
    if not docs_dir.exists():
        return chunks

    for path in sorted(docs_dir.rglob("*.md")):
        source = str(path.relative_to(docs_dir))
        kind = kind_for(path)
        text = path.read_text(encoding="utf-8", errors="ignore")
        if isinstance(chunker, HeadingChunker):
            for section in chunker.sections(text):
                if len(section.body) < _MIN_SECTION_CHARS:
                    continue
                if len(section.body) <= chunker.max_chars:
                    pieces = [section.body]
                else:
                    pieces = chunker.window.split(section.body)
                for piece in pieces:
                    chunks.append(_make_chunk(source, kind, section.title, piece))
        else:
            for piece in chunker.split(text):
                chunks.append(_make_chunk(source, kind, "", piece))

    for path in sorted(docs_dir.rglob("*.pdf")):
        text = _read_pdf(path)
        if not text:
            continue
        source = str(path.relative_to(docs_dir))
        kind = kind_for(path)
        for piece in chunker.split(text):
            chunks.append(_make_chunk(source, kind, path.stem, piece))

    logger.info("文档语料加载完成：%d 块（来自 %s）", len(chunks), docs_dir)
    return [chunk.ensure_id() for chunk in chunks]


def _make_chunk(source: str, kind: str, title: str, content: str) -> KBChunk:
    return KBChunk(
        source=f"docs:{source}",
        kind=kind,
        title=title[:_MAX_TITLE_CHARS],
        content=content.strip(),
        language="",
    )


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("跳过 PDF（未安装 pypdf）：%s", path)
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        logger.warning("PDF 解析失败 %s: %s", path, exc)
        return ""
