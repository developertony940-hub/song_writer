"""Schemas for the Phase 1 audiobook slice.

Scope is deliberately narrow: one locked chapter, a narrator plus character
voices, verbatim speaker spans over the manuscript, deterministic chunks, WAV
assembly and MP3 delivery, and byte-bound approval. No adaptation, music, or
M4B metadata is represented here.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChunkStatus(str, Enum):
    pending = "pending"
    rendered = "rendered"
    failed = "failed"


class ManuscriptRef(BaseModel):
    """Immutable, hash-locked chapter text.

    Offsets elsewhere in this module are Unicode code-point indices into the
    stored text, whose line endings were normalized to ``\\n`` at import.
    """
    filename: str
    sha256: str
    char_count: int = Field(ge=0)
    newline_policy: str = "lf"
    locked: bool = True


class Speaker(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    voice: str = Field(min_length=1, max_length=64)
    speed: float = Field(1.0, gt=0, le=4.0)


class SpeakerSpan(BaseModel):
    """A verbatim region of the manuscript attributed to one speaker."""
    id: str = Field(min_length=1, max_length=64)
    speaker: str = Field(min_length=1, max_length=64)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    needs_review: bool = False


class Exclusion(BaseModel):
    """A region intentionally omitted from the spoken text (e.g. a heading)."""
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=200)


class AnnotationSet(BaseModel):
    version: int = Field(0, ge=0)
    spans: list[SpeakerSpan] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)


class Chunk(BaseModel):
    id: str
    span_id: str
    speaker: str
    text: str
    start_char: int
    end_char: int
    status: ChunkStatus = ChunkStatus.pending
    audio_file: str | None = None
    sha256: str | None = None
    rendered_at: str | None = None


class Master(BaseModel):
    wav_file: str
    wav_sha256: str
    mp3_file: str
    mp3_sha256: str
    chunk_versions: list[dict[str, Any]] = Field(default_factory=list)
    annotation_version: int
    pause_ms: int
    loudness_target: str | None = None
    created_at: str


class Approval(BaseModel):
    wav_sha256: str
    mp3_sha256: str
    chunk_versions: list[dict[str, Any]] = Field(default_factory=list)
    annotation_version: int
    approved_at: str


class Review(BaseModel):
    """A recorded listen-back of the assembled master.

    Binds the operator's attestation to the exact WAV/MP3 bytes present at
    review time. A later byte change invalidates the review, so approval can
    never precede listening.
    """
    wav_sha256: str
    mp3_sha256: str
    listened_wav: bool
    listened_mp3: bool
    note: str | None = None
    reviewed_at: str


class RecoveryState(BaseModel):
    blocked: bool = False
    reason: str | None = None
    since: str | None = None


class AudiobookState(BaseModel):
    manuscript: ManuscriptRef | None = None
    speakers: dict[str, Speaker] = Field(default_factory=dict)
    annotations: AnnotationSet = Field(default_factory=AnnotationSet)
    chunks: list[Chunk] = Field(default_factory=list)
    master: Master | None = None
    review: Review | None = None
    approval: Approval | None = None
    recovery: RecoveryState = Field(default_factory=RecoveryState)


# --- request bodies -------------------------------------------------------

class ImportChapterRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    filename: str = Field("chapter.txt", min_length=1, max_length=120)


class SetSpeakersRequest(BaseModel):
    speakers: list[Speaker] = Field(min_length=1, max_length=32)


class SetAnnotationsRequest(BaseModel):
    spans: list[SpeakerSpan] = Field(default_factory=list, max_length=10_000)
    exclusions: list[Exclusion] = Field(default_factory=list, max_length=10_000)


class RenderRequest(BaseModel):
    force: bool = False


class AssembleRequest(BaseModel):
    pause_ms: int = Field(300, ge=0, le=5000)


class ReviewRequest(BaseModel):
    """Operator listen-back attestation for the current assembled master."""
    listened_wav: bool
    listened_mp3: bool
    note: str | None = Field(None, max_length=500)


class ClearRecoveryRequest(BaseModel):
    """Documented stop/drain evidence required to clear a recovery hold.

    A bare acknowledgement is insufficient: the operator must assert that the
    worker is stopped and its queue is drained, and attach a durable evidence
    reference (log path, command, ticket id, etc.).
    """
    worker_stopped: bool
    queue_drained: bool
    evidence: str = Field(min_length=10, max_length=2000)
    note: str | None = Field(None, max_length=500)
