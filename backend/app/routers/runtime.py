"""Runtime status + model management router."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.lmstudio_client import get_lmstudio
from app.services.model_router import get_router
from app.services.orchestrator import get_orchestrator
from app.services.preflight import RuntimePreflight
from app.services.tts_client import get_tts
from app.services.ffmpeg_runner import get_ffmpeg
from app.services.comfyui_client import get_comfyui

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/status")
async def runtime_status():
    return await get_orchestrator().status()


@router.get("/preflight")
async def runtime_preflight():
    """Read-only end-to-end readiness check; no model is loaded or queued."""
    checker = RuntimePreflight(lmstudio=get_lmstudio(), comfyui=get_comfyui(),
                               tts=get_tts(), ffmpeg=get_ffmpeg())
    return await checker.run()


@router.get("/vram")
async def vram_status():
    return get_router().status()


@router.get("/lmstudio/models")
async def lmstudio_models():
    """Return the model IDs currently available from LM Studio."""
    try:
        return {"online": True, "models": await get_lmstudio().list_models()}
    except Exception as e:
        raise HTTPException(503, f"LM Studio unavailable: {e}")


@router.get("/models")
async def list_known_models():
    """Return the curated model profiles known to the router."""
    from app.services.model_router import PROFILES
    return {
        name: {"size_mb": p.size_mb, "gpu": int(p.slot), "category": p.category}
        for name, p in PROFILES.items()
    }