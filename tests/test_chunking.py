import pytest

from reviewhive.config import ChunkerConfig
from reviewhive.rag.chunking import (
    CHUNKERS,
    FixedWindowChunker,
    HeadingChunker,
    build_chunker,
    chunk_text,
)


class TestFixedWindowChunker:
    def test_short_text_single_chunk(self):
        assert FixedWindowChunker(100).split("hello world") == ["hello world"]

    def test_empty_text(self):
        chunker = FixedWindowChunker(100)
        assert chunker.split("") == []
        assert chunker.split("   ") == []

    def test_long_text_within_limit(self):
        text = "段落。\n" * 800
        chunks = FixedWindowChunker(1200, overlap=120).split(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 1200
            assert chunk.strip()

    def test_overlap_between_neighbors(self):
        text = "".join(str(i % 10) for i in range(3000))
        chunks = FixedWindowChunker(1000, overlap=200).split(text)
        for prev, cur in zip(chunks, chunks[1:]):
            tail = prev[-100:]
            assert any(tail[i : i + 50] in cur for i in range(0, 50))


class TestHeadingChunker:
    _MD = "# 命名风格\n总述。\n## 强制条款\n【强制】命名不能以下划线开始。\n## 推荐条款\n【推荐】多单词用驼峰。"

    def test_sections_by_heading(self):
        sections = HeadingChunker().sections(self._MD)
        assert [sec.title for sec in sections] == ["命名风格", "强制条款", "推荐条款"]
        assert "【强制】" in sections[1].body
        assert "强制条款" not in sections[1].body  # 标题行不进正文

    def test_sections_no_heading(self):
        assert HeadingChunker().sections("纯文本") == HeadingChunker().sections("纯文本")
        sections = HeadingChunker().sections("纯文本")
        assert sections[0].title == "" and sections[0].body == "纯文本"
        assert HeadingChunker().sections("") == []

    def test_split_keeps_section_header(self):
        pieces = HeadingChunker().split(self._MD)
        assert pieces[1].startswith("## 强制条款")

    def test_long_section_secondary_split(self):
        text = "# 大节\n" + "内容。\n" * 1500
        pieces = HeadingChunker(1500, 150).split(text)
        assert len(pieces) > 1
        assert all(piece.startswith("## 大节") for piece in pieces)
        assert all(len(piece) <= 1600 for piece in pieces)


class TestRegistry:
    def test_build_by_config(self):
        assert isinstance(build_chunker(ChunkerConfig(strategy="heading")), HeadingChunker)
        assert isinstance(build_chunker(ChunkerConfig(strategy="fixed_window")), FixedWindowChunker)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            build_chunker(ChunkerConfig(strategy="not_exist"))

    def test_registry_complete(self):
        assert set(CHUNKERS) == {"heading", "fixed_window"}

    def test_chunk_text_helper(self):
        assert chunk_text("短文本", 100) == ["短文本"]
