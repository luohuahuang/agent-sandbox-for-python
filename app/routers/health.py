from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    runtime = request.app.state.runtime
    docker_ok = await runtime.healthcheck()
    return {"status": "ok" if docker_ok else "degraded", "docker": docker_ok}
