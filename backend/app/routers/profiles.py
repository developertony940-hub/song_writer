"""Artist profile API: capture, versions, preview, confirmed load."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.profiles import (
    ArtistProfileCreateRequest,
    ArtistProfileLoadRequest,
    ArtistProfilePreviewRequest,
    ArtistProfileUpdateRequest,
)
from app.services.profile_service import ProfileError, get_profile_service

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _raise(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc).strip("'")) from exc
    raise HTTPException(409, str(exc)) from exc


def _service():
    return get_profile_service()


@router.get("")
def list_artist_profiles():
    return _service().list_profiles()


@router.post("")
def create_artist_profile(req: ArtistProfileCreateRequest):
    try:
        return _service().create_from_take(req)
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.get("/{profile_id}")
def get_artist_profile(profile_id: str, version: int | None = None):
    try:
        return _service().get_profile(profile_id, version)
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.get("/{profile_id}/versions")
def list_artist_profile_versions(profile_id: str):
    try:
        profile = _service().get_profile(profile_id)
        return {
            "profile": profile["profile"],
            "versions": profile["versions"],
            "latest_version": profile["profile"]["latest_version"],
        }
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.get("/{profile_id}/versions/{version}")
def get_artist_profile_version(profile_id: str, version: int):
    try:
        return _service().get_profile(profile_id, version)
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.put("/{profile_id}")
def update_artist_profile(profile_id: str, req: ArtistProfileUpdateRequest):
    try:
        return _service().update_profile(profile_id, req)
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.post("/{profile_id}/versions/{version}/preview")
def preview_artist_profile_load(
    profile_id: str, version: int, req: ArtistProfilePreviewRequest,
):
    try:
        return _service().preview(profile_id, version, req)
    except (KeyError, ProfileError) as exc:
        _raise(exc)


@router.post("/{profile_id}/versions/{version}/load")
def load_artist_profile(profile_id: str, version: int, req: ArtistProfileLoadRequest):
    try:
        return _service().load(profile_id, version, req)
    except (KeyError, ProfileError) as exc:
        _raise(exc)
