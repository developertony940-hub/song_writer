"""Phase-2 canonical plan, candidate review, and proxy-delivery workflow.

This module deliberately contains no provider-specific prompting logic.  It
turns a validated :class:`VideoPlan` into the exact image, motion, narration,
captions, candidate records, and EDL that downstream workers consume.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from app.services.provenance import asset_provenance
from app.models.schemas import (
    AssetKind, CandidateRecord, CandidateStatus, Composition, GenerateAudioRequest,
    GenerateImageRequest, GeneratePlanCandidatesRequest, GenerateVideoRequest,
    PlanCandidateKind, ReviewCandidateRequest,
    ReviseCandidateRequest, VideoClip, AudioClip, SubtitleClip, VideoPlan,
    video_plan_fingerprint,
)

if TYPE_CHECKING:
    from app.services.orchestrator import Orchestrator
    from app.services.project_store import ProjectStore


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def plan_fingerprint(plan: VideoPlan) -> str:
    """Backward-compatible local name for the shared canonical fingerprint."""
    return video_plan_fingerprint(plan)


class ProductionPlanService:
    """Plan operations over a store, optionally able to invoke an orchestrator.

    Read-only evidence checks deliberately do not need an orchestrator. This
    keeps ProjectStore's state-gate validation free of dummy contexts and
    import-time cycles.
    """
    def __init__(self, store: "ProjectStore", orchestrator: "Orchestrator | None" = None) -> None:
        self.store = store
        self.orch = orchestrator

    def _orchestrator(self) -> "Orchestrator":
        if self.orch is None:
            raise RuntimeError("plan generation requires an orchestrator")
        return self.orch

    def _assert_mutable(self, project_id: str, stage: str) -> None:
        """Do not silently alter creative evidence after a human approval."""
        order = ("copy", "images", "video", "audio", "final")
        project = self.store.get(project_id)
        start = order.index(stage)
        locked = [name for name in order[start:] if project.stages.get(name) == "approved"]
        if locked:
            raise ValueError(f"cannot change plan-driven {stage} evidence after approval of {', '.join(locked)}")

    def get_plan(self, project_id: str) -> tuple[VideoPlan, str]:
        project = self.store.get(project_id)
        if not project.video_plan:
            raise ValueError("save a validated VideoPlan before starting plan production")
        stored = dict(project.video_plan)
        fingerprint = stored.pop("fingerprint", None)
        plan = VideoPlan(**stored)
        expected = plan_fingerprint(plan)
        if fingerprint != expected:
            raise ValueError("stored VideoPlan fingerprint is invalid; save the plan again")
        return plan, fingerprint

    def save_plan(self, project_id: str, plan: VideoPlan, *, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        self._assert_mutable(project_id, "copy")
        # References must be actual image assets in this project, never arbitrary IDs.
        refs = [ref for ref in [plan.style_reference_asset_id, *(s.reference_asset_id for s in plan.scenes)] if ref]
        for aid in refs:
            asset = self.store.get_asset(project_id, aid)
            if asset.kind != AssetKind.image:
                raise ValueError("VideoPlan reference assets must be image assets in the same project")
        fingerprint = plan_fingerprint(plan)
        # Keep an immutable, human-inspectable plan artifact alongside structured manifest state.
        name = f"video_plan_v{plan.version}_{fingerprint}.json"
        artifact = self.store.save_asset(project_id, AssetKind.copy, name, json.dumps(plan.model_dump(mode="json"), indent=2),
                                         meta={"kind": "video_plan", "fingerprint": fingerprint, "version": plan.version,
                                               "provenance": provenance or asset_provenance(prompt_id="asset.user_supplied")})
        # Keep the canonical-plan artifact distinct from the pre-Phase-2
        # free-form ``video_plan`` artifact.  Copy evidence is checked against
        # both this asset and the validated manifest payload, never Markdown.
        self.store.set_artifact(project_id, "production_plan", artifact.id)
        self.store.save_video_plan(project_id, plan.model_dump(mode="json"), fingerprint)
        return {"project": self.store.get(project_id), "fingerprint": fingerprint, "artifact_id": artifact.id}

    def _scene_ids(self, plan: VideoPlan, requested: list[str] | None) -> list[str]:
        ids = [s.id for s in plan.scenes]
        if requested is None:
            return ids
        if not requested or len(set(requested)) != len(requested) or any(scene not in ids for scene in requested):
            raise ValueError("scene_ids must be unique IDs from the saved VideoPlan")
        return requested

    def _candidate_records(self, project_id: str, fingerprint: str, kind: PlanCandidateKind | None = None) -> list[CandidateRecord]:
        out = []
        for raw in self.store.list_candidates(project_id):
            try:
                candidate = CandidateRecord(**raw)
            except Exception:
                continue  # legacy/corrupt review data can never count as evidence
            if candidate.plan_fingerprint == fingerprint and (kind is None or candidate.kind == kind):
                out.append(candidate)
        return out

    def selected(self, project_id: str, fingerprint: str, scene_id: str, kind: PlanCandidateKind) -> CandidateRecord | None:
        matches = [c for c in self._candidate_records(project_id, fingerprint, kind)
                   if c.scene_id == scene_id and c.selected and c.status == CandidateStatus.accepted]
        return matches[-1] if matches else None

    def candidate_evidence(self, project_id: str, kind: PlanCandidateKind) -> dict[str, Any]:
        plan, fp = self.get_plan(project_id)
        missing, selected = [], {}
        for scene in plan.scenes:
            candidate = self.selected(project_id, fp, scene.id, kind)
            if candidate is None or not self.store.asset_path(project_id, candidate.asset_id).is_file():
                missing.append(scene.id); continue
            if kind == PlanCandidateKind.video:
                image = self.selected(project_id, fp, scene.id, PlanCandidateKind.image)
                if (image is None or not self.store.asset_path(project_id, image.asset_id).is_file()
                        or candidate.reference_asset_id != image.asset_id):
                    missing.append(scene.id); continue
            selected[scene.id] = candidate
        return {"ok": not missing, "missing": missing, "selected": selected, "fingerprint": fp}

    def audio_evidence(self, project_id: str) -> dict[str, Any]:
        plan, fp = self.get_plan(project_id)
        assets = self.store.list_assets(project_id, AssetKind.audio)
        available: dict[str, Any] = {}
        for asset in assets:
            scene_id = asset.meta.get("scene_id")
            if asset.meta.get("plan_fingerprint") == fp and scene_id and self.store.asset_path(project_id, asset.id).is_file():
                available[scene_id] = asset  # last generated is current
        missing = [scene.id for scene in plan.scenes if scene.id not in available]
        return {"ok": not missing, "missing": missing, "assets": available, "fingerprint": fp}

    async def generate_candidates(self, req: GeneratePlanCandidatesRequest) -> list[CandidateRecord]:
        self._assert_mutable(req.project_id, "images" if req.kind == PlanCandidateKind.image else "video")
        plan, fp = self.get_plan(req.project_id)
        scene_ids = set(self._scene_ids(plan, req.scene_ids))
        created: list[CandidateRecord] = []
        for scene in plan.scenes:
            if scene.id not in scene_ids:
                continue
            count = scene.image_candidates if req.kind == PlanCandidateKind.image else scene.video_candidates
            source_image = None
            if req.kind == PlanCandidateKind.video:
                selected_image = self.selected(req.project_id, fp, scene.id, PlanCandidateKind.image)
                if not selected_image or not self.store.asset_path(req.project_id, selected_image.asset_id).is_file():
                    raise ValueError(f"scene '{scene.id}' requires an existing reviewed, selected image candidate before motion generation")
                source_image = selected_image.asset_id
            for ordinal in range(count):
                seed = secrets.randbelow(2_147_483_647)
                if req.kind == PlanCandidateKind.image:
                    reference = scene.reference_asset_id or plan.style_reference_asset_id
                    asset = await self._orchestrator().generate_image(GenerateImageRequest(
                        project_id=req.project_id, prompt=scene.visual_prompt, seed=seed,
                        reference_image=reference, save_as=f"{scene.id}_image_{ordinal + 1}_{seed}.png",
                    ))
                else:
                    reference = source_image
                    asset = await self._orchestrator().generate_video(GenerateVideoRequest(
                        project_id=req.project_id, mode="image_to_video", source_image=source_image,
                        prompt=scene.motion_prompt, duration_s=scene.end_s - scene.start_s, seed=seed,
                        save_as=f"{scene.id}_motion_{ordinal + 1}_{seed}.mp4",
                    ))
                record = CandidateRecord(id=uuid.uuid4().hex[:12], project_id=req.project_id, plan_fingerprint=fp,
                    scene_id=scene.id, kind=req.kind, asset_id=asset.id, seed=seed, reference_asset_id=reference)
                self.store.update_asset_meta(req.project_id, asset.id, candidate_id=record.id, plan_fingerprint=fp,
                    scene_id=scene.id, candidate_kind=req.kind.value, seed=seed, reference_asset_id=reference)
                self.store.add_candidate(req.project_id, record.model_dump(mode="json")); created.append(record)
        return created

    def review(self, project_id: str, candidate_id: str, req: ReviewCandidateRequest) -> CandidateRecord:
        plan, fp = self.get_plan(project_id)
        records = self._candidate_records(project_id, fp)
        candidate = next((record for record in records if record.id == candidate_id), None)
        if not candidate:
            raise KeyError("candidate was not found for the current VideoPlan")
        self._assert_mutable(project_id, "images" if candidate.kind == PlanCandidateKind.image else "video")
        if not self.store.asset_path(project_id, candidate.asset_id).is_file():
            raise ValueError("cannot review a candidate whose asset file is missing")
        if req.select:
            # Exactly one active choice per scene/kind. Selection is evidence, not a UI preference.
            all_records = self.store.list_candidates(project_id)
            for item in all_records:
                if item.get("plan_fingerprint") == fp and item.get("scene_id") == candidate.scene_id and item.get("kind") == candidate.kind.value:
                    item["selected"] = False
            self.store.save_candidates(project_id, all_records)
        changed = self.store.update_candidate(project_id, candidate_id, status=req.status, selected=req.select,
            review_reason=req.reason, reviewed_at=utcnow().isoformat())
        self.store.set_preview_approval(project_id, None)  # any creative review invalidates a prior proxy
        result = CandidateRecord(**changed)
        # Mark the stage succeeded only after every current-plan scene has an accepted selected result.
        stage = "images" if result.kind == PlanCandidateKind.image else "video"
        evidence = self.candidate_evidence(project_id, result.kind)
        current = self.store.get(project_id).stages.get(stage)
        if evidence["ok"] and current != "approved":
            self.store.update_stage(project_id, stage, "succeeded")
        return result

    async def revise(self, req: ReviseCandidateRequest) -> CandidateRecord:
        plan, fp = self.get_plan(req.project_id)
        old = next((r for r in self._candidate_records(req.project_id, fp) if r.id == req.candidate_id), None)
        if old is None: raise KeyError("candidate was not found for the current VideoPlan")
        self._assert_mutable(req.project_id, "images" if old.kind == PlanCandidateKind.image else "video")
        scene = next(s for s in plan.scenes if s.id == old.scene_id)
        seed = req.seed if req.seed is not None else secrets.randbelow(2_147_483_647)
        if old.kind == PlanCandidateKind.image:
            prompt, reference = f"{scene.visual_prompt}\nRevision direction: {req.instructions}", old.reference_asset_id
            asset = await self._orchestrator().generate_image(GenerateImageRequest(project_id=req.project_id, prompt=prompt, seed=seed,
                reference_image=reference, save_as=f"{scene.id}_image_revision_{seed}.png"))
        else:
            image = self.selected(req.project_id, fp, scene.id, PlanCandidateKind.image)
            if not image or not self.store.asset_path(req.project_id, image.asset_id).is_file():
                raise ValueError("select an existing accepted image candidate before revising motion")
            prompt, reference = f"{scene.motion_prompt}\nRevision direction: {req.instructions}", image.asset_id
            asset = await self._orchestrator().generate_video(GenerateVideoRequest(project_id=req.project_id, mode="image_to_video", source_image=reference,
                prompt=prompt, duration_s=scene.end_s - scene.start_s, seed=seed, save_as=f"{scene.id}_motion_revision_{seed}.mp4"))
        record = CandidateRecord(id=uuid.uuid4().hex[:12], project_id=req.project_id, plan_fingerprint=fp, scene_id=scene.id,
            kind=old.kind, asset_id=asset.id, seed=seed, reference_asset_id=reference, parent_candidate_id=old.id,
            revision_instructions=req.instructions)
        self.store.update_asset_meta(req.project_id, asset.id, candidate_id=record.id, plan_fingerprint=fp, scene_id=scene.id,
            candidate_kind=old.kind.value, seed=seed, reference_asset_id=reference, parent_candidate_id=old.id,
            revision_instructions=req.instructions)
        self.store.add_candidate(req.project_id, record.model_dump(mode="json")); self.store.set_preview_approval(req.project_id, None)
        return record

    async def generate_audio(self, project_id: str) -> list[Any]:
        self._assert_mutable(project_id, "audio")
        plan, fp = self.get_plan(project_id)
        created = []
        for scene in plan.scenes:
            orch = self._orchestrator()
            asset = await orch.generate_audio(GenerateAudioRequest(project_id=project_id, text=scene.voiceover,
                voice=orch.tts.default_voice, save_as=f"{scene.id}_voice_{uuid.uuid4().hex[:6]}.wav"))
            self.store.update_asset_meta(project_id, asset.id, plan_fingerprint=fp, scene_id=scene.id,
                source="VideoPlan.voiceover", timing={"start_s": scene.start_s, "end_s": scene.end_s})
            created.append(asset)
        if self.audio_evidence(project_id)["ok"] and self.store.get(project_id).stages["audio"] != "approved":
            self.store.update_stage(project_id, "audio", "succeeded")
        return created

    def composition(self, project_id: str) -> Composition:
        plan, fp = self.get_plan(project_id)
        videos = self.candidate_evidence(project_id, PlanCandidateKind.video)
        audios = self.audio_evidence(project_id)
        if not videos["ok"] or not audios["ok"]:
            missing = {"video": videos["missing"], "audio": audios["missing"]}
            raise ValueError(f"plan proxy/master requires selected video candidates and plan audio for every scene: {missing}")
        return Composition(total_duration_s=plan.target_duration_s,
            video_clips=[VideoClip(asset_id=videos["selected"][s.id].asset_id, start_s=s.start_s, end_s=s.end_s) for s in plan.scenes],
            audio_clips=[AudioClip(asset_id=audios["assets"][s.id].id, start_s=s.start_s, end_s=s.end_s) for s in plan.scenes],
            subtitles=[SubtitleClip(start_s=s.start_s, end_s=s.end_s, text=s.caption) for s in plan.scenes])
