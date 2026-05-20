"""Files API — upload, list, download and delete workspace files.

Workspace files live on the host at session.container.workspace_path
and are bind-mounted read-write into the container at /workspace.
All paths are validated to prevent traversal outside the workspace root.
"""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import require_api_key
from app.models import FileDeleteResponse, FileEntry, FileListResponse, FileUploadResponse
from app.runtime.manager import SessionManager

router = APIRouter(
    prefix="/v1/sessions",
    tags=["files"],
    dependencies=[Depends(require_api_key)],
)


def _manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _safe_path(workspace: Path, rel: str) -> Path:
    """Resolve rel against workspace; raise 400 on path traversal."""
    if ".." in Path(rel).parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path traversal not allowed")
    resolved = (workspace / rel).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path traversal not allowed")
    return resolved


@router.post("/{session_id}/files", response_model=FileUploadResponse, status_code=201)
async def upload_file(
    session_id: str,
    request: Request,
    file: UploadFile,
    dest: str | None = Form(default=None),
) -> FileUploadResponse:
    mgr = _manager(request)
    session = mgr.require(session_id)
    settings = request.app.state.settings

    max_bytes = settings.file_upload_max_mb * 1024 * 1024
    rel_path = dest if dest is not None else (file.filename or "upload")
    target = _safe_path(session.container.workspace_path, rel_path)

    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"file exceeds {settings.file_upload_max_mb} MiB limit",
        )

    def _write() -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.stat().st_size

    size_bytes = await asyncio.to_thread(_write)
    return FileUploadResponse(
        path=str(target.relative_to(session.container.workspace_path)),
        size_bytes=size_bytes,
    )


@router.get("/{session_id}/files", response_model=FileListResponse)
async def list_files(
    session_id: str,
    request: Request,
    dir: str = "",
) -> FileListResponse:
    mgr = _manager(request)
    session = mgr.require(session_id)
    workspace = session.container.workspace_path
    list_root = _safe_path(workspace, dir) if dir else workspace

    def _list() -> list[FileEntry]:
        if not list_root.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"directory {dir!r} not found")
        if not list_root.is_dir():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{dir!r} is not a directory")
        entries = []
        for child in sorted(list_root.iterdir()):
            rel = str(child.relative_to(workspace))
            size = child.stat().st_size if child.is_file() else 0
            entries.append(
                FileEntry(name=child.name, path=rel, size_bytes=size, is_dir=child.is_dir())
            )
        return entries

    files = await asyncio.to_thread(_list)
    return FileListResponse(files=files)


@router.get("/{session_id}/files/{file_path:path}")
async def download_file(
    session_id: str,
    file_path: str,
    request: Request,
) -> FileResponse:
    mgr = _manager(request)
    session = mgr.require(session_id)
    target = _safe_path(session.container.workspace_path, file_path)

    def _check() -> None:
        if not target.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"file {file_path!r} not found")
        if not target.is_file():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{file_path!r} is a directory")

    await asyncio.to_thread(_check)
    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
    )


@router.delete("/{session_id}/files/{file_path:path}", response_model=FileDeleteResponse)
async def delete_file(
    session_id: str,
    file_path: str,
    request: Request,
) -> FileDeleteResponse:
    mgr = _manager(request)
    session = mgr.require(session_id)
    target = _safe_path(session.container.workspace_path, file_path)

    def _delete() -> None:
        if not target.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"file {file_path!r} not found")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    await asyncio.to_thread(_delete)
    return FileDeleteResponse(deleted=True)
