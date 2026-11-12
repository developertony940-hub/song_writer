"""Typed contracts for every edge in the agent pipeline.

recommendations_validation.html Section 6.2. One contract per edge means a
producer fails small and locally instead of forcing a repair of the whole
draft. The final artifact is still a ``MusicSettings``: that contract is
untouched, so approval hashes and gates behave exactly as before.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.music import KEY_SCALES

Verdict = Literal["pass", "revise", "reject"]


class SongBrief(BaseModel):
    """What the operator asked for, after deterministic intake."""

    title: str = ""
    bpm: int | None = None
    key_scale: str | None = None
    time_signature: Literal["2", "3", "4", "6"] | None = None
    stated_duration_s: float | None = None
    references: list[str] = Field(default_factory=list)
    research: str = ""
    mix_chain: str = ""
    chords: str = ""
    has_operator_lyrics: bool = False
    operator_lyric_lines: int = 0
    source_sha256: str = ""
    gaps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SectionSpec(BaseModel):
    name: str
    occurrence: int = 1
    chords: list[str] = Field(default_factory=list)
    energy: Literal["sparse", "building", "full", "peak", "fading"] = "full"
    intent: str = ""


class CompositionPlan(BaseModel):
    key_scale: str = "C minor"
    bpm: int = 120
    time_signature: Literal["2", "3", "4", "6"] = "4"
    duration_s: float = 30.0
    sections: list[SectionSpec] = Field(default_factory=list)
    rationale: str = Field("", max_length=400)


class LyricsDraft(BaseModel):
    lyrics: str = ""
    written_sections: list[str] = Field(default_factory=list)


class ArrangementPlan(BaseModel):
    arrangement: dict[str, str] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class DirectionSpec(BaseModel):
    musical_direction: str = ""
    references: list[str] = Field(default_factory=list)


class MixPlan(BaseModel):
    checklist: str = ""


class Defect(BaseModel):
    code: str
    section: str = ""
    message: str = ""
    severity: Literal["info", "warn", "error"] = "warn"


class Critique(BaseModel):
    """A critic's verdict. Critics never write project state."""

    critic: str
    verdict: Verdict = "pass"
    defects: list[Defect] = Field(default_factory=list)
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Patch(BaseModel):
    """A minimal correction request. Only the cited defect may change."""

    defect_codes: list[str] = Field(default_factory=list)
    section: str = ""
    instruction: str = ""
    forbidden_fields: list[str] = Field(default_factory=list)


class TakeCandidate(BaseModel):
    asset_id: str
    attempt_id: str = ""
    revision: int | None = None
    duration_s: float | None = None
    seed: int | None = None
    preferred: bool = False
    plan_label: str = ""


class TakeRanking(BaseModel):
    ranked: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)
    preferred: str | None = None
    labelled: int = 0
    status: Literal["ranked", "insufficient_labels"] = "ranked"
    note: str = ""


class PolicyVersion(BaseModel):
    """A versioned agent policy. Adoption always needs a human approval."""

    agent: str
    policy_version: int = 1
    template: str = ""
    template_sha256: str = ""
    model_role: str = "strong_prose"
    model_id: str = ""
    temperature: float = 0.4
    exemplars: list[str] = Field(default_factory=list)
    budget: dict[str, int] = Field(default_factory=dict)
    scorecard: dict[str, object] = Field(default_factory=dict)
    status: Literal["draft", "approved", "active", "superseded", "rejected"] = "draft"
    approved_by: str = ""
    approved_at: str = ""
    adopted_at: str = ""
    notes: str = ""


class RunResult(BaseModel):
    """The outcome of one pipeline run."""

    run_id: str
    mode: Literal["fast", "decomposed"] = "fast"
    status: Literal["saved", "partial", "failed"] = "failed"
    draft_version: int | None = None
    lyrics_sha256: str = ""
    settings_sha256: str = ""
    title: str = ""
    model: str = ""
    steps: list[dict] = Field(default_factory=list)
    defects: list[Defect] = Field(default_factory=list)
    filled: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    budget: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


KEYS = tuple(sorted(KEY_SCALES))
