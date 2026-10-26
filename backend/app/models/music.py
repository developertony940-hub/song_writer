"""Versioned music workflow schemas.

The music workflow is intentionally separate from narration and social-video
assets. Lyrics/settings approval binds exact content hashes; generation and
audio approval are separate human actions.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

KEY_SCALES = {
    "C major", "C# major", "Db major", "D major", "D# major", "Eb major",
    "E major", "F major", "F# major", "Gb major", "G major", "G# major",
    "Ab major", "A major", "A# major", "Bb major", "B major",
    "C minor", "C# minor", "Db minor", "D minor", "D# minor", "Eb minor",
    "E minor", "F minor", "F# minor", "Gb minor", "G minor", "G# minor",
    "Ab minor", "A minor", "A# minor", "Bb minor", "B minor",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


from app.services.music_input_review import SECTION_TAG_RE, parse_section_tags


class MusicSettings(BaseModel):
    musical_direction: str = Field(..., min_length=8, max_length=2000)
    lyrics: str = Field(..., min_length=8, max_length=12000)
    duration_s: float = Field(30.0, ge=10, le=600)
    language: str = Field("en", min_length=2, max_length=16)
    bpm: int = Field(120, ge=10, le=300)
    time_signature: Literal["2", "3", "4", "6"] = "4"
    key_scale: str = Field("C major", min_length=3, max_length=40)
    generate_audio_codes: bool = True
    seed: int = Field(0, ge=0, le=2**63 - 1)
    arrangement: dict[str, str] = Field(default_factory=dict)
    temperature: float | None = Field(None, gt=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    cfg_scale: float | None = Field(None, gt=0, le=10)

    @field_validator("key_scale")
    @classmethod
    def _validate_key_scale(cls, value: str) -> str:
        if value not in KEY_SCALES:
            raise ValueError("key_scale must be a supported ACE-Step key")
        return value

    @field_validator("arrangement")
    @classmethod
    def _validate_arrangement(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("arrangement supports at most 20 section directives")
        total = 0
        for section, directive in value.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}", section):
                raise ValueError(f"arrangement section {section!r} is not a valid section name")
            if not directive.strip():
                raise ValueError(f"arrangement directive for [{section}] is empty")
            if len(directive) > 300:
                raise ValueError(f"arrangement directive for [{section}] exceeds 300 characters")
            total += len(section) + len(directive)
        if total > 2000:
            raise ValueError("arrangement exceeds 2000 total characters")
        return value


class MusicDraftRequest(BaseModel):
    project_id: str
    settings: MusicSettings
    revision_name: str | None = Field(None, min_length=1, max_length=60)


class MusicApproveRequest(BaseModel):
    project_id: str
    note: str = Field(..., min_length=1, max_length=500)
    submission_cap: int = Field(..., ge=1, le=4)
    variant_plan: list["MusicVariantPlanItem"] = Field(default_factory=list, max_length=4)


class MusicGenerateRequest(BaseModel):
    project_id: str
    plan_label: str | None = Field(None, min_length=1, max_length=60)


class MusicCritiqueSuggestion(BaseModel):
    target: str = Field(..., min_length=1, max_length=120)
    kind: Literal["lyric", "arrangement", "settings", "direction"]
    current: str = Field("", max_length=500)
    alternatives: list[str] = Field(..., min_length=1, max_length=8)
    rationale: str = Field(..., min_length=1, max_length=1000)

    @field_validator("alternatives")
    @classmethod
    def _validate_alternatives(cls, value: list[str]) -> list[str]:
        cleaned = [alternative.strip() for alternative in value]
        if not cleaned or any(not alternative for alternative in cleaned):
            raise ValueError("suggestion alternatives must be non-empty strings")
        if any(len(alternative) > 500 for alternative in cleaned):
            raise ValueError("suggestion alternatives must be at most 500 characters")
        return cleaned


class MusicCritiqueRequest(BaseModel):
    project_id: str
    summary: str = Field(..., min_length=1, max_length=2000)
    suggestions: list[MusicCritiqueSuggestion] = Field(..., min_length=1, max_length=50)
    key_candidates: list[str] = Field(default_factory=list, max_length=12)
    refined_direction: str | None = Field(None, min_length=8, max_length=2000)

    @field_validator("key_candidates")
    @classmethod
    def _validate_key_candidates(cls, value: list[str]) -> list[str]:
        for key in value:
            if key not in KEY_SCALES:
                raise ValueError("key_candidates must be supported ACE-Step keys")
        return value


class MusicVariantRequest(BaseModel):
    project_id: str
    name: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
    section: str = Field(..., min_length=1, max_length=40)
    occurrence: int = Field(..., ge=1)
    text: str = Field(..., min_length=1, max_length=4000)
    note: str = Field("", max_length=500)


class MusicVariantPromoteRequest(BaseModel):
    project_id: str


class MusicVariantPlanItem(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)
    seed: int | None = Field(None, ge=0, le=2**63 - 1)
    key_scale: str | None = Field(None, min_length=3, max_length=40)
    bpm: int | None = Field(None, ge=10, le=300)
    temperature: float | None = Field(None, gt=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    cfg_scale: float | None = Field(None, gt=0, le=10)

    @field_validator("key_scale")
    @classmethod
    def _validate_key_scale(cls, value: str | None) -> str | None:
        if value is not None and value not in KEY_SCALES:
            raise ValueError("key_scale must be a supported ACE-Step key")
        return value

    @model_validator(mode="after")
    def _require_override(self) -> "MusicVariantPlanItem":
        overridden = any(
            getattr(self, field_name) is not None
            for field_name in ("seed", "key_scale", "bpm", "temperature", "top_p", "cfg_scale")
        )
        if not overridden:
            raise ValueError(
                "plan item must override at least one of seed, key_scale, bpm, "
                "temperature, top_p, cfg_scale")
        return self


class MusicCancelRequest(BaseModel):
    project_id: str


class MusicRecoverRequest(BaseModel):
    project_id: str
    confirm_stopped: bool


class MusicAudioApproveRequest(BaseModel):
    project_id: str
    asset_id: str
    note: str = Field(..., min_length=1, max_length=500)


class MusicPreferTakeRequest(BaseModel):
    project_id: str
    asset_id: str
    note: str = Field(..., min_length=1, max_length=500)


class MusicRepaintProposalRequest(BaseModel):
    project_id: str
    source_asset_id: str
    start_s: float = Field(..., ge=0.0)
    end_s: float = Field(..., gt=0.0)
    prompt: str = Field(..., min_length=1, max_length=2000)
    context_s: float = Field(3.0, ge=0.0, le=30.0)


class MusicSectionBoundaryRequest(BaseModel):
    project_id: str
    asset_id: str
    section: str = Field(..., min_length=1, max_length=60)
    occurrence: int = Field(..., ge=1)
    arrangement_index: int = Field(..., ge=0)
    start_s: float = Field(..., ge=0.0)
    end_s: float = Field(..., gt=0.0)
    suggested_start_s: float | None = None
    suggested_end_s: float | None = None
    shared_start_from: str | None = Field(None, max_length=120)


class MusicRepaintAuthorizeRequest(BaseModel):
    project_id: str
    note: str = Field(..., min_length=1, max_length=500)


class MusicRepaintRejectRequest(BaseModel):
    project_id: str
    reason: str = Field("", max_length=500)


class MusicRepaintReviseRequest(BaseModel):
    project_id: str
    start_s: float = Field(..., ge=0.0)
    end_s: float = Field(..., gt=0.0)
    prompt: str = Field(..., min_length=1, max_length=2000)
    context_s: float = Field(3.0, ge=0.0, le=30.0)


class MusicRepaintRecoverRequest(BaseModel):
    project_id: str
    confirm_stopped: bool = False


class MusicRepaintCapacityProposalRequest(BaseModel):
    project_id: str
    measured_free_vram_mib: int = Field(..., ge=1)
    gpu_index: int = Field(..., ge=0)
    competing_processes: list[str] = Field(default_factory=list)
    note: str = Field(..., min_length=1, max_length=500)


class MusicDraftRecord(BaseModel):
    version: int
    settings: MusicSettings
    lyrics_sha256: str
    settings_sha256: str
    revision_name: str = Field("", max_length=60)
    saved_at: datetime = Field(default_factory=_utcnow)


class MusicRevisionRecord(BaseModel):
    version: int
    name: str = Field("", max_length=60)
    parent_version: int | None = None
    source: Literal["draft", "promote", "restore", "repaint"] = "draft"
    settings: MusicSettings
    lyrics_sha256: str
    settings_sha256: str
    created_at: datetime = Field(default_factory=_utcnow)


class MusicApprovalRecord(BaseModel):
    draft_version: int
    lyrics_sha256: str
    settings_sha256: str
    submission_cap: int
    note: str
    variant_plan: list[MusicVariantPlanItem] = Field(default_factory=list)
    approved_at: datetime = Field(default_factory=_utcnow)


class MusicCritiqueRecord(BaseModel):
    critique_id: str
    draft_version: int
    lyrics_sha256: str
    settings_sha256: str
    summary: str
    suggestions: list[MusicCritiqueSuggestion]
    key_candidates: list[str]
    refined_direction: str | None
    created_at: datetime = Field(default_factory=_utcnow)


class MusicLyricVariant(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
    section: str = Field(..., min_length=1, max_length=40)
    occurrence: int = Field(..., ge=1)
    text: str = Field(..., min_length=1, max_length=4000)
    lyrics_sha256: str
    note: str = Field("", max_length=500)
    created_at: datetime = Field(default_factory=_utcnow)


class MusicAttemptRecord(BaseModel):
    id: str
    status: Literal["running", "completed", "failed", "uncertain", "recovered"]
    prompt_id: str | None = None
    workflow: str | None = None
    checkpoint: str | None = None
    lyrics_sha256: str | None = None
    settings_sha256: str | None = None
    plan_label: str | None = None
    revision: int | None = None
    cancel_requested: bool = False
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None
    error: str | None = None
    asset_id: str | None = None
    audio_sha256: str | None = None


class MusicAudioApprovalRecord(BaseModel):
    asset_id: str
    audio_sha256: str
    note: str
    approved_at: datetime = Field(default_factory=_utcnow)


class MusicPreferredTakeRecord(BaseModel):
    asset_id: str
    attempt_id: str
    audio_sha256: str
    revision: int | None = None
    note: str
    selected_at: datetime = Field(default_factory=_utcnow)


class MusicRepaintCandidateRecord(BaseModel):
    raw_asset_id: str
    master_asset_id: str
    raw_sha256: str
    master_sha256: str
    sample_rate: int
    channels: int
    duration_samples: int
    start_sample: int
    end_sample: int
    transition_start_sample: int
    transition_end_sample: int
    source_sha256: str
    assembly: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class MusicRepaintProposalRecord(BaseModel):
    proposal_id: str
    source_asset_id: str
    source_attempt_id: str
    source_revision: int
    source_sha256: str
    source_duration_s: float
    start_s: float
    end_s: float
    start_sample: int
    end_sample: int
    context_start_s: float
    context_end_s: float
    prompt: str
    settings: MusicSettings
    lyrics_sha256: str
    settings_sha256: str
    provider: str
    model: str
    workflow: str
    output_format: str
    status: Literal[
        "proposed", "authorized", "submitted", "candidate", "applied",
        "rejected", "failed", "uncertain",
    ] = "proposed"
    authorization_note: str = ""
    authorized_fingerprint: str = ""
    authorized_at: datetime | None = None
    attempt_id: str | None = None
    candidate: MusicRepaintCandidateRecord | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class MusicSectionBoundaryRecord(BaseModel):
    source_asset_id: str
    source_sha256: str
    source_revision: int
    section: str
    occurrence: int
    arrangement_index: int
    suggested_start_s: float | None = None
    suggested_end_s: float | None = None
    start_s: float
    end_s: float
    start_sample: int
    end_sample: int
    source_duration_s: float
    shared_start_from: str | None = None
    confirmed_at: datetime


class MusicState(BaseModel):
    schema_version: int = 5
    draft: MusicDraftRecord | None = None
    approval: MusicApprovalRecord | None = None
    lifetime_cap: int | None = None
    current_attempt: MusicAttemptRecord | None = None
    critiques: list[MusicCritiqueRecord] = Field(default_factory=list)
    variants: list[MusicLyricVariant] = Field(default_factory=list)
    revisions: list[MusicRevisionRecord] = Field(default_factory=list)
    preferred_take: MusicPreferredTakeRecord | None = None
    repaint_proposals: list[MusicRepaintProposalRecord] = Field(default_factory=list)
    section_boundaries: list[MusicSectionBoundaryRecord] = Field(default_factory=list)
    recovery_required: bool = False
    recovery_reason: str | None = None
    audio_approval: MusicAudioApprovalRecord | None = None
    updated_at: datetime = Field(default_factory=_utcnow)
