"""GET /v1/env — return pre-installed packages for a sandbox template image.

Results are cached in memory keyed by image name; the cache is cleared only
on process restart, which is acceptable because image contents only change
when a new image is built (which requires a restart to pick up the new tag).
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request

from app.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["env"])

_package_cache: dict[str, list[dict]] = {}
_cache_lock = asyncio.Lock()


def _pip_list_sync(image: str, client) -> list[dict]:
    raw = client.containers.run(
        image,
        command=["pip", "list", "--format=json"],
        remove=True,
        stdout=True,
        stderr=False,
        user="root",
    )
    return json.loads(raw.decode())


@router.get("/env")
async def get_env(
    request: Request,
    template: str = "default",
    _: None = Depends(require_api_key),
) -> dict:
    """Return the list of pip packages installed in the given template's image.

    Query params:
      template — sandbox template name (default: "default")

    Response:
      {"template": "qa", "image": "agent-sandbox:qa", "packages": [...]}
    """
    manager = request.app.state.manager
    runtime = request.app.state.runtime
    image = manager.resolve_image(template)

    async with _cache_lock:
        if image not in _package_cache:
            logger.info("env cache miss for image=%s — running pip list", image)
            client = runtime.client()
            packages = await asyncio.to_thread(_pip_list_sync, image, client)
            _package_cache[image] = packages
            logger.info("env cache populated: %d packages for image=%s", len(packages), image)
        packages = _package_cache[image]

    return {"template": template, "image": image, "packages": packages}
