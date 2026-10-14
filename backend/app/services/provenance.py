"""Versioned, content-addressed provenance attached to generated assets.

This intentionally records identifiers and hashes rather than copying secrets or
large workflow bodies into each project manifest.  The hash makes a workflow or
prompt-template edit observable even when its friendly version was not bumped.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from app.config import get_settings

WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / "comfyui" / "workflows"

# Every JSON graph shipped in ``comfyui/workflows`` must have a semantic version.
# Keep this explicit mapping synchronized with the directory: the regression test
# fails for both an unversioned new graph and a stale mapping entry.
WORKFLOW_VERSIONS: dict[str, str] = {
    "flux_dev_fp8": "1.0.0",
    "flux_dev_fp8_img2img": "1.0.0",
    "flux_dev_fp8_pulid": "1.0.0",
    "upscale_esrgan": "1.0.0",
    "wan21_i2v_14b_fp8": "1.0.0",
    "wan21_t2v_14b_fp8": "1.0.0",
}

# A prompt revision must change either its semantic version or its template text.
# Templates are represented here even where rendering happens in an orchestrator,
# so provenance is available without coupling asset persistence to prompt code.
PROMPT_SPECS: dict[str, tuple[str, str]] = {
    "copy.generate": ("1.0.0", "Generate marketing copy from a project brief and optional system instruction."),
    "video_plan.generate": ("1.0.0", "Generate one validated, platform-specific VideoPlan JSON document."),
    "image_plan.generate": ("1.0.0", "Generate a structured image prompt plan from approved copy."),
    "image.generate": ("1.0.0", "Generate one image from a supplied visual prompt and optional reference image."),
    "video.generate": ("1.0.0", "Generate one motion clip from a supplied motion prompt and optional source image."),
    "video.upscale": ("1.0.0", "Upscale a supplied video using the selected enhancement model and workflow."),
    "audio.synthesize": ("1.0.0", "Synthesize narration from validated voiceover text."),
    "render.composition": ("1.0.0", "Render a normalized edit decision list using the selected delivery profile."),
    "asset.user_supplied": ("1.0.0", "Persist a user-supplied artifact without implying model generation."),
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def prompt_record(prompt_id: str, rendered_text: str | None = None) -> dict[str, str]:
    """Return a versioned prompt-template record and optional rendered-text hash."""
    version, template = PROMPT_SPECS.get(prompt_id, ("unversioned", prompt_id))
    record = {
        "id": prompt_id,
        "version": version,
        "template_sha256": _sha256(template.encode("utf-8")),
    }
    if rendered_text is not None:
        record["rendered_sha256"] = _sha256(rendered_text.encode("utf-8"))
    return record


def workflow_record(name: str) -> dict[str, str]:
    """Hash the exact committed API workflow selected for a generation job."""
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.is_file():
        # Generation will fail later with its normal contract error; recording a
        # clear unavailable state preserves a truthful asset manifest for fakes
        # and makes configuration errors diagnosable.
        return {"name": name, "path": str(path.relative_to(WORKFLOWS_DIR.parents[1])).replace("\\", "/"),
                "version": WORKFLOW_VERSIONS.get(name, "unversioned"), "status": "missing"}
    return {"name": name, "path": str(path.relative_to(WORKFLOWS_DIR.parents[1])).replace("\\", "/"),
            "version": WORKFLOW_VERSIONS.get(name, "unversioned"), "sha256": _sha256(path.read_bytes())}


def render_profile_record() -> dict[str, Any]:
    profile = get_settings().social_master
    values = {
        "id": profile.profile_id,
        "version": profile.profile_version,
        "width": profile.width,
        "height": profile.height,
        "fps": profile.fps,
        "video_codec": profile.video_codec,
        "video_bitrate": profile.video_bitrate,
        "audio_codec": profile.audio_codec,
        "audio_bitrate": profile.audio_bitrate,
        "audio_sample_rate": profile.audio_sample_rate,
        "audio_channels": profile.audio_channels,
        "pix_fmt": profile.pix_fmt,
        "color_space": profile.color_space,
        "color_primaries": profile.color_primaries,
        "color_trc": profile.color_trc,
    }
    values["fingerprint"] = _json_sha256(values)
    return values


def asset_provenance(*, prompt_id: str, rendered_prompt: str | None = None,
                     models: dict[str, str | None] | None = None,
                     workflows: Iterable[str] = (), include_render_profile: bool = False) -> dict[str, Any]:
    """Build the stable ``meta.provenance`` payload for one saved asset."""
    cfg = get_settings()
    result: dict[str, Any] = {
        "schema_version": cfg.provenance.schema_version,
        "prompt": prompt_record(prompt_id, rendered_prompt),
        "models": {role: name for role, name in (models or {}).items() if name},
        "workflows": [workflow_record(name) for name in workflows if name],
    }
    if include_render_profile:
        result["render_profile"] = render_profile_record()
    return result
