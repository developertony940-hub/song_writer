"""Read-only readiness; intentionally no POST dispatch/lifecycle operations."""
from fastapi import APIRouter, HTTPException

from app.services.music_readiness import music_readiness

router = APIRouter(prefix="/api/projects", tags=["music-readiness"])


@router.get("/{pid}/music/readiness")
async def readiness(pid: str):
    try:
        return await music_readiness(pid)
    except KeyError:
        raise HTTPException(404, "Project not found")
    except (ValueError, OSError, RuntimeError):
        # Never turn corrupt/inconsistent persisted accounting into an empty,
        # apparently ready project. Do not expose filesystem paths or secrets.
        raise HTTPException(409, "Music readiness cannot be established: saved state or accounting is unreadable/inconsistent")
