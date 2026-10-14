"""Pydantic schemas for projects, generation requests, and runtime status."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
TIMELINE_FPS = 30
CAPTION_SAFE_WIDTH = 936
CAPTION_SAFE_HEIGHT = 1440
_CAPTION_COLORS = {"white", "black", "red", "green", "blue", "yellow", "cyan", "magenta"}
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate_caption_color(value: str, *, background: bool) -> str:
    value = value.strip().lower()
    name, separator, opacity = value.partition("@")
    if name not in _CAPTION_COLORS and not _COLOR_RE.fullmatch(name):
        raise ValueError("caption color must be a supported name or #RRGGBB")
    if separator:
        if not background:
            raise ValueError("caption foreground color cannot include opacity")
        try:
            alpha = float(opacity)
        except ValueError as exc:
            raise ValueError("caption background opacity must be a number from 0 to 1") from exc
        if not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise ValueError("caption background opacity must be from 0 to 1")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_output_name(value: str) -> str:
    """Accept a simple stem only; callers never control a filesystem path."""
    if not _OUTPUT_NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("output_name must be a basename using letters, digits, '.', '_' or '-'")
    return value


def quantize_timeline_seconds(value: float) -> float:
    """Round a finite timestamp to the nearest 30-fps edit boundary.

    Timeline data is persisted in seconds for API compatibility, but all visual
    edit points have a deterministic frame representation.  Half frames round
    forward, avoiding Python's banker's-rounding surprise.
    """
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("timeline timestamps must be finite")
    return math.floor(value * TIMELINE_FPS + 0.5) / TIMELINE_FPS


def _caption_max_chars(width: int, font_size: int) -> int:
    # Conservative average-glyph estimate. This is intentionally stricter than
    # the renderer so a declared safe box cannot be defeated by a long token.
    return max(8, int(width / (font_size * 0.65)))


def _normalise_caption_text(value: str, width: int, font_size: int) -> str:
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if not text:
        raise ValueError("caption text must contain visible characters")
    if any(len(word) > _caption_max_chars(width, font_size) for word in text.split(" ")):
        raise ValueError("caption contains an unbroken token too long for its safe width")
    # The renderer supports two lines only. Reject rather than silently clipping
    # content that a reviewer believes will be visible.
    max_chars = _caption_max_chars(width, font_size)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > 2:
        raise ValueError("caption does not fit within two lines at its declared safe width")
    return text


class AssetKind(str, Enum):
    copy = "copy"
    image = "image"
    video = "video"
    audio = "audio"
    music = "music"
    final = "final"
    preview = "preview"


class Product(str, Enum):
    """Project product identity.

    ``social_video`` is the only product creatable in the current phase. The
    remaining members are reserved for later phases and have no creation path,
    stage graph, or behavior yet. A project is social-video by the *absence* of
    the ``product`` field; the value is only ever written by the internal
    migration path (``ProjectStore.set_product``).
    """
    social_video = "social_video"
    audiobook = "audiobook"
    podcast = "podcast"
    song = "song"


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    approved = "approved"


class Platform(str, Enum):
    tiktok = "tiktok"
    instagram_reels = "instagram_reels"
    youtube_shorts = "youtube_shorts"


_PLATFORM_LIMITS: dict[Platform, tuple[float, float]] = {
    Platform.tiktok: (5.0, 180.0),
    Platform.instagram_reels: (5.0, 90.0),
    Platform.youtube_shorts: (5.0, 180.0),
}


class PlanScene(BaseModel):
    """One timed, self-contained social-video beat from the canonical plan."""
    id: str = Field(..., pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    voiceover: str = Field(..., min_length=1, max_length=700)
    caption: str = Field(..., min_length=1, max_length=300)
    visual_prompt: str = Field(..., min_length=12, max_length=4000)
    motion_prompt: str = Field(..., min_length=8, max_length=2000)
    image_candidates: int = Field(2, ge=1, le=4)
    video_candidates: int = Field(1, ge=1, le=3)
    reference_asset_id: str | None = None

    @model_validator(mode="after")
    def validate_timing_and_caption(self) -> "PlanScene":
        if not math.isfinite(self.start_s) or not math.isfinite(self.end_s) or self.end_s <= self.start_s:
            raise ValueError("scene end_s must be finite and greater than start_s")
        self.start_s, self.end_s = quantize_timeline_seconds(self.start_s), quantize_timeline_seconds(self.end_s)
        if self.end_s <= self.start_s:
            raise ValueError("scene duration collapses after 30-fps normalization")
        # Reuse production caption safe-area/line validation rather than trusting plan prose.
        self.caption = _normalise_caption_text(self.caption, CAPTION_SAFE_WIDTH, 58)
        return self


class VideoPlan(BaseModel):
    """Validated production source of truth for a vertical social video.

    All narration, captions, images, motion candidates and planned edit points
    are derived from these scenes; free-form copy is never sent to TTS by the
    plan workflow.
    """
    platform: Platform
    target_duration_s: float = Field(..., gt=0)
    hook: str = Field(..., min_length=3, max_length=300)
    cta: str = Field(..., min_length=2, max_length=300)
    scenes: list[PlanScene] = Field(..., min_length=2, max_length=30)
    style_reference_asset_id: str | None = None
    version: int = Field(1, ge=1)

    @model_validator(mode="after")
    def validate_production_contract(self) -> "VideoPlan":
        low, high = _PLATFORM_LIMITS[self.platform]
        if not math.isfinite(self.target_duration_s):
            raise ValueError("target_duration_s must be finite")
        self.target_duration_s = quantize_timeline_seconds(self.target_duration_s)
        if not low <= self.target_duration_s <= high:
            raise ValueError(f"{self.platform.value} plans must be between {low:g} and {high:g} seconds")
        ids = [scene.id for scene in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("scene ids must be unique")
        scenes = sorted(self.scenes, key=lambda scene: scene.start_s)
        if scenes[0].start_s != 0:
            raise ValueError("the hook scene must start at 0 seconds")
        previous = 0.0
        for scene in scenes:
            if abs(scene.start_s - previous) > 1 / TIMELINE_FPS / 2:
                raise ValueError("scenes must provide contiguous coverage with no gaps or overlaps")
            previous = scene.end_s
        if abs(previous - self.target_duration_s) > 1 / TIMELINE_FPS / 2:
            raise ValueError("scene timing must end exactly at target_duration_s")
        self.scenes = scenes
        words = sum(len(scene.voiceover.split()) for scene in scenes)
        # Conservative 2.8 spoken words/sec: a plan that exceeds it cannot be reliably narrated.
        if words > math.floor(self.target_duration_s * 2.8):
            raise ValueError("voiceover exceeds the 2.8 words/second spoken-word budget")
        if self.hook.lower() not in scenes[0].voiceover.lower():
            raise ValueError("first scene voiceover must include the declared hook")
        if self.cta.lower() not in scenes[-1].voiceover.lower():
            raise ValueError("final scene voiceover must include the declared CTA")
        return self


def video_plan_fingerprint(plan: VideoPlan) -> str:
    """Stable identifier for the validated, canonical plan payload."""
    import hashlib
    import json

    raw = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class SaveVideoPlanRequest(BaseModel):
    plan: VideoPlan


class GenerateVideoPlanRequest(BaseModel):
    project_id: str
    platform: Platform = Platform.tiktok
    model: str | None = None


class PlanCandidateKind(str, Enum):
    image = "image"
    video = "video"


class CandidateStatus(str, Enum):
    pending_review = "pending_review"
    accepted = "accepted"
    rejected = "rejected"


class CandidateRecord(BaseModel):
    id: str
    project_id: str
    plan_fingerprint: str
    scene_id: str
    kind: PlanCandidateKind
    asset_id: str
    seed: int
    reference_asset_id: str | None = None
    parent_candidate_id: str | None = None
    revision_instructions: str | None = None
    status: CandidateStatus = CandidateStatus.pending_review
    selected: bool = False
    review_reason: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    reviewed_at: datetime | None = None


class ReviewCandidateRequest(BaseModel):
    status: Literal["accepted", "rejected"]
    reason: str = Field(..., min_length=2, max_length=1000)
    select: bool = False

    @model_validator(mode="after")
    def selected_must_be_accepted(self) -> "ReviewCandidateRequest":
        if self.select and self.status != "accepted":
            raise ValueError("only an accepted candidate can be selected")
        return self


class GeneratePlanCandidatesRequest(BaseModel):
    project_id: str
    scene_ids: list[str] | None = None
    kind: PlanCandidateKind = PlanCandidateKind.image


class ReviseCandidateRequest(BaseModel):
    project_id: str
    candidate_id: str
    instructions: str = Field(..., min_length=3, max_length=1000)
    seed: int | None = Field(None, ge=0, le=2_147_483_647)


class RenderPlanPreviewRequest(BaseModel):
    project_id: str
    output_name: str = "plan_preview"
    validate_output_name = field_validator("output_name")(_validate_output_name)


class ApprovePreviewRequest(BaseModel):
    project_id: str
    preview_asset_id: str


class RenderPlanMasterRequest(BaseModel):
    project_id: str
    output_name: str = "final_master"
    validate_output_name = field_validator("output_name")(_validate_output_name)


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    brief: str = Field("")
    brand_voice: str = Field("professional")
    target_audience: str = Field("")
    notes: str = Field("")


class ProjectMeta(BaseModel):
    id: str
    name: str
    brief: str
    brand_voice: str
    target_audience: str
    notes: str
    mode: str = Field("manual", description="manual or auto; both require review approval")
    created_at: datetime
    updated_at: datetime
    asset_counts: dict[str, int] = Field(default_factory=dict)
    stages: dict[str, str] = Field(default_factory=lambda: {
        "copy": "pending", "images": "pending", "video": "pending", "audio": "pending", "final": "pending",
    })
    artifacts: dict[str, str | None] = Field(default_factory=lambda: {
        # ``video_plan`` remains the legacy free-form artifact slot. The canonical
        # Phase-2 JSON plan has its own namespace and is never overwritten by it.
        "copy_plan": None, "image_plan": None, "video_plan": None, "audio_plan": None,
        "production_plan": None,
    })
    composition: dict[str, Any] | None = None
    # Phase-2 canonical production plan and a short-lived approval of its proxy.
    video_plan: dict[str, Any] | None = None
    preview_approval: dict[str, Any] | None = None
    # Product identity and manifest schema version. Both are presence-gated:
    # absence means "legacy social video", and the writer must never inject them
    # merely because the model carries a default. ``exclude=True`` keeps them out
    # of every serialized API response (and out of ``model_dump``); the manifest
    # writer reads the attributes directly instead.
    product: Product | None = Field(default=None, exclude=True)
    manifest_schema_version: int | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def reject_explicit_null_product_fields(self) -> "ProjectMeta":
        """Explicit null is rejected in code, not by convention.

        An absent key means legacy social video; a present key must carry a real
        value. This fires for any source (or reconstruction) that passes the
        field explicitly as null, so a stored null cannot be silently accepted.
        """
        if "product" in self.model_fields_set and self.product is None:
            raise ValueError(
                "product must not be explicitly null; omit the key for legacy social-video projects"
            )
        if "manifest_schema_version" in self.model_fields_set and self.manifest_schema_version is None:
            raise ValueError(
                "manifest_schema_version must not be explicitly null; omit the key when unset"
            )
        return self


class AssetRef(BaseModel):
    id: str
    project_id: str
    kind: AssetKind
    filename: str
    relative_path: str
    created_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)


class GenerateCopyRequest(BaseModel):
    project_id: str
    prompt: str
    model: str | None = None
    system_prompt: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    save_as: str | None = Field(None, description="Filename under copy/ (e.g. hero.md)")


class GenerateImageRequest(BaseModel):
    project_id: str
    prompt: str
    negative_prompt: str = ""
    width: int = 1080
    height: int = 1920
    steps: int = 20
    seed: int | None = None
    save_as: str | None = None
    reference_image: str | None = None
    denoise: float | None = None
    face_reference: str | None = None


class GenerateVideoRequest(BaseModel):
    project_id: str
    mode: Literal["text_to_video", "image_to_video"] = "image_to_video"
    prompt: str
    source_image: str | None = None
    duration_s: float = Field(5.0, gt=0, le=60)
    fps: int = Field(16, gt=0, le=120)
    width: int = Field(720, gt=0)
    height: int = Field(1280, gt=0)
    seed: int | None = None
    save_as: str | None = None


class GenerateAudioRequest(BaseModel):
    project_id: str
    text: str
    voice: str | None = None
    speed: float = Field(1.0, gt=0)
    save_as: str | None = None


class AssembleRequest(BaseModel):
    project_id: str
    video_asset_id: str | None = None
    audio_asset_id: str | None = None
    music_asset_id: str | None = None
    subtitle_text: str | None = Field(None, max_length=1000)
    output_name: str = "final"

    validate_output_name = field_validator("output_name")(_validate_output_name)

    @field_validator("subtitle_text")
    @classmethod
    def sanitize_legacy_subtitle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # A legacy input becomes one literal VTT cue, never caller-controlled
        # cue syntax. Limit it to a readable two-line-equivalent payload.
        literal = " ".join("".join(ch if ch >= " " else " " for ch in value).split())
        if not literal:
            raise ValueError("subtitle_text must contain visible characters")
        if len(literal) > 300:
            raise ValueError("subtitle_text is too long for one bounded legacy cue")
        return literal


class UpscaleVideoRequest(BaseModel):
    project_id: str
    video_asset_id: str
    model_name: str = "RealESRGAN_x2plus.pth"
    target_size: int = 1920
    save_as: str | None = None


class StageUpdateRequest(BaseModel):
    """The public review endpoint deliberately cannot forge worker states."""
    stage: Literal["copy", "images", "video", "audio", "final"]
    status: Literal["pending", "approved"]


class SubtitleClip(BaseModel):
    """A two-line, social-safe burn-in caption segment.

    ``x`` is the left/centre/right anchor selected by ``align`` and ``y`` is the
    vertical centre of the declared caption box. Zero retains legacy behaviour
    and resolves to the safe centre/bottom position during validation.
    """
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=1000)
    x: int = 0
    y: int = 0
    width: int = Field(CAPTION_SAFE_WIDTH, ge=200, le=CAPTION_SAFE_WIDTH)
    height: int = Field(120, ge=40, le=CAPTION_SAFE_HEIGHT)
    font_size: int = Field(58, ge=28, le=96)
    color: str = "white"
    bg_color: str = "black@0.5"
    align: Literal["left", "center", "right"] = "center"

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        return _validate_caption_color(value, background=False)

    @field_validator("bg_color")
    @classmethod
    def validate_background_color(cls, value: str) -> str:
        return _validate_caption_color(value, background=True)

    @model_validator(mode="after")
    def validate_box_and_text(self) -> "SubtitleClip":
        if not math.isfinite(self.start_s) or not math.isfinite(self.end_s):
            raise ValueError("subtitle times must be finite")
        if self.end_s <= self.start_s:
            raise ValueError("subtitle end_s must be greater than start_s")
        self.text = _normalise_caption_text(self.text, self.width, self.font_size)
        # Two 1.2em lines plus at least a modest visual padding must fit the
        # explicit height; otherwise there is no safe implementation of it.
        min_height = math.ceil(self.font_size * 2.0) + 4
        if self.height < min_height:
            raise ValueError("caption height is too small for two lines at this font_size")
        x = self.x or (72 if self.align == "left" else 540 if self.align == "center" else 1008)
        y = self.y or 1500
        if self.align == "left":
            horizontal_ok = 72 <= x and x + self.width <= 1008
        elif self.align == "right":
            horizontal_ok = 72 <= x - self.width and x <= 1008
        else:
            horizontal_ok = 72 <= x - self.width / 2 and x + self.width / 2 <= 1008
        if not horizontal_ok or not (180 <= y - self.height / 2 and y + self.height / 2 <= 1620):
            raise ValueError("caption box must remain within the social-safe area")
        return self


class VideoClip(BaseModel):
    asset_id: str
    start_s: float = Field(0.0, ge=0)
    end_s: float | None = Field(None, gt=0)
    trim_start_s: float = Field(0.0, ge=0)


class AudioClip(BaseModel):
    asset_id: str
    start_s: float = Field(0.0, ge=0)
    end_s: float | None = Field(None, gt=0)
    trim_start_s: float = Field(0.0, ge=0)
    volume: float = Field(1.0, ge=0, le=4)


class Composition(BaseModel):
    """30-fps normalized edit decision list persisted by the project store."""
    total_duration_s: float = Field(30.0, gt=0, le=3600)
    video_clips: list[VideoClip] = Field(default_factory=list)
    audio_clips: list[AudioClip] = Field(default_factory=list)
    subtitles: list[SubtitleClip] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_and_validate_timeline(self) -> "Composition":
        total = quantize_timeline_seconds(self.total_duration_s)
        if total < 1 / TIMELINE_FPS:
            raise ValueError("composition must be at least one 30-fps frame")
        self.total_duration_s = total
        for label, clips in (("video", self.video_clips), ("audio", self.audio_clips)):
            for clip in clips:
                start = quantize_timeline_seconds(clip.start_s)
                end = total if clip.end_s is None else quantize_timeline_seconds(clip.end_s)
                trim_start = quantize_timeline_seconds(clip.trim_start_s)
                if start < 0 or trim_start < 0 or end <= start:
                    raise ValueError(f"{label} clip end_s must be greater than start_s after 30-fps normalization")
                if end > total:
                    raise ValueError(f"{label} clip interval must be within composition duration")
                clip.start_s, clip.end_s, clip.trim_start_s = start, end, trim_start
        for subtitle in self.subtitles:
            subtitle.start_s = quantize_timeline_seconds(subtitle.start_s)
            subtitle.end_s = quantize_timeline_seconds(subtitle.end_s)
            if subtitle.end_s <= subtitle.start_s:
                raise ValueError("subtitle interval collapses after 30-fps normalization")
            if subtitle.end_s > total:
                raise ValueError("subtitle interval must be within composition duration")
        return self


class SaveCompositionRequest(BaseModel):
    composition: Composition


class SetModeRequest(BaseModel):
    mode: Literal["manual", "auto"]


class GenerateImagePlanRequest(BaseModel):
    project_id: str
    model: str | None = None
    filename: str | None = None


class SaveArtifactRequest(BaseModel):
    project_id: str
    stage: Literal["copy_plan", "image_plan", "video_plan", "audio_plan"]
    content: str
    filename: str | None = None


class RunProjectRequest(BaseModel):
    project_id: str


class AssembleCompositionRequest(BaseModel):
    project_id: str
    composition: Composition
    output_name: str = "final"

    validate_output_name = field_validator("output_name")(_validate_output_name)


class AssembleSavedCompositionRequest(BaseModel):
    project_id: str
    output_name: str = "final"

    validate_output_name = field_validator("output_name")(_validate_output_name)


class RenderPreviewRequest(BaseModel):
    project_id: str
    timestamp_s: float = Field(ge=0)
    composition: Composition


class RuntimeStatus(BaseModel):
    lmstudio: dict[str, Any]
    comfyui: dict[str, Any]
    tts: dict[str, Any]
    ffmpeg: dict[str, Any]
    vram: dict[str, Any]


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Job(BaseModel):
    id: str
    kind: str
    status: JobStatus
    project_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
