"""Export-only replacement-section manifests.

These records never apply audio to a project. They describe one fixed-duration
source-conditioned repaint candidate and the exact manual splice handles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReplacementExportRequest(BaseModel):
    project_id: str
    asset_id: str
    section: str = Field("Bridge", min_length=1, max_length=60)
    occurrence: int = Field(1, ge=1)
    target_duration_s: float = Field(..., gt=0.0)
    bridge_lyrics: str = Field(..., min_length=1)
    performance_guidance: str = Field(..., min_length=1, max_length=2000)
    context_start_s: float = Field(..., ge=0.0)
    context_end_s: float = Field(..., gt=0.0)
    target_duration_reviewed: bool = False
    context_reviewed: bool = False
    context_note: str = Field("", max_length=500)


class ReplacementExportProviderCapability(BaseModel):
    provider: str
    native_install_present: bool
    source_conditioning: bool
    duration_mode: str
    supports_longer_replacement: bool
    supports_variable_duration: bool
    comfyui_workflow_supports_repaint: bool
    native_lyrics_conditioning_supported: bool
    export_client_can_send_lyrics_and_settings: bool
    documented_manual_multipass_extension: bool
    replacement_duration_rule: str
    provider_output_duration_rule: str
    notes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ReplacementExportOutput(BaseModel):
    status: Literal["pending", "candidate"] = "pending"
    provider_output_sha256: str | None = None
    export_sha256: str | None = None
    replacement_sha256: str | None = None
    lead_in_sha256: str | None = None
    lead_out_sha256: str | None = None
    provider_output_duration_s: float | None = None
    export_duration_s: float | None = None
    replacement_duration_s: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    files: dict[str, str] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    candidate_at: datetime | None = None


class ReplacementExportManifest(BaseModel):
    schema_version: int = 1
    export_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    status: Literal["prepared", "candidate"] = "prepared"
    project_id: str
    section: str
    occurrence: int
    source: dict[str, Any]
    boundary: dict[str, Any]
    context: dict[str, Any]
    handles: dict[str, Any]
    lyrics: dict[str, Any]
    performance_guidance: str
    effective_settings: dict[str, Any]
    conditioning: dict[str, Any]
    provider_capability: ReplacementExportProviderCapability
    provider_request: dict[str, Any]
    capacity_plan: dict[str, Any]
    authorization_request: dict[str, Any]
    output: ReplacementExportOutput = Field(default_factory=ReplacementExportOutput)
    continuity_label: str = "singer/melody continuity unverified"
    apply_allowed: bool = False
    warnings: list[str] = Field(default_factory=list)
