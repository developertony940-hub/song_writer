"""Generation endpoints. Each maps a request schema to an orchestrator job.

All generation endpoints are async and respect the ModelRouter's VRAM
phasing. They return the resulting AssetRef.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import (
    AssembleCompositionRequest,
    AssembleSavedCompositionRequest,
    AssembleRequest,
    AssetRef,
    Composition,
    GenerateAudioRequest,
    GenerateCopyRequest,
    GenerateImagePlanRequest,
    GenerateImageRequest,
    GenerateVideoRequest,
    RenderPreviewRequest,
    RunProjectRequest,
    SaveArtifactRequest,
    UpscaleVideoRequest, GenerateVideoPlanRequest, GeneratePlanCandidatesRequest, ReviseCandidateRequest,
    RenderPlanPreviewRequest, ApprovePreviewRequest, RenderPlanMasterRequest, CandidateRecord, AssetRef,
)
from app.services.ffmpeg_runner import get_ffmpeg
from app.services.orchestrator import get_orchestrator
from app.services.project_store import get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/generate", tags=["generate"])


@router.post("/copy", response_model=AssetRef)
async def gen_copy(req: GenerateCopyRequest):
    try:
        return await get_orchestrator().generate_copy(req)
    except Exception as e:
        log.exception("copy generation failed")
        raise HTTPException(502, f"Generation failed: {e}")


@router.post("/image", response_model=AssetRef)
async def gen_image(req: GenerateImageRequest):
    try:
        return await get_orchestrator().generate_image(req)
    except Exception as e:
        log.exception("image generation failed")
        raise HTTPException(502, f"Generation failed: {e}")


@router.post("/video", response_model=AssetRef)
async def gen_video(req: GenerateVideoRequest):
    try:
        return await get_orchestrator().generate_video(req)
    except Exception as e:
        log.exception("video generation failed")
        raise HTTPException(502, f"Generation failed: {e}")


@router.post("/audio", response_model=AssetRef)
async def gen_audio(req: GenerateAudioRequest):
    try:
        return await get_orchestrator().generate_audio(req)
    except Exception as e:
        log.exception("audio generation failed")
        raise HTTPException(502, f"Generation failed: {e}")


@router.post("/assemble", response_model=AssetRef)
async def assemble(req: AssembleRequest):
    try:
        return await get_orchestrator().assemble(req)
    except Exception as e:
        log.exception("assembly failed")
        raise HTTPException(502, f"Assembly failed: {e}")


@router.post("/assemble-composition", response_model=AssetRef)
async def assemble_composition(req: AssembleCompositionRequest):
    try:
        return await get_orchestrator().assemble_composition(req.project_id, req.composition, req.output_name)
    except ValueError as e:
        # Invalid ranges/unprobeable default ranges are caller validation errors,
        # not a runtime gateway failure.
        raise HTTPException(422, f"Composition validation failed: {e}")
    except Exception as e:
        log.exception("composition assembly failed")
        raise HTTPException(502, f"Composition assembly failed: {e}")


@router.post("/assemble-saved-composition", response_model=AssetRef)
async def assemble_saved_composition(req: AssembleSavedCompositionRequest):
    """Render the project's persisted, normalized EDL as the delivery master."""
    try:
        return await get_orchestrator().assemble_saved_composition(req.project_id, req.output_name)
    except ValueError as e:
        raise HTTPException(422, f"Saved composition validation failed: {e}")
    except Exception as e:
        log.exception("saved composition assembly failed")
        raise HTTPException(502, f"Saved composition assembly failed: {e}")


@router.post("/upscale", response_model=AssetRef)
async def upscale(req: UpscaleVideoRequest):
    try:
        return await get_orchestrator().upscale_video(req)
    except Exception as e:
        log.exception("upscale failed")
        raise HTTPException(502, f"Upscale failed: {e}")


@router.post("/image-plan", response_model=AssetRef)
async def gen_image_plan(req: GenerateImagePlanRequest):
    try:
        return await get_orchestrator().generate_image_plan(req)
    except Exception as e:
        log.exception("image plan generation failed")
        raise HTTPException(502, f"Image plan generation failed: {e}")


@router.post("/artifact", response_model=AssetRef)
async def save_artifact(req: SaveArtifactRequest):
    try:
        return await get_orchestrator().save_artifact(req)
    except Exception as e:
        log.exception("artifact save failed")
        raise HTTPException(502, f"Artifact save failed: {e}")


@router.post("/run-project")
async def run_project(req: RunProjectRequest):
    try:
        return await get_orchestrator().run_project(req.project_id)
    except Exception as e:
        log.exception("run-project failed")
        raise HTTPException(502, f"Run project failed: {e}")


# --- preview frame render ---

@router.post("/preview-frame")
async def render_preview_frame(req: RenderPreviewRequest):
    """Render a single preview frame at the given timestamp with subtitle
    overlays burned in. Returns the image file directly."""
    comp = req.composition
    if not comp.video_clips:
        raise HTTPException(400, "No video clips in composition")
    store = get_store()
    try:
        # Find the video clip active at this timestamp
        active_clip = None
        for clip in comp.video_clips:
            start = clip.start_s
            end = clip.end_s if clip.end_s is not None else comp.total_duration_s
            if start <= req.timestamp_s <= end:
                active_clip = clip
                break
        if active_clip is None:
            active_clip = comp.video_clips[0]
        video_path = store.asset_path(req.project_id, active_clip.asset_id)
    except KeyError:
        raise HTTPException(404, "Video asset not found")
    if not video_path.exists():
        raise HTTPException(404, "Video file missing on disk")
    # Compute local timestamp within the clip
    local_ts = req.timestamp_s - active_clip.start_s + active_clip.trim_start_s
    out_dir = store.asset_dir(req.project_id, "final")
    out_path = out_dir / f"preview_{req.timestamp_s:.1f}.png"
    ff = get_ffmpeg()
    sub_dicts = [s.model_dump() for s in comp.subtitles]
    try:
        await ff.render_preview_frame(video_path, local_ts, sub_dicts, out_path)
    except Exception as e:
        log.exception("preview render failed")
        raise HTTPException(502, f"Preview render failed: {e}")
    return FileResponse(out_path, media_type="image/png")

# --- Phase 2 plan-driven production ---------------------------------
@router.post("/video-plan")
async def generate_video_plan(req: GenerateVideoPlanRequest):
    try:
        result = await get_orchestrator().generate_video_plan(req)
        return {"project": result["project"], "fingerprint": result["fingerprint"], "artifact_id": result["artifact_id"]}
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        log.exception("VideoPlan generation failed")
        raise HTTPException(502, f"Generation failed: {exc}")

@router.post("/plan/candidates", response_model=list[CandidateRecord])
async def generate_plan_candidates(req: GeneratePlanCandidatesRequest):
    try:
        return await get_orchestrator().generate_plan_candidates(req)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("plan candidate generation failed")
        raise HTTPException(502, f"Generation failed: {exc}")


@router.post("/plan/revise", response_model=CandidateRecord)
async def revise_plan_candidate(req: ReviseCandidateRequest):
    try:
        return await get_orchestrator().revise_candidate(req)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("candidate revision failed")
        raise HTTPException(502, f"Generation failed: {exc}")


@router.post("/plan/audio", response_model=list[AssetRef])
async def generate_plan_audio(req: RunProjectRequest):
    try:
        return await get_orchestrator().generate_plan_audio(req.project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("plan audio generation failed")
        raise HTTPException(502, f"Generation failed: {exc}")


@router.post("/plan/preview", response_model=AssetRef)
async def render_plan_preview(req: RenderPlanPreviewRequest):
    try:
        return await get_orchestrator().render_plan_preview(req.project_id, req.output_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("plan proxy rendering failed")
        raise HTTPException(502, f"Proxy render failed: {exc}")


@router.post("/plan/approve-preview")
async def approve_plan_preview(req: ApprovePreviewRequest):
    try:
        return get_orchestrator().approve_plan_preview(req.project_id, req.preview_asset_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, str(exc))


@router.post("/plan/render-master", response_model=AssetRef)
async def render_plan_master(req: RenderPlanMasterRequest):
    try:
        return await get_orchestrator().render_plan_master(req.project_id, req.output_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("plan master rendering failed")
        raise HTTPException(502, f"Master render failed: {exc}")
