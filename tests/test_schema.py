from reviewhive.core.schema import KBChunk, Finding, ReviewReport


def test_chunk_id_stable():
    chunk_a = KBChunk(source="s", title="t", content="c").ensure_id()
    chunk_b = KBChunk(source="s", title="t", content="c").ensure_id()
    assert chunk_a.id == chunk_b.id


def test_chunk_id_differs_on_content():
    chunk_a = KBChunk(source="s", title="t", content="c1").ensure_id()
    chunk_b = KBChunk(source="s", title="t", content="c2").ensure_id()
    assert chunk_a.id != chunk_b.id


def test_severity_counts():
    report = ReviewReport(
        findings=[
            Finding(severity="critical", title="a"),
            Finding(severity="high", title="b"),
            Finding(severity="high", title="c"),
        ]
    )
    counts = report.severity_counts()
    assert counts["critical"] == 1
    assert counts["high"] == 2
    assert counts["medium"] == 0
