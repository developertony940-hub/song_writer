"""Versioned production-brief records.

The production brief is a separate, additive record family. It stores the
operator's raw prompt/research verbatim, structured fields with explicit
origins, derived stanzas, reserved capability references, and immutable
revision/review/context evidence. It does not change legacy music approvals,
submission caps, settings, audio, or profile state.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

ORIGINS = (
    "user_assertion",
    "documented_evidence",
    "measured_evidence",
    "proposal",
    "contradiction",
    "unknown",
)
FieldOrigin = Literal[
    "user_assertion",
    "documented_evidence",
    "measured_evidence",
    "proposal",
    "contradiction",
    "unknown",
]

CAPABILITY_KINDS = (
    "score",
    "alignment",
    "tempo_meter_map",
    "guide",
    "midi",
    "musicxml",
    "notation",
)
CapabilityKind = Literal[
    "score",
    "alignment",
    "tempo_meter_map",
    "guide",
    "midi",
    "musicxml",
    "notation",
]
CapabilityStatus = Literal["unavailable", "unverified", "provided_unverified"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefField(_Strict):
    """One structured field with an explicit provenance classification."""

    value: str | int | float | None = None
    origin: FieldOrigin = "unknown"
    note: str = Field("", max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    evidence_verified: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def _reject_bool_value(cls, value):
        if isinstance(value, bool):
            raise ValueError("field values must be text or a finite number, not a boolean")
        return value

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value):
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError("field values must be at most 2000 characters")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("field values must be finite numbers")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _validate_refs(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 500:
                raise ValueError("evidence references must be nonempty text up to 500 characters")
            cleaned.append(item.strip())
        return cleaned

    @model_validator(mode="after")
    def _no_unverified_evidence_claim(self) -> "BriefField":
        if self.evidence_verified:
            raise ValueError("evidence cannot be marked verified in this batch; links remain unverified")
        return self


class IntendedSection(_Strict):
    name: str = Field(..., min_length=1, max_length=120)
    occurrence: int = Field(..., ge=1, le=100)
    origin: FieldOrigin = "unknown"
    note: str = Field("", max_length=1000)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("section name is required")
        if any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise ValueError("section name must not contain control characters")
        return text


class AssetReference(_Strict):
    # ProjectStore.save_asset generates AssetRef.id as uuid4().hex[:10].
    # Project IDs are 12 hex and master-source IDs are 64 hex; neither is an
    # asset ID. Historical/current take IDs are 10 lowercase hex characters.
    asset_id: str | None = Field(None, pattern=r"^[0-9a-f]{10}$")
    relative_path: str | None = Field(None, max_length=500)
    sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    label: str = Field("", max_length=200)
    origin: FieldOrigin = "unknown"
    verified: bool = False

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _require_reference(self) -> "AssetReference":
        if not self.asset_id and not self.relative_path:
            raise ValueError("asset reference requires asset_id or relative_path")
        if self.verified:
            raise ValueError("asset references cannot be marked verified in this batch")
        return self


class CapabilityReference(_Strict):
    kind: CapabilityKind
    reference: str = Field("", max_length=1000)
    status: CapabilityStatus = "unavailable"

    @model_validator(mode="after")
    def _status_matches_reference(self) -> "CapabilityReference":
        has_reference = bool(self.reference.strip())
        if has_reference and self.status == "unavailable":
            raise ValueError("a capability reference requires unverified or provided_unverified status")
        return self


class Stanza(_Strict):
    """Derived from the exact supplied lyrics; never invented or repaired."""

    index: int = Field(..., ge=1)
    section: str | None = None
    occurrence: int | None = Field(None, ge=1)
    lines: list[str]
    text: str


class ProductionBriefDraftRequest(_Strict):
    raw_prompt: str = Field(..., min_length=1, max_length=200000)
    raw_research: str = Field("", max_length=200000)
    note: str = Field("", max_length=500)
    key: BriefField | None = None
    tempo_bpm: BriefField | None = None
    meter: BriefField | None = None
    intended_sections: list[IntendedSection] = Field(default_factory=list, max_length=64)
    lyrics_text: str = Field("", max_length=100000)
    lyrics_origin: FieldOrigin = "user_assertion"
    hard_constraints: list[BriefField] = Field(default_factory=list, max_length=64)
    asset_references: list[AssetReference] = Field(default_factory=list, max_length=64)
    capabilities: list[CapabilityReference] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _unique_sections(self) -> "ProductionBriefDraftRequest":
        seen: set[tuple[str, int]] = set()
        for section in self.intended_sections:
            key = (section.name.casefold(), section.occurrence)
            if key in seen:
                raise ValueError(
                    f"duplicate intended section {section.name!r} occurrence {section.occurrence}"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _unique_capabilities(self) -> "ProductionBriefDraftRequest":
        seen: set[str] = set()
        for capability in self.capabilities:
            if capability.kind in seen:
                raise ValueError(f"duplicate capability kind {capability.kind!r}")
            seen.add(capability.kind)
        return self


class ProductionBriefReviewRequest(_Strict):
    expected_content_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    note: str = Field("", max_length=500)
    confirm: bool = Field(..., strict=True)

    @model_validator(mode="after")
    def _require_explicit_confirmation(self) -> "ProductionBriefReviewRequest":
        if self.confirm is not True:
            raise ValueError("explicit confirmation is required to review a production brief")
        return self


class ProductionBriefContextRequest(_Strict):
    section: str | None = Field(None, min_length=1, max_length=120)
    occurrence: int | None = Field(None, ge=1, le=100)
    max_tokens: int = Field(8000, ge=256, le=32000)

    @field_validator("section")
    @classmethod
    def _validate_section(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("section is required when occurrence is supplied")
        if any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise ValueError("section must not contain control characters")
        return text

    @model_validator(mode="after")
    def _require_section_pair(self) -> "ProductionBriefContextRequest":
        if (self.section is None) != (self.occurrence is None):
            raise ValueError("section and occurrence must be supplied together")
        return self


class ProductionBriefRecord(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(..., ge=1)
    parent_revision: int | None = Field(None, ge=1)
    parent_content_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    note: str = Field("", max_length=500)
    raw_prompt: str
    raw_research: str
    raw_prompt_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    raw_research_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    key: BriefField | None = None
    tempo_bpm: BriefField | None = None
    meter: BriefField | None = None
    intended_sections: list[IntendedSection] = Field(default_factory=list)
    lyrics_text: str
    lyrics_origin: FieldOrigin = "user_assertion"
    stanzas: list[Stanza] = Field(default_factory=list)
    hard_constraints: list[BriefField] = Field(default_factory=list)
    asset_references: list[AssetReference] = Field(default_factory=list)
    capabilities: list[CapabilityReference] = Field(default_factory=list)
    content_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")


class ProductionBriefReviewRecord(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(..., ge=1)
    content_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    reviewed_at: datetime
    note: str = Field("", max_length=500)
    attestation: Literal["operator_self_asserted_via_ui"] = "operator_self_asserted_via_ui"
    generation_authorized: Literal[False] = False
    audio_processing_authorized: Literal[False] = False
    tool_permissions_granted: Literal[False] = False
    legacy_music_approval_transferred: Literal[False] = False


class ProductionBriefContextRecord(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    context_id: int = Field(..., ge=1)
    revision: int = Field(..., ge=1)
    content_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    section: str | None = None
    occurrence: int | None = Field(None, ge=1)
    template_id: str
    template_version: str
    template_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    context_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    token_estimate: int = Field(..., ge=1)
    token_count_kind: Literal["estimated_characters_div_4_not_tokenizer"] = (
        "estimated_characters_div_4_not_tokenizer"
    )
    required_material_within_budget: Literal[True] = True
    prompt_injection_eliminated: Literal[False] = False
    generation: Literal[False] = False
    audio_processing: Literal[False] = False
    created_at: datetime
