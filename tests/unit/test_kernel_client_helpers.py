from app.runtime.kernel_client import _truncate


def test_truncate_passthrough_when_under_cap() -> None:
    assert _truncate("hello", 100) == "hello"


def test_truncate_clips_and_marks() -> None:
    s = "x" * 200
    out = _truncate(s, 50)
    assert out.startswith("x" * 50)
    assert "truncated" in out
    assert "200" in out
