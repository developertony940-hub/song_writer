"""FastAPI application factory and lifespan.

Mounts routers, serves the static frontend, and wires the ModelRouter's
unloaders to the service clients at startup.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.routers import assets, audiobook, generate, music, profiles, projects, replacement, runtime
from app.routers import master_audio, music_readiness
from app.services.comfyui_client import get_comfyui
from app.services.lmstudio_client import get_lmstudio
from app.services.model_router import get_router
from app.services.orchestrator import get_orchestrator
from app.services.project_store import get_store
from app.services.tts_client import get_tts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
TEMPLATES_DIR = FRONTEND_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    log.info("Starting %s v%s (projects root: %s)", s.name, s.version, s.projects_root)
    # Ensure project store root exists
    get_store()
    # Wire ModelRouter unloaders -> service clients
    router = get_router()
    router.register_unloader(0, get_lmstudio().unload)
    router.register_unloader(1, get_comfyui().unload)
    yield
    # Cleanup
    await get_lmstudio().close()
    await get_comfyui().close()
    await get_tts().close()
    log.info("Shutdown complete.")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.name, version=s.version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(projects.router)
    app.include_router(assets.router)
    app.include_router(generate.router)
    app.include_router(runtime.router)
    app.include_router(audiobook.router)
    app.include_router(music.router)
    app.include_router(profiles.router)
    app.include_router(replacement.router)
    app.include_router(master_audio.router)
    app.include_router(music_readiness.router)

    # Frontend
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/")
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request, "settings": s})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": s.version}

    @app.get("/mastering")
    async def mastering(request: Request):
        return templates.TemplateResponse("mastering.html", {"request": request})

    return app


# uvicorn entrypoint
app = create_app()