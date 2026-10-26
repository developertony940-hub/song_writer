"""Bounded ACE-Step music workflow API.

Lyrics/settings approval, generation, cancellation/recovery, and audio approval
are separate explicit operations. Live submission is refused unless the
process-scoped music authorization hold is enabled.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.models.music import (
    MusicApproveRequest,
    MusicAudioApproveRequest,
    MusicCancelRequest,
    MusicCritiqueRequest,
    MusicDraftRequest,
    MusicGenerateRequest,
    MusicPreferTakeRequest,
    MusicRecoverRequest,
    MusicRepaintAuthorizeRequest,
    MusicRepaintCapacityProposalRequest,
    MusicRepaintProposalRequest,
    MusicRepaintRecoverRequest,
    MusicRepaintRejectRequest,
    MusicRepaintReviseRequest,
    MusicSectionBoundaryRequest,
    MusicVariantPromoteRequest,
    MusicVariantRequest,
)
from app.services.take_export_service import TakeExportError, get_take_export_service
from app.services.music_service import (
    MusicApprovalRequired,
    MusicCapReached,
    MusicConflict,
    MusicError,
    MusicGenerationFailed,
    MusicNotAuthorized,
    MusicRecoveryRequired,
    MusicRuntimeUnavailable,
    get_music_service,
)

router = APIRouter(prefix="/api/music", tags=["music"])


def _check_project(pid: str, body_pid: str) -> None:
    if body_pid != pid:
        raise HTTPException(422, "project_id must match the URL project")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    if isinstance(exc, MusicNotAuthorized):
        return HTTPException(403, str(exc))
    if isinstance(exc, MusicRuntimeUnavailable):
        return HTTPException(503, {"detail": str(exc), "runtime": exc.runtime})
    if isinstance(exc, MusicCapReached):
        return HTTPException(409, str(exc))
    if isinstance(exc, MusicRecoveryRequired):
        return HTTPException(409, str(exc))
    if isinstance(exc, MusicApprovalRequired):
        return HTTPException(409, str(exc))
    if isinstance(exc, MusicConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, MusicGenerationFailed):
        return HTTPException(502, str(exc))
    if isinstance(exc, MusicError):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


@router.get("/runtime")
async def runtime():
    return await get_music_service().runtime()


@router.get("/{pid}")
async def status(pid: str):
    try:
        return await get_music_service().status(pid)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.put("/{pid}/draft")
def save_draft(pid: str, req: MusicDraftRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().save_draft(pid, req.settings, req.revision_name)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/approve")
def approve(pid: str, req: MusicApproveRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().approve(
            pid, req.note, req.submission_cap, req.variant_plan)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/generate")
async def generate(pid: str, req: MusicGenerateRequest):
    _check_project(pid, req.project_id)
    try:
        return await get_music_service().generate(pid, req.plan_label)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/critique")
def save_critique(pid: str, req: MusicCritiqueRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().save_critique(pid, req)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/critique/{critique_id}")
def get_critique(pid: str, critique_id: str):
    try:
        return get_music_service().get_critique(pid, critique_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.put("/{pid}/variant")
def register_variant(pid: str, req: MusicVariantRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().register_variant(pid, req)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/variant/{name}")
def get_variant(pid: str, name: str):
    try:
        return get_music_service().get_variant(pid, name)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/variant/{name}/promote")
def promote_variant(pid: str, name: str, req: MusicVariantPromoteRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().promote_variant(pid, name)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/revisions")
def list_revisions(pid: str):
    try:
        return get_music_service().revisions(pid)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/revisions/compare")
def compare_revisions(pid: str, from_version: int, to_version: int):
    try:
        return get_music_service().revision_compare(pid, from_version, to_version)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/revisions/{version}")
def get_revision(pid: str, version: int):
    try:
        return get_music_service().revision_detail(pid, version)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/revisions/{version}/restore")
def restore_revision(pid: str, version: int):
    try:
        return get_music_service().restore_revision(pid, version)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/takes")
def list_takes(pid: str):
    try:
        return get_music_service().takes(pid)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/takes/{asset_id}/provenance")
def take_provenance(pid: str, asset_id: str):
    try:
        return get_take_export_service().audit(pid, asset_id)["audit"]
    except (KeyError, TakeExportError) as exc:
        raise HTTPException(404 if isinstance(exc, KeyError) else 409, str(exc))


@router.get("/{pid}/takes/{asset_id}/package")
def take_package(pid: str, asset_id: str):
    try:
        content, filename, _digest = get_take_export_service().package(pid, asset_id)
    except (KeyError, TakeExportError) as exc:
        raise HTTPException(404 if isinstance(exc, KeyError) else 409, str(exc))
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{pid}/take/prefer")
def prefer_take(pid: str, req: MusicPreferTakeRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().prefer_take(pid, req.asset_id, req.note)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/cancel")
async def cancel(pid: str, req: MusicCancelRequest):
    _check_project(pid, req.project_id)
    try:
        return await get_music_service().cancel(pid)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/recover")
async def recover(pid: str, req: MusicRecoverRequest):
    _check_project(pid, req.project_id)
    try:
        return await get_music_service().recover(pid, req.confirm_stopped)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/audio/approve")
async def approve_audio(pid: str, req: MusicAudioApproveRequest):
    _check_project(pid, req.project_id)
    try:
        return await get_music_service().approve_audio(pid, req.asset_id, req.note)
    except (KeyError, MusicError) as exc:
        raise _error(exc)

@router.get("/{pid}/repaint/proposals")
def list_repaint_proposals(pid: str):
    try:
        return get_music_service().repaint_proposals(pid)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals")
def create_repaint_proposal(pid: str, req: MusicRepaintProposalRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().create_repaint_proposal(
            pid,
            source_asset_id=req.source_asset_id,
            start_s=req.start_s,
            end_s=req.end_s,
            prompt=req.prompt,
            context_s=req.context_s,
        )
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/repaint/proposals/{proposal_id}/compare")
def compare_repaint_proposal(pid: str, proposal_id: str):
    try:
        return get_music_service().compare_repaint(pid, proposal_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/repaint/proposals/{proposal_id}")
def get_repaint_proposal(pid: str, proposal_id: str):
    try:
        return get_music_service().repaint_proposal(pid, proposal_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals/{proposal_id}/authorize")
def authorize_repaint_proposal(pid: str, proposal_id: str, req: MusicRepaintAuthorizeRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().authorize_repaint(pid, proposal_id, req.note)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals/{proposal_id}/reject")
def reject_repaint_proposal(pid: str, proposal_id: str, req: MusicRepaintRejectRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().reject_repaint(pid, proposal_id, req.reason)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals/{proposal_id}/submit")
def submit_repaint_proposal(pid: str, proposal_id: str):
    try:
        return get_music_service().submit_repaint(pid, proposal_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals/{proposal_id}/apply")
def apply_repaint_proposal(pid: str, proposal_id: str):
    try:
        return get_music_service().apply_repaint(pid, proposal_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.get("/{pid}/repaint/capacity-gate")
def get_repaint_capacity_gate(pid: str):
    try:
        return {"gate": get_music_service().repaint_capacity_gate(pid)}
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/capacity-gate")
def propose_repaint_capacity_gate(pid: str, req: MusicRepaintCapacityProposalRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().propose_repaint_capacity_gate(
            pid,
            measured_free_vram_mib=req.measured_free_vram_mib,
            gpu_index=req.gpu_index,
            competing_processes=req.competing_processes,
            note=req.note,
        )
    except (KeyError, MusicError) as exc:
        raise _error(exc)

@router.post("/{pid}/repaint/proposals/{proposal_id}/revise")
def revise_repaint_proposal(pid: str, proposal_id: str, req: MusicRepaintReviseRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().revise_repaint(
            pid, proposal_id,
            start_s=req.start_s, end_s=req.end_s,
            prompt=req.prompt, context_s=req.context_s,
        )
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/repaint/proposals/{proposal_id}/recover")
def recover_repaint_proposal(pid: str, proposal_id: str, req: MusicRepaintRecoverRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().recover_repaint(pid, proposal_id, req.confirm_stopped)
    except (KeyError, MusicError) as exc:
        raise _error(exc)

@router.get("/{pid}/section-boundaries")
def list_section_boundaries(pid: str, asset_id: str | None = None):
    try:
        return get_music_service().section_boundaries(pid, asset_id)
    except (KeyError, MusicError) as exc:
        raise _error(exc)


@router.post("/{pid}/section-boundaries")
def save_section_boundary(pid: str, req: MusicSectionBoundaryRequest):
    _check_project(pid, req.project_id)
    try:
        return get_music_service().save_section_boundary(
            pid,
            asset_id=req.asset_id,
            section=req.section,
            occurrence=req.occurrence,
            arrangement_index=req.arrangement_index,
            start_s=req.start_s,
            end_s=req.end_s,
            suggested_start_s=req.suggested_start_s,
            suggested_end_s=req.suggested_end_s,
            shared_start_from=req.shared_start_from,
        )
    except (KeyError, MusicError) as exc:
        raise _error(exc)
