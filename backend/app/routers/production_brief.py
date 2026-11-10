"""Project-scoped production-brief API.

The routes are additive and operate only on ``production-brief`` records. They
do not expose generation, audio processing, legacy music approval, or tool
permissions.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.production_brief import (
    ProductionBriefContextRequest,
    ProductionBriefDraftRequest,
    ProductionBriefReviewRequest,
)
from app.services.master_audio import MasterAudioError
from app.services.production_brief_service import (
    ProductionBriefError,
    get_production_brief_service,
)

router = APIRouter(
    prefix="/api/projects/{pid}/production-brief",
    tags=["production-brief"],
)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ProductionBriefError):
        return HTTPException(
            exc.status, {"code": exc.code, "message": str(exc)})
    if isinstance(exc, MasterAudioError):
        return HTTPException(
            exc.status, {"code": exc.code, "message": str(exc)})
    return HTTPException(500, {"code": "internal", "message": str(exc)})


@router.get("")
def status(pid: str):
    try:
        return get_production_brief_service().status(pid)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.get("/revisions")
def list_revisions(pid: str):
    try:
        return get_production_brief_service().status(pid)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.get("/revisions/compare")
def compare_revisions(pid: str, from_version: int, to_version: int):
    try:
        return get_production_brief_service().compare(pid, from_version, to_version)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.get("/revisions/{revision}")
def get_revision(pid: str, revision: int):
    try:
        return get_production_brief_service().get_revision(pid, revision)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.post("/drafts")
def save_draft(pid: str, request: ProductionBriefDraftRequest):
    try:
        return get_production_brief_service().save_draft(pid, request)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.post("/revisions/{revision}/review")
def review_revision(pid: str, revision: int, request: ProductionBriefReviewRequest):
    try:
        return get_production_brief_service().review(pid, revision, request)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)


@router.post("/revisions/{revision}/context")
def assemble_context(pid: str, revision: int, request: ProductionBriefContextRequest):
    try:
        return get_production_brief_service().context(pid, revision, request)
    except (KeyError, ProductionBriefError) as exc:
        raise _error(exc)
