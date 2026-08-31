from reviewhive.config import ObservabilityConfig
from reviewhive.observability import span


def test_disabled_setup_returns_false():
    from reviewhive import observability

    assert observability.setup(ObservabilityConfig(enabled=False)) is False


def test_span_noop_when_tracer_absent():
    import reviewhive.observability as obs

    original = obs._tracer
    obs._tracer = None
    try:
        with span("test.span", "CHAIN", {"k": "v"}) as active:
            assert active is None
    finally:
        obs._tracer = original


def test_span_accepts_various_attribute_types():
    import reviewhive.observability as obs

    class FakeSpan:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeTracer:
        def start_as_current_span(self, name):
            self.last = FakeSpan()
            return self.last

    fake = FakeTracer()
    original = obs._tracer
    obs._tracer = fake
    try:
        with span("x", "TOOL", {"a": 1, "b": [1, 2], "c": None}) as active:
            pass
        assert active.attrs["a"] == 1
        assert active.attrs["b"] == "[1, 2]"
        assert "c" not in active.attrs
        assert active.attrs["openinference.span.kind"] == "TOOL"
    finally:
        obs._tracer = original
