"""Phase-2 canonical-plan regressions using real orchestration and state gates.

Providers/FFmpeg are deterministic fakes, but candidate generation, TTS,
proxy approval, composition assembly, final registration, and every relevant
ProjectStore stage transition execute through the production methods.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from pydantic import ValidationError


def run(coro):
    return asyncio.run(coro)


def valid_plan():
    return {
        "platform": "tiktok", "target_duration_s": 6, "hook": "Stop scrolling", "cta": "Try it today",
        "scenes": [
            {"id": "hook", "start_s": 0, "end_s": 3, "voiceover": "Stop scrolling. This makes planning easier.", "caption": "Stop scrolling. This makes planning easier.", "visual_prompt": "Vertical cinematic product reveal with empty lower safe area.", "motion_prompt": "Slow camera push with subtle natural motion."},
            {"id": "cta", "start_s": 3, "end_s": 6, "voiceover": "Try it today.", "caption": "Try it today.", "visual_prompt": "Vertical confident call to action with clean safe background.", "motion_prompt": "Gentle camera settle and confident movement."},
        ],
    }


class FakeRouter:
    async def acquire(self, *args, **kwargs):
        return None

    async def release(self, *args, **kwargs):
        return None


class FakeComfy:
    async def generate_image(self, *args, **kwargs):
        return {"bytes": b"deterministic-image"}

    async def generate_video(self, *args, **kwargs):
        return {"bytes": b"deterministic-video", "duration_s": kwargs["duration_s"], "frames": 48}


class FakeTTS:
    default_voice = "synthetic"

    async def synthesize(self, text, **kwargs):
        return f"audio:{text}".encode()


class FakeFFmpeg:
    async def render_composition(self, **kwargs):
        Path(kwargs["output_path"]).write_bytes(b"deterministic-render")

    def qc_metadata(self, path):
        assert Path(path).is_file()
        return {"passed": True, "duration_s": 6, "profile": {"video_codec": "h264"}}


@pytest.fixture()
def service(tmp_path: Path, monkeypatch):
    from app.config.settings import get_settings
    from app.config import settings as settings_module
    from app.services import project_store as ps
    from app.services.orchestrator import Orchestrator
    from app.models.schemas import ProjectCreate

    monkeypatch.setattr(settings_module, "_cached", dataclasses.replace(get_settings(), projects_root=tmp_path))
    ps._store = None
    store = ps.get_store()
    project = store.create(ProjectCreate(name="P", brief="B"))
    # Do not replace any orchestration method under test. Only external workers
    # are deterministic fakes; real generate_*, assembly, registration, and
    # update_stage paths run below.
    orch = Orchestrator.__new__(Orchestrator)
    orch.store, orch.router = store, FakeRouter()
    orch.comfy, orch.tts, orch.ffmpeg = FakeComfy(), FakeTTS(), FakeFFmpeg()
    from app.services.production_plan import ProductionPlanService
    orch.production = ProductionPlanService(store, orch)
    return orch, project.id


def select_all(orch, candidates, kind):
    from app.models.schemas import ReviewCandidateRequest
    for scene in ("hook", "cta"):
        candidate = next(item for item in candidates if item.scene_id == scene)
        reviewed = orch.review_candidate(
            candidate.project_id, candidate.id,
            ReviewCandidateRequest(status="accepted", select=True, reason="Safe framing"),
        )
        assert reviewed.kind == kind and reviewed.selected


def approve(store, pid, stage):
    assert store.get(pid).stages[stage] == "succeeded"
    store.public_update_stage(pid, stage, "approved")


def test_video_plan_enforces_platform_timing_budget_hook_cta_and_contiguous_scenes():
    from app.models.schemas import VideoPlan
    plan = VideoPlan(**valid_plan())
    assert [x.id for x in plan.scenes] == ["hook", "cta"]
    bad = valid_plan(); bad["scenes"][1]["start_s"] = 3.2
    with pytest.raises(ValidationError, match="contiguous"):
        VideoPlan(**bad)
    bad = valid_plan(); bad["scenes"][0]["voiceover"] = "Different opening"
    with pytest.raises(ValidationError, match="declared hook"):
        VideoPlan(**bad)


def test_plan_pipeline_uses_real_low_level_generation_stage_gates_proxy_and_master_registration(service):
    """Regression for premature low-level stage updates and late final metadata."""
    from app.models.schemas import GeneratePlanCandidatesRequest, PlanCandidateKind, VideoPlan

    orch, pid = service
    orch.save_video_plan(pid, VideoPlan(**valid_plan()))
    store = orch.store
    # Structured state + fingerprinted production artifact are the only copy evidence.
    assert store.stage_output_summary(pid, "copy")["ok"]
    approve(store, pid, "copy")

    images = run(orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=pid, kind="image")))
    assert len(images) == 4
    # Real generate_image saved every asset but did not falsely complete aggregate evidence.
    assert store.get(pid).stages["images"] == "pending"
    select_all(orch, images, PlanCandidateKind.image)
    approve(store, pid, "images")

    videos = run(orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=pid, kind="video")))
    assert len(videos) == 2
    assert all(store.asset_path(pid, item.reference_asset_id).is_file() for item in videos)
    select_all(orch, videos, PlanCandidateKind.video)
    approve(store, pid, "video")

    audio = run(orch.generate_plan_audio(pid))
    assert len(audio) == 2
    assert store.get(pid).stages["audio"] == "succeeded"
    approve(store, pid, "audio")

    preview = run(orch.render_plan_preview(pid))
    approval = orch.approve_plan_preview(pid, preview.id)
    assert approval["preview_asset_id"] == preview.id
    master = run(orch.render_plan_master(pid))
    assert master.kind.value == "final"
    assert master.meta["qc"]["passed"] is True
    assert master.meta["plan_fingerprint"] == store.get(pid).video_plan["fingerprint"]
    # This is the real low-level _register_final -> update_stage path.
    assert store.get(pid).stages["final"] == "succeeded"
    assert store.stage_output_summary(pid, "final")["ok"]


def test_copy_gate_rejects_legacy_video_plan_markdown_and_validates_plan_artifact(service):
    from app.models.schemas import SaveArtifactRequest, VideoPlan

    orch, pid = service
    legacy = run(orch.save_artifact(SaveArtifactRequest(project_id=pid, stage="video_plan", content="# arbitrary markdown")))
    assert legacy.kind.value == "copy"
    assert not orch.store.stage_output_summary(pid, "copy")["ok"]
    with pytest.raises(ValueError, match="Cannot mark 'copy' succeeded"):
        orch.store.update_stage(pid, "copy", "succeeded")

    orch.save_video_plan(pid, VideoPlan(**valid_plan()))
    assert orch.store.get(pid).artifacts["video_plan"] == legacy.id
    assert orch.store.get(pid).artifacts["production_plan"] != legacy.id
    assert orch.store.stage_output_summary(pid, "copy")["ok"]
    plan_asset = orch.store.get(pid).artifacts["production_plan"]
    orch.store.asset_path(pid, plan_asset).write_text(json.dumps({"not": "a plan"}), encoding="utf-8")
    assert not orch.store.stage_output_summary(pid, "copy")["ok"]


def test_plan_runner_rejects_legacy_markdown_narration_path(service):
    from app.models.schemas import VideoPlan

    orch, pid = service
    orch.save_video_plan(pid, VideoPlan(**valid_plan()))
    report = run(orch.run_project(pid))
    assert report["status"] == "plan_workflow_required"
    assert not report["steps"]
    assert orch.store.list_assets(pid, kind="audio") == []


def test_candidate_deserialization_lineage_and_preview_rescan_are_robust(service):
    from app.models.schemas import AssetKind, GeneratePlanCandidatesRequest, VideoPlan

    orch, pid = service
    orch.save_video_plan(pid, VideoPlan(**valid_plan()))
    candidates_file = orch.store._candidates_path(orch.store._dir(pid))
    candidates_file.write_text(json.dumps([{"bad": True}, "not-a-row"]), encoding="utf-8")
    assert orch.store.list_candidates(pid) == [{"bad": True}]
    assert orch.production._candidate_records(pid, orch.store.get(pid).video_plan["fingerprint"]) == []

    # A selected image whose bytes disappeared can never be used as I2V lineage.
    images = run(orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=pid, scene_ids=["hook"], kind="image")))
    select_all_hook = next(item for item in images if item.scene_id == "hook")
    from app.models.schemas import ReviewCandidateRequest
    orch.review_candidate(pid, select_all_hook.id, ReviewCandidateRequest(status="accepted", select=True, reason="works"))
    orch.store.asset_path(pid, select_all_hook.asset_id).unlink()
    with pytest.raises(ValueError, match="existing reviewed, selected image"):
        run(orch.generate_plan_candidates(GeneratePlanCandidatesRequest(project_id=pid, scene_ids=["hook"], kind="video")))

    preview_dir = orch.store.asset_dir(pid, AssetKind.preview)
    (preview_dir / "unregistered.mp4").write_bytes(b"proxy")
    result = orch.store.rescan_assets(pid)
    assert "unregistered.mp4" in result["added"]
    assert any(asset.kind == AssetKind.preview for asset in orch.store.list_assets(pid))
