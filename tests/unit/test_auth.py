import os

import pytest
from fastapi import HTTPException

from app.auth import require_api_key
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_API_KEY", "test-key")
    # Force re-read in get_settings.
    import app.config

    app.config._settings = None  # type: ignore[attr-defined]
    yield
    app.config._settings = None  # type: ignore[attr-defined]


async def test_rejects_missing_key() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key=None)
    assert exc.value.status_code == 401


async def test_rejects_wrong_key() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key="nope")
    assert exc.value.status_code == 401


async def test_accepts_correct_key() -> None:
    assert get_settings().sandbox_api_key == "test-key"
    await require_api_key(x_api_key="test-key")  # no exception
    _ = os  # silence unused import warning
