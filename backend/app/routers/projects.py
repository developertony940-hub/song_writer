"""Project lifecycle and explicit review-stage API."""
from fastapi import APIRouter, HTTPException

from app.models.schemas import (ProjectCreate, ProjectMeta, SaveCompositionRequest,
                                SetModeRequest, StageUpdateRequest, SaveVideoPlanRequest, CandidateRecord, ReviewCandidateRequest)
from app.services.project_store import get_store
from app.services.orchestrator import get_orchestrator
from app.services.production_plan import ProductionPlanService

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects() -> list[ProjectMeta]:
    return get_store().list_projects()


@router.post("", response_model=ProjectMeta)
def create_project(req: ProjectCreate):
    return get_store().create(req)


@router.get("/{pid}", response_model=ProjectMeta)
def get_project(pid: str):
    try:
        return get_store().get(pid)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.patch("/{pid}/mode", response_model=ProjectMeta)
def set_mode(pid: str, req: SetModeRequest):
    try:
        return get_store().set_mode(pid, req.mode)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.patch("/{pid}/stage", response_model=ProjectMeta)
def update_stage(pid: str, req: StageUpdateRequest):
    try:
        return get_store().public_update_stage(pid, req.stage, req.status)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.put("/{pid}/composition", response_model=ProjectMeta)
def save_composition(pid: str, req: SaveCompositionRequest):
    """Persist an editable composition; rendering remains an explicit action."""
    try:
        # model_dump contains the 30-fps-normalized duration/endpoints, which
        # is the exact EDL later consumed by saved-composition rendering.
        return get_store().save_composition(pid, req.composition.model_dump(mode="json"))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.put("/{pid}/video-plan", response_model=ProjectMeta)
def save_video_plan(pid: str, req: SaveVideoPlanRequest):
    """Save the canonical validated production plan (not free-form Markdown)."""
    try:
        return get_orchestrator().save_video_plan(pid, req.plan)["project"]
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/{pid}/video-plan")
def get_video_plan(pid: str):
    try:
        plan, fingerprint = ProductionPlanService(get_store()).get_plan(pid)
        return {"plan": plan, "fingerprint": fingerprint}
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/{pid}/candidates", response_model=list[CandidateRecord])
def list_candidates(pid: str):
    try:
        # Malformed legacy review rows are never API output or stage evidence.
        valid: list[CandidateRecord] = []
        for row in get_store().list_candidates(pid):
            try:
                valid.append(CandidateRecord(**row))
            except ValueError:
                continue
        return valid
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.patch("/{pid}/candidates/{candidate_id}", response_model=CandidateRecord)
def review_candidate(pid: str, candidate_id: str, req: ReviewCandidateRequest):
    try:
        return get_orchestrator().review_candidate(pid, candidate_id, req)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
