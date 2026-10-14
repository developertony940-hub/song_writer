"""Frozen snapshot of ``ProjectMeta`` as it existed before the Phase 0 change.

This module is intentionally self-contained: it must NOT import from
``app.models.schemas``. A later edit to the live schema must not be able to
silently alter the rollback compatibility test, so every type and default the
previous model relied on is inlined here.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PrevProjectMeta(BaseModel):
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
