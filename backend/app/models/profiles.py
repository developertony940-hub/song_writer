"""Versioned artist production profiles, stored separately from projects."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.music import MusicSettings

PROFILE_LABEL = "production preset; singer identity not verified"


class ArtistProfileSource(BaseModel):
    project_id: str
    asset_id: str
    attempt_id: str
    revision: int | None = None
    audio_sha256: str
    take_created_at: str | None = None
    engine: str | None = None
    checkpoint: str | None = None
    checkpoint_sha256: str | None = None
    workflow: str | None = None
    settings_source: str | None = None


class ArtistProfileVersion(BaseModel):
    profile_id: str
    version: int
    name: str
    created_at: datetime
    source: ArtistProfileSource
    recorded_settings: MusicSettings | None = None
    provenance_gaps: list[str] = Field(default_factory=list)
    artist_description: str = ""
    vocal_description: str = ""
    production_defaults: dict[str, Any] = Field(default_factory=dict)
    label: str = PROFILE_LABEL


class ArtistProfileMeta(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    latest_version: int
    version_count: int


class ArtistProfileCreateRequest(BaseModel):
    project_id: str
    asset_id: str
    name: str = Field(..., min_length=1, max_length=80)
    artist_description: str = Field("", max_length=2000)
    vocal_description: str = Field("", max_length=2000)
    production_defaults: dict[str, Any] = Field(default_factory=dict)


class ArtistProfileUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    artist_description: str = Field("", max_length=2000)
    vocal_description: str = Field("", max_length=2000)
    production_defaults: dict[str, Any] = Field(default_factory=dict)


class ArtistProfilePreviewRequest(BaseModel):
    project_id: str
    exclude: list[str] = Field(default_factory=list)
    apply_defaults: bool = False
    acknowledge_unverified: bool = False


class ArtistProfileLoadRequest(ArtistProfilePreviewRequest):
    preview_token: str = Field(..., min_length=8)
    confirm: bool = False
