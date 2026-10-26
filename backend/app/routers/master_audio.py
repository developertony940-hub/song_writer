"""Nondestructive local master editor routes; mount router explicitly in main."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.services.master_audio import MAX_BYTES, MasterAudioError, get_master_audio_service

router = APIRouter(prefix="/api/projects/{pid}/master-audio", tags=["master-audio"])


def _call(method, *args):
    try:
        return getattr(get_master_audio_service(), method)(*args)
    except MasterAudioError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc


async def _body(request: Request, maximum):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise HTTPException(413, {"code": "size_limit", "message": "request body too large"})
        data.extend(chunk)
    return bytes(data)


async def _json(request):
    try:
        return json.loads(await _body(request, 256 * 1024))
    except (ValueError, UnicodeError):
        raise HTTPException(422, {"code": "validation", "message": "valid JSON object required"})


@router.get("")
def status(pid: str):
    return _call("status", pid)


@router.post("/sources")
async def register_source(pid: str, request: Request):
    return await run_in_threadpool(_call, "register_source", pid, await _json(request))


@router.post("/sources/upload")
async def upload_source(pid: str, request: Request, filename: str):
    return await run_in_threadpool(_call, "upload_source", pid, filename, await _body(request, MAX_BYTES))


@router.get("/sources/{source_id}/peaks")
def peaks(pid: str, source_id: str, bins: int = 1000):
    return _call("peaks", pid, source_id, bins)


@router.post("/revisions")
async def create_revision(pid: str, request: Request):
    return await run_in_threadpool(_call, "create_revision", pid, await _json(request))


@router.post("/renders")
async def render(pid: str, request: Request):
    return await run_in_threadpool(_call, "render", pid, await _json(request))


@router.get("/files/{relative:path}")
def file(pid: str, relative: str):
    path = _call("file", pid, relative)
    return FileResponse(path, media_type={".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
                        ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac"}.get(path.suffix.lower(), "application/octet-stream"))
