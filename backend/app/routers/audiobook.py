"""Audiobook slice API (Phase 1).

Exposes the manual speaker-span editing surface, coverage/status, chunk
planning, rendering, assembly, byte-bound approval, and recovery clearing.
Rendering uses the configured TTS engine; it is not exercised by any automated
test (fake synthesis only) and real generation is not authorized in Phase 1.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.audiobook import (
    AssembleRequest,
    ClearRecoveryRequest,
    ImportChapterRequest,
    RenderRequest,
    ReviewRequest,
    SetAnnotationsRequest,
    SetSpeakersRequest,
)
from app.services.audiobook import (
    AudiobookError,
    GenerationNotAuthorized,
    RecoveryBlocked,
    get_audiobook,
)

router = APIRouter(prefix="/api/audiobook", tags=["audiobook"])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    if isinstance(exc, GenerationNotAuthorized):
        return HTTPException(403, str(exc))
    if isinstance(exc, RecoveryBlocked):
        return HTTPException(423, str(exc))
    return HTTPException(409, str(exc))


@router.get("/{pid}/status")
def status(pid: str):
    try:
        return get_audiobook().status(pid)
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.get("/{pid}/chapter")
def chapter(pid: str):
    try:
        return get_audiobook().chapter(pid)
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/chapter")
def import_chapter(pid: str, req: ImportChapterRequest):
    try:
        return get_audiobook().import_chapter(pid, req).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.put("/{pid}/speakers")
def set_speakers(pid: str, req: SetSpeakersRequest):
    try:
        return get_audiobook().set_speakers(pid, req.speakers).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.put("/{pid}/annotations")
def set_annotations(pid: str, req: SetAnnotationsRequest):
    try:
        return get_audiobook().set_annotations(pid, req.spans, req.exclusions).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/chunks/plan")
def plan_chunks(pid: str):
    try:
        return get_audiobook().plan_chunks(pid).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/render")
async def render(pid: str, req: RenderRequest):
    try:
        return await get_audiobook().render(pid, force=req.force)
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/assemble")
def assemble(pid: str, req: AssembleRequest):
    try:
        return get_audiobook().assemble(pid, pause_ms=req.pause_ms).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/review")
def review(pid: str, req: ReviewRequest):
    try:
        return get_audiobook().review(
            pid, listened_wav=req.listened_wav, listened_mp3=req.listened_mp3, note=req.note,
        ).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/approve")
def approve(pid: str):
    try:
        return get_audiobook().approve(pid)
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.get("/{pid}/approval")
def approval(pid: str):
    try:
        return get_audiobook().verify_approval(pid)
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)


@router.post("/{pid}/recovery/clear")
def clear_recovery(pid: str, req: ClearRecoveryRequest):
    try:
        return get_audiobook().clear_recovery(
            pid, worker_stopped=req.worker_stopped, queue_drained=req.queue_drained,
            evidence=req.evidence, note=req.note,
        ).model_dump(mode="json")
    except (KeyError, AudiobookError) as exc:
        raise _error(exc)
