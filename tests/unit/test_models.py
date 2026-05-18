import pytest
from pydantic import ValidationError

from app.models import CreateSessionRequest, ExecRequest


def test_create_session_requires_conversation_id() -> None:
    with pytest.raises(ValidationError):
        CreateSessionRequest()  # type: ignore[call-arg]


def test_create_session_minimum_payload() -> None:
    req = CreateSessionRequest(conversation_id="conv-1")
    assert req.conversation_id == "conv-1"
    assert req.agent_id is None
    assert req.env is None


def test_exec_request_timeout_bounds() -> None:
    ExecRequest(code="print(1)", timeout_s=600)
    with pytest.raises(ValidationError):
        ExecRequest(code="x", timeout_s=0)
    with pytest.raises(ValidationError):
        ExecRequest(code="x", timeout_s=601)


def test_exec_request_accepts_empty_code() -> None:
    req = ExecRequest(code="")
    assert req.code == ""
    assert req.timeout_s is None
