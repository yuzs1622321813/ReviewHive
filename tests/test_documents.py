from reviewhive.rag.chunking import HeadingChunker
from reviewhive.rag.documents import kind_for, load_documents


def test_load_markdown_with_heading_chunker(tmp_path):
    docs = tmp_path / "best_practice"
    docs.mkdir()
    (docs / "rules.md").write_text(
        "# 命名风格\n【强制】代码命名不能以下划线或美元符号开始或结束。\n"
        "## OOP 规约\n【推荐】类内方法定义的顺序：公有 > 保护 > 私有。",
        encoding="utf-8",
    )

    chunks = load_documents(tmp_path, HeadingChunker(max_chars=1500))
    titles = {chunk.title for chunk in chunks}
    assert titles == {"命名风格", "OOP 规约"}
    for chunk in chunks:
        assert chunk.kind == "best_practice"
        assert chunk.source.startswith("docs:")
        assert chunk.id


def test_long_section_split(tmp_path):
    long_body = "规则内容。" * 600
    docs = tmp_path / "best_practice"
    docs.mkdir()
    (docs / "rules.md").write_text(f"# 总则\n{long_body}", encoding="utf-8")

    chunks = load_documents(tmp_path, HeadingChunker(max_chars=1500))
    body_chunks = [chunk for chunk in chunks if chunk.title == "总则"]
    assert len(body_chunks) >= 2
    for chunk in body_chunks:
        assert len(chunk.content) <= 1500
        assert chunk.id


def test_kind_from_directory(tmp_path):
    vuln_dir = tmp_path / "vulnerability"
    vuln_dir.mkdir()
    (vuln_dir / "sqli.md").write_text("# 注入\n外部输入拼进 SQL 会造成注入，应参数化查询。", encoding="utf-8")

    chunks = load_documents(tmp_path, HeadingChunker())
    assert len(chunks) == 1
    assert chunks[0].kind == "vulnerability"


def test_fixed_window_chunker_fallback(tmp_path):
    from reviewhive.rag.chunking import FixedWindowChunker

    docs = tmp_path / "x"
    docs.mkdir()
    (docs / "plain.md").write_text("没有任何标题的纯文本内容，长度足够成为一块。", encoding="utf-8")

    chunks = load_documents(tmp_path, FixedWindowChunker(max_chars=1500))
    assert len(chunks) == 1
    assert chunks[0].title == ""


def test_kind_for_helper():
    from pathlib import Path

    assert kind_for(Path("a/vulnerability/x.md")) == "vulnerability"
    assert kind_for(Path("a/review_example/x.md")) == "review_example"
    assert kind_for(Path("a/b/x.md")) == "best_practice"
