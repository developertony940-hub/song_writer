"""Orchestrator: high-level generation jobs that coordinate the services and
the ModelRouter. Each public method is a coroutine returning an AssetRef.

Flow:
  1. Acquire VRAM slot for the target model.
  2. Run the service call.
  3. Persist the asset via ProjectStore.
  4. Release the slot (optionally unload).

Jobs never hold two heavy models simultaneously (enforced by ModelRouter's
sequential_phasing).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.models.schemas import (
    AssetKind,
    AssetRef,
    AssembleRequest,
    Composition,
    GenerateAudioRequest,
    GenerateCopyRequest,
    GenerateImageRequest,
    GenerateImagePlanRequest,
    GenerateVideoRequest,
    SaveArtifactRequest,
    UpscaleVideoRequest,
    VideoPlan, GenerateVideoPlanRequest, GeneratePlanCandidatesRequest, ReviewCandidateRequest, ReviseCandidateRequest,
)
from app.config import get_settings
from app.services.comfyui_client import get_comfyui
from app.services.ffmpeg_runner import get_ffmpeg
from app.services.lmstudio_client import get_lmstudio
from app.services.model_router import get_router
from app.services.project_store import get_store
from app.services.production_plan import ProductionPlanService
from app.services.provenance import asset_provenance
from app.services.tts_client import get_tts

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self.store = get_store()
        self.router = get_router()
        self.lms = get_lmstudio()
        self.comfy = get_comfyui()
        self.tts = get_tts()
        self.ffmpeg = get_ffmpeg()
        self.production = ProductionPlanService(self.store, self)

    # --- Phase 2: canonical VideoPlan workflow ----------------------
    def save_video_plan(self, project_id: str, plan: VideoPlan, *, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.production.save_plan(project_id, plan, provenance=provenance)
        # A manually saved validated production plan is content-stage output,
        # but it still requires the existing explicit copy approval.
        stage = self.store.get(project_id).stages["copy"]
        if stage != "approved":
            self.store.update_stage(project_id, "copy", "succeeded")
        return {**result, "project": self.store.get(project_id)}

    async def generate_video_plan(self, req: GenerateVideoPlanRequest) -> dict[str, Any]:
        """Ask the local LLM for JSON, then fail closed through VideoPlan validation."""
        project = self.store.get(req.project_id)
        model, job_id = req.model or self.lms.default_model, uuid.uuid4().hex[:10]
        old_version = int((project.video_plan or {}).get("version", 0))
        copy = ""
        copy_id = project.artifacts.get("copy_plan")
        if copy_id:
            try: copy = self.store.asset_path(req.project_id, copy_id).read_text(encoding="utf-8")[:12000]
            except (KeyError, OSError): pass
        system = "You are a social-video producer. Return only a valid JSON object; no Markdown or commentary."
        prompt = (f"Build a {req.platform.value} 9:16 VideoPlan for this project. Brief: {project.brief!r}. "
                  f"Audience: {project.target_audience!r}. Brand voice: {project.brand_voice!r}. Source copy: {copy!r}. "
                  "Schema keys: platform, target_duration_s, hook, cta, scenes. Each scene needs id, start_s, end_s, "
                  "voiceover, caption, visual_prompt, motion_prompt, image_candidates, video_candidates. Use at least two scenes; "
                  "scene timings must start at 0, be contiguous, end exactly at target_duration_s; include hook exactly in first "
                  "voiceover and CTA exactly in final voiceover. Keep narration <=2.8 words/sec.")
        await self.router.acquire(model, job_id)
        try:
            raw = await self.lms.chat_text(prompt, model=model, system_prompt=system, temperature=.25, max_tokens=5000)
        finally:
            await self.router.release(model, job_id, unload=False)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.rstrip().endswith("```"): cleaned = cleaned.rstrip()[:-3]
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM did not return a JSON VideoPlan") from exc
        data["platform"] = req.platform.value  # caller, not the model, chooses delivery platform
        data["version"] = old_version + 1
        return self.save_video_plan(req.project_id, VideoPlan(**data), provenance=asset_provenance(
            prompt_id="video_plan.generate", rendered_prompt=f"{system}\n{prompt}", models={"llm": model}
        ))

    async def generate_plan_candidates(self, req: GeneratePlanCandidatesRequest):
        return await self.production.generate_candidates(req)

    def review_candidate(self, project_id: str, candidate_id: str, req: ReviewCandidateRequest):
        return self.production.review(project_id, candidate_id, req)

    async def revise_candidate(self, req: ReviseCandidateRequest):
        return await self.production.revise(req)

    async def generate_plan_audio(self, project_id: str):
        self.store.set_preview_approval(project_id, None)
        return await self.production.generate_audio(project_id)

    @staticmethod
    def _composition_digest(comp: Composition) -> str:
        import hashlib
        raw = json.dumps(comp.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _sha256_path(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def composition_media_evidence(self, project_id: str, comp: Composition | None = None) -> dict[str, Any]:
        """Content-address every video/audio byte used by the normalized EDL."""
        comp = comp or self.production.composition(project_id)
        media: list[dict[str, Any]] = []
        for role, clips in (("video", comp.video_clips), ("audio", comp.audio_clips)):
            for clip in clips:
                path = self.store.asset_path(project_id, clip.asset_id)
                if not path.is_file() or path.stat().st_size == 0:
                    raise ValueError(f"composition {role} asset {clip.asset_id} is missing or empty")
                media.append({"role": role, "asset_id": clip.asset_id, "sha256": self._sha256_path(path),
                              "start_s": clip.start_s, "end_s": clip.end_s, "trim_start_s": clip.trim_start_s})
        media.sort(key=lambda x: (x["role"], x["asset_id"], x["start_s"], -1 if x["end_s"] is None else x["end_s"]))
        return {"media": media, "digest": hashlib.sha256(json.dumps(media, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}

    async def render_plan_preview(self, project_id: str, output_name: str = "plan_preview") -> AssetRef:
        comp = self.production.composition(project_id)
        _plan, fingerprint = self.production.get_plan(project_id)
        evidence = self.composition_media_evidence(project_id, comp)
        out_dir = self.store.asset_dir(project_id, AssetKind.preview).resolve()
        out_path = (out_dir / f"{output_name}.mp4").resolve()
        if out_path.parent != out_dir: raise ValueError("preview output path escapes project")
        self.store.set_preview_approval(project_id, None)
        await self.ffmpeg.render_composition(
            video_paths=[{"path": str(self.store.asset_path(project_id, c.asset_id)), "start_s": c.start_s, "end_s": c.end_s, "trim_start_s": c.trim_start_s} for c in comp.video_clips],
            audio_paths=[{"path": str(self.store.asset_path(project_id, c.asset_id)), "start_s": c.start_s, "end_s": c.end_s, "trim_start_s": c.trim_start_s, "volume": c.volume} for c in comp.audio_clips],
            subtitles=[x.model_dump() for x in comp.subtitles], total_duration_s=comp.total_duration_s, output_path=out_path, proxy=True)
        if not out_path.is_file() or out_path.stat().st_size == 0: raise RuntimeError("preview render did not create output")
        return self.store.save_asset(project_id, AssetKind.preview, out_path.name, None, meta={
            "plan_fingerprint": fingerprint, "composition_digest": self._composition_digest(comp), "source_media": evidence["media"],
            "source_media_digest": evidence["digest"], "proxy_sha256": self._sha256_path(out_path),
            "kind": "review_proxy", "delivery_evidence": False, "profile": "540x960@15 h264/aac",
            "provenance": asset_provenance(prompt_id="render.composition", include_render_profile=True),
        })

    def approve_plan_preview(self, project_id: str, preview_asset_id: str) -> dict[str, Any]:
        comp = self.production.composition(project_id)
        _plan, fingerprint = self.production.get_plan(project_id)
        evidence = self.composition_media_evidence(project_id, comp)
        preview = self.store.get_asset(project_id, preview_asset_id)
        path = self.store.asset_path(project_id, preview.id)
        actual = self._sha256_path(path) if path.is_file() else None
        digest = self._composition_digest(comp)
        if (preview.kind != AssetKind.preview or preview.meta.get("plan_fingerprint") != fingerprint or
            preview.meta.get("composition_digest") != digest or preview.meta.get("source_media_digest") != evidence["digest"] or
            preview.meta.get("proxy_sha256") != actual):
            raise ValueError("preview is stale, altered, or does not match current plan/composition/source media")
        project = self.store.set_preview_approval(project_id, {"preview_asset_id": preview.id, "plan_fingerprint": fingerprint,
            "composition_digest": digest, "proxy_sha256": actual, "source_media_digest": evidence["digest"],
            "source_media": evidence["media"], "approved_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
        return {"project": project, "preview_asset_id": preview.id}

    async def render_plan_master(self, project_id: str, output_name: str = "final_master") -> AssetRef:
        comp = self.production.composition(project_id)
        _plan, fingerprint = self.production.get_plan(project_id)
        approval = self.store.get(project_id).preview_approval or {}
        digest = self._composition_digest(comp); evidence = self.composition_media_evidence(project_id, comp)
        preview_id = approval.get("preview_asset_id")
        if not preview_id or approval.get("plan_fingerprint") != fingerprint or approval.get("composition_digest") != digest:
            raise ValueError("render and explicitly approve the current plan proxy before rendering a master")
        try:
            preview = self.store.get_asset(project_id, preview_id); preview_path = self.store.asset_path(project_id, preview_id)
        except KeyError as exc: raise ValueError("approved proxy is missing; render and approve a new proxy") from exc
        if (preview.kind != AssetKind.preview or not preview_path.is_file() or self._sha256_path(preview_path) != approval.get("proxy_sha256") or
            preview.meta.get("proxy_sha256") != approval.get("proxy_sha256") or approval.get("source_media_digest") != evidence["digest"]):
            raise ValueError("approved proxy bytes or source-media hashes are stale; render and review a new proxy")
        return await self.assemble_composition(project_id, comp, output_name, final_meta={
            "plan_fingerprint": fingerprint, "composition_digest": digest, "approved_preview_asset_id": preview_id,
            "approved_preview_sha256": approval["proxy_sha256"], "source_media": evidence["media"], "source_media_digest": evidence["digest"], "source": "VideoPlan"})

    # --- copy --------------------------------------------------------
    async def generate_copy(self, req: GenerateCopyRequest) -> AssetRef:
        job_id = uuid.uuid4().hex[:10]
        model = req.model or self.lms.default_model
        await self.router.acquire(model, job_id)
        try:
            system = req.system_prompt or (
                f"You are a {self.store.get(req.project_id).brand_voice} marketing copywriter. "
                f"Output clean Markdown. Audience: {self.store.get(req.project_id).target_audience or 'general'}."
            )
            text = await self.lms.chat_text(
                req.prompt,
                model=model,
                system_prompt=system,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            )
        finally:
            await self.router.release(model, job_id, unload=False)
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = req.save_as or f"copy_{ts}.md"
        ref = self.store.save_asset(
            req.project_id, AssetKind.copy, filename, text, meta={"model": model, "prompt": req.prompt, "provenance": asset_provenance(
                prompt_id="copy.generate", rendered_prompt=f"{system}\n{req.prompt}", models={"llm": model}
            )}
        )
        self.store.set_artifact(req.project_id, "copy_plan", ref.id)
        self.store.update_stage(req.project_id, "copy", "succeeded")
        return ref

    # --- image -------------------------------------------------------
    async def generate_image(self, req: GenerateImageRequest) -> AssetRef:
        job_id = uuid.uuid4().hex[:10]
        model = "flux_dev_fp8"
        ref_b64: str | None = None
        if req.reference_image:
            ref_path = self.store.asset_path(req.project_id, req.reference_image)
            ref_b64 = base64.b64encode(ref_path.read_bytes()).decode("ascii")
        face_b64: str | None = None
        if req.face_reference:
            face_path = self.store.asset_path(req.project_id, req.face_reference)
            face_b64 = base64.b64encode(face_path.read_bytes()).decode("ascii")
        await self.router.acquire(model, job_id)
        try:
            result = await self.comfy.generate_image(
                req.prompt,
                negative_prompt=req.negative_prompt,
                width=req.width,
                height=req.height,
                steps=req.steps,
                seed=req.seed,
                reference_image_b64=ref_b64,
                denoise=req.denoise,
                face_reference_b64=face_b64,
            )
        finally:
            await self.router.release(model, job_id, unload=True)  # free FLUX VRAM before video models load
        filename = req.save_as or f"image_{uuid.uuid4().hex[:6]}.png"
        ref = self.store.save_asset(
            req.project_id, AssetKind.image, filename, result["bytes"],
            meta={"model": model, "prompt": req.prompt, "seed": req.seed, "size": f"{req.width}x{req.height}",
                  "provenance": asset_provenance(prompt_id="image.generate", rendered_prompt=req.prompt,
                      models={"image": model}, workflows=[result.get("workflow", getattr(self.comfy, "workflow_image", "flux_dev_fp8"))])},
        )
        self._advance_legacy_asset_stage(req.project_id, "images")
        return ref

    # --- video -------------------------------------------------------
    async def generate_video(self, req: GenerateVideoRequest) -> AssetRef:
        job_id = uuid.uuid4().hex[:10]
        model = "wan21_i2v_14b_fp8" if req.mode == "image_to_video" else "wan21_t2v_14b_fp8"
        source_b64: str | None = None
        if req.mode == "image_to_video":
            if not req.source_image:
                raise ValueError("image_to_video requires source_image")
            src_path = self.store.asset_path(req.project_id, req.source_image)
            source_b64 = base64.b64encode(src_path.read_bytes()).decode("ascii")
        await self.router.acquire(model, job_id)
        try:
            result = await self.comfy.generate_video(
                req.prompt,
                mode=req.mode,
                source_image_b64=source_b64,
                width=req.width,
                height=req.height,
                duration_s=req.duration_s,
                fps=req.fps,
                seed=req.seed,
            )
        finally:
            await self.router.release(model, job_id, unload=True)  # video models are huge
        ext = "mp4"  # ComfyUI VHS nodes typically output mp4
        filename = req.save_as or f"video_{uuid.uuid4().hex[:6]}.{ext}"
        ref = self.store.save_asset(
            req.project_id, AssetKind.video, filename, result["bytes"],
            meta={"model": model, "prompt": req.prompt, "mode": req.mode,
                  "duration_s": result.get("duration_s", req.duration_s),
                  "frames": result.get("frames"), "fps": req.fps,
                  "provenance": asset_provenance(prompt_id="video.generate", rendered_prompt=req.prompt,
                      models={"video": model}, workflows=[result.get("workflow", getattr(self.comfy, "workflow_i2v", "wan21_i2v_14b_fp8") if req.mode == "image_to_video" else getattr(self.comfy, "workflow_t2v", "wan21_t2v_14b_fp8"))])},
        )
        self._advance_legacy_asset_stage(req.project_id, "video")
        return ref

    # --- audio -------------------------------------------------------
    async def generate_audio(self, req: GenerateAudioRequest) -> AssetRef:
        job_id = uuid.uuid4().hex[:10]
        engine = get_settings().tts.engine
        # Keep provenance truthful when the optional Edge provider is selected.
        model = "kokoro-82m" if engine == "kokoro" else "edge-tts"
        await self.router.acquire(model, job_id)
        try:
            wav = await self.tts.synthesize(req.text, voice=req.voice, speed=req.speed)
        finally:
            await self.router.release(model, job_id, unload=False)
        filename = req.save_as or f"audio_{uuid.uuid4().hex[:6]}.wav"
        ref = self.store.save_asset(
            req.project_id, AssetKind.audio, filename, wav,
            meta={"engine": get_settings().tts.engine, "voice": req.voice, "text": req.text[:200],
                  "provenance": asset_provenance(prompt_id="audio.synthesize", rendered_prompt=req.text,
                      models={"tts": model, "tts_engine": get_settings().tts.engine})},
        )
        self._advance_legacy_asset_stage(req.project_id, "audio")
        return ref

    def _advance_legacy_asset_stage(self, project_id: str, stage: str) -> None:
        """Preserve Phase 0/1 one-asset completion without corrupting plan flow.

        Plan assets become evidence only after their candidate review/selection
        (or after all scene audio is tagged), so individual provider calls must
        not try to complete the aggregate stage.
        """
        if not self.store.get(project_id).video_plan:
            self.store.update_stage(project_id, stage, "succeeded")

    # --- stage-plan artifacts ----------------------------------------
    async def generate_image_plan(self, req: GenerateImagePlanRequest) -> AssetRef:
        """Generate the Step-2 image-plan artifact (scene prompts, character lock,
        negatives) from the approved copy using the local model. Stored as an
        editable .md asset and registered as the project's image_plan artifact."""
        proj = self.store.get(req.project_id)
        model = req.model or self.lms.default_model
        job_id = uuid.uuid4().hex[:10]
        await self.router.acquire(model, job_id)
        try:
            # Build prompt from the approved copy + project brief
            copy_asset_id = proj.artifacts.get("copy_plan")
            copy_text = ""
            if copy_asset_id:
                try:
                    p = self.store.asset_path(req.project_id, copy_asset_id)
                    if p.exists():
                        copy_text = p.read_text(encoding="utf-8")
                except Exception:
                    copy_text = ""
            system = (
                "You are a cinematic film-director and image-prompt engineer. "
                "Produce a FLUX image-generation plan in clean Markdown."
            )
            prompt = (
                f"Create a Step-2 Image Plan for a 25-second vertical (9:16, 1080x1920) "
                f"social commercial. Project brief: {proj.brief or 'N/A'}.\n\n"
                f"Approved copy:\n{copy_text or '(none provided)'}\n\n"
                "Output EXACTLY this Markdown structure:\n"
                "1. A 'Character / style lock' section listing: a fixed supervisor "
                "description, a fixed new-employee description, style keywords, and a "
                "single reusable 'hard negative' prompt block (no text/watermark/logo).\n"
                "2. One section per scene named for the 5 commercial segments "
                "(Hook, Problem, Solution, Proof, CTA). For each give: a one-line visual, "
                "a 'save as' filename (img_scene{N}_{name}.png), and a full positive "
                "prompt block using 9:16 vertical cinematic language.\n"
                "3. A 'Book cover' note (upload real cover, not AI) and an optional "
                "'social thumbnail' prompt.\n"
                "Keep every positive prompt self-contained and character-consistent."
            )
            text = await self.lms.chat_text(
                prompt, model=model, system_prompt=system, temperature=0.5, max_tokens=3000
            )
        finally:
            await self.router.release(model, job_id, unload=False)
        filename = req.filename or f"image_plan_{uuid.uuid4().hex[:6]}.md"
        ref = self.store.save_asset(
            req.project_id, AssetKind.copy, filename, text,
            meta={"model": model, "kind": "image_plan", "stage": "images",
                  "provenance": asset_provenance(prompt_id="image_plan.generate", rendered_prompt=f"{system}\n{prompt}", models={"llm": model})},
        )
        self.store.set_artifact(req.project_id, "image_plan", ref.id)
        return ref

    async def save_artifact(self, req: SaveArtifactRequest) -> AssetRef:
        """Create or overwrite an editable stage-plan artifact with user text."""
        proj = self.store.get(req.project_id)
        existing = proj.artifacts.get(req.stage)
        if existing:
            # Overwrite the existing artifact file
            ref = self.store.get_asset(req.project_id, existing)
            path = self.store.asset_path(req.project_id, existing)
            path.write_text(req.content, encoding="utf-8")
            return ref
        filename = req.filename or f"{req.stage}_{uuid.uuid4().hex[:6]}.md"
        ref = self.store.save_asset(
            req.project_id, AssetKind.copy, filename, req.content,
            meta={"kind": req.stage, "stage": req.stage.split("_")[0],
                  "provenance": asset_provenance(prompt_id="asset.user_supplied", rendered_prompt=req.content)},
        )
        self.store.set_artifact(req.project_id, req.stage, ref.id)
        return ref

    # --- retired pre-Phase-0 pipeline (retained only for migration reference) ---
    async def _deprecated_run_project(self, project_id: str) -> dict[str, Any]:
        """Run the full pipeline in order (manual mode: stops for approval;
        auto mode: approves each stage and proceeds). Returns a report.

        Stages: copy -> images -> video -> audio -> final
        Each stage checks if already approved and skips; auto mode approves
        each stage after generation and continues to the next.
        """
        # Kept as a migration compatibility shim; it must preserve review gates.
        return await self.run_project(project_id)

        proj = self.store.get(project_id)
        report: dict[str, Any] = {"project_id": project_id, "steps": [], "mode": proj.mode}

        # Step 1: copy (only if copy not already approved)
        if proj.stages.get("copy") != "approved":
            if not proj.artifacts.get("copy_plan"):
                copy_req = GenerateCopyRequest(project_id=project_id, prompt=proj.brief or project_id)
                ref = await self.generate_copy(copy_req)
                self.store.set_artifact(project_id, "copy_plan", ref.id)
                report["steps"].append({"step": "copy", "status": "generated", "asset": ref.id})

        # Step 2: image plan + images
        if proj.stages.get("images") != "approved":
            if not proj.artifacts.get("image_plan"):
                plan_req = GenerateImagePlanRequest(project_id=project_id)
                ref = await self.generate_image_plan(plan_req)
                report["steps"].append({"step": "image_plan", "status": "generated", "asset": ref.id})

        if proj.mode == "manual":
            report["message"] = "Stopped after copy + image plan for approval."
            return report

        # Step 3: video (generate from each image asset)
        if proj.stages.get("video") != "approved":
            video_assets = [a for a in self.store.list_assets(project_id) if a.kind == AssetKind.image and a.filename.startswith("img_scene")]
            for img_asset in video_assets:
                scene_name = img_asset.filename.replace("img_", "").replace(".png", "").replace(".jpg", "")
                video_req = GenerateVideoRequest(
                    project_id=project_id,
                    mode="image_to_video",
                    source_image=img_asset.id,
                    prompt=f"Cinematic motion for {scene_name}. Subtle, natural movement.",
                    duration_s=5.0,
                    fps=16,
                    width=720,
                    height=1280,
                    save_as=f"video_{scene_name}.mp4",
                )
                ref = await self.generate_video(video_req)
                report["steps"].append({"step": "video", "status": "generated", "asset": ref.id, "scene": scene_name})

        # Step 4: audio (generate voiceover from copy)
        if proj.stages.get("audio") != "approved":
            copy_path = self.store.asset_path(project_id, proj.artifacts["copy_plan"])
            copy_text = copy_path.read_text(encoding="utf-8") if copy_path.exists() else ""
            # Extract the narration/voiceover portion (strip markdown headers)
            narration = "\n".join(l for l in copy_text.splitlines() if not l.startswith("#"))
            if narration.strip():
                audio_req = GenerateAudioRequest(
                    project_id=project_id,
                    text=narration[:5000],
                    voice=self.tts.default_voice,
                    save_as="voiceover_full.wav",
                )
                ref = await self.generate_audio(audio_req)
                report["steps"].append({"step": "audio", "status": "generated", "asset": ref.id})

        # Step 5: final assembly (concatenate videos + audio -> master)
        if proj.stages.get("final") != "approved":
            video_assets = [a for a in self.store.list_assets(project_id) if a.kind == AssetKind.video and not a.meta.get("upscaled")]
            audio_assets = [a for a in self.store.list_assets(project_id) if a.kind == AssetKind.audio]
            if video_assets:
                video_ids = [a.id for a in video_assets]
                audio_id = audio_assets[0].id if audio_assets else None
                assemble_req = AssembleRequest(
                    project_id=project_id,
                    video_asset_id=video_ids[0] if len(video_ids) == 1 else None,
                    audio_asset_id=audio_id,
                    output_name="final_master",
                )
                ref = await self.assemble(assemble_req)
                report["steps"].append({"step": "final", "status": "assembled", "asset": ref.id})

        report["status"] = "complete"
        return report

    # --- reliability-first pipeline ---------------------------------
    async def run_project(self, project_id: str) -> dict[str, Any]:
        """Advance one review-gated stage only when persisted evidence exists.

        ``auto`` retains automatic worker execution, but it no longer means an
        implicit approval. A human must explicitly approve each succeeded stage
        before downstream work can begin.
        """
        project = self.store.get(project_id)  # Always reload from disk; never use stale state.
        # This legacy runner consumes Markdown copy. A canonical plan contains
        # structured JSON and must only be run through the plan endpoints.
        if project.video_plan:
            return {
                "project_id": project_id,
                "mode": project.mode,
                "status": "plan_workflow_required",
                "stage": "copy",
                "message": "This project has a canonical VideoPlan; use plan candidates, plan audio, proxy approval, and plan master endpoints.",
                "steps": [],
                "stages": project.stages,
            }
        report: dict[str, Any] = {"project_id": project_id, "mode": project.mode, "steps": []}
        stages = ("copy", "images", "video", "audio", "final")

        def waiting(stage: str, message: str) -> dict[str, Any]:
            report.update(status="awaiting_approval", stage=stage, message=message,
                          stages=self.store.get(project_id).stages)
            return report

        # A succeeded stage is a hard review gate; failed stages are retried only
        # on a deliberate subsequent Run request.
        for stage in stages:
            current = self.store.get(project_id).stages[stage]
            if current == "approved":
                continue
            if current == "succeeded":
                evidence = self.store.stage_output_summary(project_id, stage)
                if evidence["ok"]:
                    return waiting(stage, f"{stage} outputs exist. Review and explicitly approve them to continue.")
                # A manifest is not evidence. Recover stale/forged success into
                # pending so a normal run can repair it instead of deadlocking.
                self.store.update_stage(project_id, stage, "pending")
                current = "pending"

            if stage == "copy":
                if self.store.stage_output_summary(project_id, "copy")["ok"]:
                    self.store.update_stage(project_id, "copy", "succeeded")
                    return waiting("copy", "Copy plan was recovered from persisted assets; approve it to continue.")
                self.store.update_stage(project_id, "copy", "running")
                try:
                    ref = await self.generate_copy(GenerateCopyRequest(project_id=project_id, prompt=project.brief or project_id))
                except Exception as exc:
                    self.store.update_stage(project_id, "copy", "failed")
                    report.update(status="failed", stage="copy", error=str(exc))
                    return report
                report["steps"].append({"step": "copy", "status": "succeeded", "asset": ref.id})
                return waiting("copy", "Copy was generated. Review and explicitly approve it to continue.")

            if stage == "images":
                # An image plan helps the operator create images but is not treated
                # as image evidence and therefore cannot advance this stage.
                if not self.store.get(project_id).artifacts.get("image_plan"):
                    try:
                        plan = await self.generate_image_plan(GenerateImagePlanRequest(project_id=project_id))
                        report["steps"].append({"step": "image_plan", "status": "succeeded", "asset": plan.id})
                    except Exception as exc:
                        self.store.update_stage(project_id, "images", "failed")
                        report.update(status="failed", stage="images", error=str(exc))
                        return report
                evidence = self.store.stage_output_summary(project_id, "images")
                if not evidence["ok"]:
                    report.update(status="blocked", stage="images", message="Image plan is ready; generate at least one image before approval.", stages=self.store.get(project_id).stages)
                    return report
                self.store.update_stage(project_id, "images", "succeeded")
                return waiting("images", "Image assets exist. Review and explicitly approve them to continue.")

            if stage == "video":
                images = self.store.list_assets(project_id, AssetKind.image)
                if not images:
                    report.update(status="blocked", stage="video", message="No image assets are available for image-to-video generation.")
                    return report
                self.store.update_stage(project_id, "video", "running")
                try:
                    for image in images:
                        scene = Path(image.filename).stem
                        ref = await self.generate_video(GenerateVideoRequest(
                            project_id=project_id, mode="image_to_video", source_image=image.id,
                            prompt=f"Cinematic motion for {scene}. Subtle, natural movement.", duration_s=5.0,
                            fps=16, width=720, height=1280, save_as=f"video_{scene}.mp4"))
                        report["steps"].append({"step": "video", "status": "succeeded", "asset": ref.id, "scene": scene})
                except Exception as exc:
                    self.store.update_stage(project_id, "video", "failed")
                    report.update(status="failed", stage="video", error=str(exc))
                    return report
                if self.store.stage_output_summary(project_id, "video")["ok"]:
                    self.store.update_stage(project_id, "video", "succeeded")
                    return waiting("video", "Video outputs exist. Review and explicitly approve them to continue.")
                self.store.update_stage(project_id, "video", "failed")
                report.update(status="failed", stage="video", error="Generation returned without a persisted video asset")
                return report

            if stage == "audio":
                copy_id = self.store.get(project_id).artifacts.get("copy_plan")
                copy_text = self.store.asset_path(project_id, copy_id).read_text(encoding="utf-8") if copy_id else ""
                narration = "\n".join(line for line in copy_text.splitlines() if not line.startswith("#")).strip()
                if not narration:
                    report.update(status="blocked", stage="audio", message="Approved copy contains no narration text.")
                    return report
                self.store.update_stage(project_id, "audio", "running")
                try:
                    ref = await self.generate_audio(GenerateAudioRequest(project_id=project_id, text=narration[:5000], voice=self.tts.default_voice, save_as="voiceover_full.wav"))
                except Exception as exc:
                    self.store.update_stage(project_id, "audio", "failed")
                    report.update(status="failed", stage="audio", error=str(exc))
                    return report
                report["steps"].append({"step": "audio", "status": "succeeded", "asset": ref.id})
                return waiting("audio", "Audio output exists. Review and explicitly approve it to continue.")

            if stage == "final":
                videos = self.store.list_assets(project_id, AssetKind.video)
                audio = self.store.list_assets(project_id, AssetKind.audio)
                if not videos:
                    report.update(status="blocked", stage="final", message="No video asset is available for assembly.")
                    return report
                self.store.update_stage(project_id, "final", "running")
                try:
                    saved = self.store.get(project_id).composition
                    if saved:
                        # Saved compositions are validated/normalized when
                        # persisted and are the authoritative final EDL.
                        ref = await self.assemble_composition(project_id, Composition(**saved), output_name="final_master")
                    else:
                        ref = await self.assemble(AssembleRequest(project_id=project_id, video_asset_id=videos[0].id,
                                                                   audio_asset_id=audio[0].id if audio else None,
                                                                   output_name="final_master"))
                except Exception as exc:
                    self.store.update_stage(project_id, "final", "failed")
                    report.update(status="failed", stage="final", error=str(exc))
                    return report
                report["steps"].append({"step": "final", "status": "succeeded", "asset": ref.id})
                return waiting("final", "Final master exists. Review and explicitly approve delivery.")

        report.update(status="complete", stages=self.store.get(project_id).stages)
        return report

    # --- upscale -----------------------------------------------------
    async def upscale_video(self, req: UpscaleVideoRequest) -> AssetRef:
        """Upscale a video asset using Real-ESRGAN via ComfyUI."""
        src_path = self.store.asset_path(req.project_id, req.video_asset_id)
        video_bytes = src_path.read_bytes()
        # Determine fps from asset meta or default
        asset = self.store.get_asset(req.project_id, req.video_asset_id)
        fps = 16
        if asset and asset.meta and "fps" in asset.meta:
            fps = asset.meta["fps"]
        result = await self.comfy.upscale_video(
            video_bytes,
            fps=fps,
            model_name=req.model_name,
            target_size=req.target_size,
        )
        filename = req.save_as or f"upscaled_{uuid.uuid4().hex[:6]}.mp4"
        return self.store.save_asset(
            req.project_id, AssetKind.video, filename, result["bytes"],
            meta={
                "model": "real-esrgan",
                "esrgan_model": req.model_name,
                "target_size": req.target_size,
                "source_asset": req.video_asset_id,
                "upscaled": True,
                "provenance": asset_provenance(prompt_id="video.upscale", models={"upscale": "real-esrgan"},
                    workflows=[getattr(self.comfy, "workflow_upscale", "")]),
            },
        )

    # --- assembly ----------------------------------------------------
    def _final_output_path(self, project_id: str, output_name: str) -> Path:
        """Build a contained final destination after schema basename validation."""
        out_dir = self.store.asset_dir(project_id, AssetKind.final).resolve()
        out_path = (out_dir / f"{output_name}.mp4").resolve()
        if out_path.parent != out_dir:
            raise ValueError("output path escapes the project's final directory")
        return out_path

    def _begin_final_render(self, project_id: str, out_path: Path) -> None:
        """Invalidate old evidence before starting a replacement render.

        The old bytes are intentionally not deleted: FFmpeg renders a sibling
        temporary file and atomically promotes only after QC.  Removing the old
        registry evidence first means a failed replacement can never leave an
        old final approvable under a misleading succeeded stage.
        """
        project = self.store.get(project_id)
        if project.stages.get("final") == "approved":
            raise ValueError("approved final masters are immutable; reset through an explicit new delivery workflow")
        # Invalidate every prior final asset, not merely a same-named output:
        # otherwise a failed render to a new name leaves the older master as
        # approvable evidence for this delivery stage.
        self.store.unregister_final_output(project_id)
        self.store.update_stage(project_id, "final", "running")

    def _fail_final_render(self, project_id: str, out_path: Path) -> None:
        # Remove any accidental registration for this exact output, but leave
        # any prior on-disk bytes untouched because they were never promoted.
        self.store.unregister_final_output(project_id, out_path.name)
        try:
            self.store.update_stage(project_id, "final", "failed")
        except ValueError:
            log.exception("could not mark final stage failed for %s", project_id)

    def _register_final(self, project_id: str, out_path: Path, meta: dict[str, Any]) -> AssetRef:
        """Register only the exact existing output that has passed QC."""
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise RuntimeError("refusing to register a missing or empty final output")
        qc = dict(self.ffmpeg.qc_metadata(out_path))
        if not isinstance(qc, dict) or qc.get("passed") is not True:
            raise RuntimeError("refusing to register a master that did not pass ffprobe/loudness quality gates")
        master_sha256 = self._sha256_path(out_path)
        qc["media_sha256"] = master_sha256
        ref = self.store.save_asset(project_id, AssetKind.final, out_path.name, None, meta={
            **meta, "master_sha256": master_sha256, "qc": qc, "provenance": asset_provenance(
                prompt_id="render.composition", include_render_profile=True
            )
        })
        self.store.update_stage(project_id, "final", "succeeded")
        return ref

    async def assemble(self, req: AssembleRequest) -> AssetRef:
        """Render the legacy single-video request without weakening final QC."""
        video_path = self.store.asset_path(req.project_id, req.video_asset_id) if req.video_asset_id else None
        audio_path = self.store.asset_path(req.project_id, req.audio_asset_id) if req.audio_asset_id else None
        music_path = self.store.asset_path(req.project_id, req.music_asset_id) if req.music_asset_id else None
        out_dir = self.store.asset_dir(req.project_id, AssetKind.final).resolve()
        out_path = self._final_output_path(req.project_id, req.output_name)
        self._begin_final_render(req.project_id, out_path)
        sub_path: Path | None = None
        try:
            if req.subtitle_text:
                if video_path is None: raise ValueError("subtitle_text requires a visual video asset")
                visual_duration_s = await self.ffmpeg.probe_duration(video_path)
                # The schema has collapsed all control/newline input. HTML escape
                # markup too, so this is literal bounded VTT cue content, not VTT.
                total_ms = max(1, int(round(visual_duration_s * 1000)))
                hours, remainder = divmod(total_ms, 3_600_000); minutes, remainder = divmod(remainder, 60_000)
                seconds, milliseconds = divmod(remainder, 1000)
                cue_end = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
                sub_path = out_dir / f".{req.output_name}.{uuid.uuid4().hex}.vtt"
                sub_path.write_text(f"WEBVTT\n\n00:00:00.000 --> {cue_end}\n{html.escape(req.subtitle_text)}\n", encoding="utf-8")
            await self.ffmpeg.assemble(video_path=video_path, audio_path=audio_path, music_path=music_path,
                                       subtitles_path=sub_path, output_path=out_path)
            return self._register_final(req.project_id, out_path,
                                        {"legacy_assembly": True, "video": req.video_asset_id,
                                         "audio": req.audio_asset_id, "music": req.music_asset_id})
        except Exception:
            self._fail_final_render(req.project_id, out_path)
            raise
        finally:
            if sub_path: sub_path.unlink(missing_ok=True)

    # --- composition assembly ----------------------------------------
    async def assemble_composition(self, project_id: str, comp: Composition, output_name: str = "final", *,
                                   final_meta: dict[str, Any] | None = None) -> AssetRef:
        """Assemble the final master from a timeline composition.

        ``final_meta`` is supplied before registration so plan-specific evidence
        is visible to the low-level final-stage state gate.
        """
        video_paths = []
        for clip in comp.video_clips:
            video_paths.append({
                "path": str(self.store.asset_path(project_id, clip.asset_id)),
                "start_s": clip.start_s,
                "end_s": clip.end_s,
                "trim_start_s": clip.trim_start_s,
            })
        audio_paths = []
        for clip in comp.audio_clips:
            audio_paths.append({
                "path": str(self.store.asset_path(project_id, clip.asset_id)),
                "start_s": clip.start_s,
                "end_s": clip.end_s,
                "trim_start_s": clip.trim_start_s,
                "volume": clip.volume,
            })
        out_path = self._final_output_path(project_id, output_name)
        self._begin_final_render(project_id, out_path)
        try:
            sub_dicts = [s.model_dump() for s in comp.subtitles]
            await self.ffmpeg.render_composition(video_paths=video_paths, audio_paths=audio_paths, subtitles=sub_dicts,
                                                 total_duration_s=comp.total_duration_s, output_path=out_path)
            return self._register_final(project_id, out_path, {
                "composition": True, "output_name": output_name,
                "normalized_composition": comp.model_dump(mode="json"),
                **(final_meta or {}),
            })
        except Exception:
            self._fail_final_render(project_id, out_path)
            raise

    async def assemble_saved_composition(self, project_id: str, output_name: str = "final") -> AssetRef:
        project = self.store.get(project_id)
        if not project.composition:
            raise ValueError("project has no saved composition")
        return await self.assemble_composition(project_id, Composition(**project.composition), output_name)

    # --- pipeline status ---------------------------------------------
    async def status(self) -> dict[str, Any]:
        return {
            "lmstudio": await self.lms.status(),
            "comfyui": await self.comfy.status(),
            "tts": await self.tts.status(),
            "ffmpeg": await self.ffmpeg.status(),
            "vram": self.router.status(),
        }


_orch: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch