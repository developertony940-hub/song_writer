"""Replacement-section export API (offline prepare/read only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.replacement import ReplacementExportRequest
from app.services.replacement_export import (
    ReplacementExportError,
    get_replacement_export_service,
)

router = APIRouter(prefix="/api/music", tags=["replacement-export"])


def _raise(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc).strip("'")) from exc
    raise HTTPException(409, str(exc)) from exc


@router.get("/{pid}/replacement-exports/capability")
def replacement_export_capability(pid: str):
    return get_replacement_export_service().capability().model_dump(mode="json")


@router.get("/{pid}/replacement-exports")
def list_replacement_exports(pid: str):
    try:
        return get_replacement_export_service().list_exports(pid)
    except (KeyError, ReplacementExportError) as exc:
        _raise(exc)


@router.post("/{pid}/replacement-exports")
def prepare_replacement_export(pid: str, req: ReplacementExportRequest):
    try:
        return get_replacement_export_service().prepare(pid, req)
    except (KeyError, ReplacementExportError) as exc:
        _raise(exc)


@router.get("/{pid}/replacement-exports/{export_id}")
def get_replacement_export(pid: str, export_id: str):
    try:
        return get_replacement_export_service().get(pid, export_id)
    except (KeyError, ReplacementExportError) as exc:
        _raise(exc)
