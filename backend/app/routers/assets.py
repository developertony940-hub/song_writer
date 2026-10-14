"""Asset listing and download routes."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from app.models.schemas import AssetKind, AssetRef
from app.services.project_store import get_store

router = APIRouter(prefix="/api/projects/{pid}/assets", tags=["assets"])

@router.get("")
def list_assets(pid: str, kind: AssetKind | None = None) -> list[AssetRef]:
    try: return get_store().list_assets(pid, kind)
    except KeyError as exc: raise HTTPException(404, str(exc))

@router.get("/{aid}")
def download_asset(pid: str, aid: str):
    try: path = get_store().asset_path(pid, aid)
    except KeyError as exc: raise HTTPException(404, str(exc))
    if not path.is_file(): raise HTTPException(404, "Asset file is missing")
    return FileResponse(path, filename=path.name)
