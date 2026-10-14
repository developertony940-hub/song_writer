"""FastAPI contract tests for public validation and review transitions."""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.config.settings import get_settings
    from app.config import settings as settings_module
    from app.services import project_store as ps
    from app.services import orchestrator as orch_module
    from app.services.model_router import reset_router

    monkeypatch.setattr(settings_module, "_cached", dataclasses.replace(get_settings(), projects_root=tmp_path))
    ps._store = None
    orch_module._orch = None
    reset_router()
    from app.main import create_app
    with TestClient(create_app()) as result:
        yield result


def make_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "Route test", "brief": "brief"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_public_stage_route_rejects_forged_worker_status_and_requires_evidence(client: TestClient):
    pid = make_project(client)
    forged = client.patch(f"/api/projects/{pid}/stage", json={"stage": "copy", "status": "succeeded"})
    assert forged.status_code == 422
    missing = client.patch(f"/api/projects/{pid}/stage", json={"stage": "copy", "status": "approved"})
    assert missing.status_code == 409

    from app.models.schemas import AssetKind
    from app.services.project_store import get_store
    store = get_store()
    ref = store.save_asset(pid, AssetKind.copy, "plan.md", "approved copy")
    store.set_artifact(pid, "copy_plan", ref.id)
    store.update_stage(pid, "copy", "succeeded")
    approved = client.patch(f"/api/projects/{pid}/stage", json={"stage": "copy", "status": "approved"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["stages"]["copy"] == "approved"


def test_output_name_is_rejected_before_assembly_and_composition_can_be_saved(client: TestClient):
    pid = make_project(client)
    traversal = client.post("/api/generate/assemble", json={"project_id": pid, "output_name": "../escape"})
    assert traversal.status_code == 422
    traversal = client.post("/api/generate/assemble-composition", json={
        "project_id": pid, "output_name": "a/b", "composition": {"video_clips": []},
    })
    assert traversal.status_code == 422

    saved = client.put(f"/api/projects/{pid}/composition", json={
        "composition": {"total_duration_s": 4, "video_clips": [], "audio_clips": []},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["composition"]["total_duration_s"] == 4


def test_composition_save_normalizes_frame_times_and_rejects_out_of_range_intervals(client: TestClient):
    pid = make_project(client)
    saved = client.put(f"/api/projects/{pid}/composition", json={
        "composition": {
            "total_duration_s": 1.01,  # quantizes to exactly 30 frames
            "video_clips": [{"asset_id": "unused", "start_s": 0.017, "end_s": 0.984}],
            "audio_clips": [],
            "subtitles": [{"start_s": 0.017, "end_s": 0.984, "text": "Frame-normalized caption"}],
        },
    })
    assert saved.status_code == 200, saved.text
    comp = saved.json()["composition"]
    assert comp["total_duration_s"] == pytest.approx(1.0)
    assert comp["video_clips"][0]["start_s"] == pytest.approx(1 / 30)
    assert comp["video_clips"][0]["end_s"] == pytest.approx(1.0)
    assert comp["subtitles"][0]["end_s"] == pytest.approx(1.0)

    invalid = client.put(f"/api/projects/{pid}/composition", json={
        "composition": {"total_duration_s": 1, "video_clips": [], "audio_clips": [],
                        "subtitles": [{"start_s": 0, "end_s": 1.1, "text": "Outside edit"}]},
    })
    assert invalid.status_code == 422


def test_final_evidence_requires_existing_exact_file_and_passed_qc(client: TestClient):
    pid = make_project(client)
    from app.models.schemas import AssetKind
    from app.services.project_store import get_store

    store = get_store()
    # A final that does not include explicit QC cannot become review evidence.
    unverified = store.save_asset(pid, AssetKind.final, "unverified.mp4", b"not-a-master", meta={})
    assert store.stage_output_summary(pid, "final")["ok"] is False
    # QC approval is tied to the registered file. Removing it invalidates the
    # evidence even though the manifest/asset record itself remains.
    verified = store.save_asset(pid, AssetKind.final, "verified.mp4", b"passed-by-test", meta={"qc": {"passed": True}})
    assert store.stage_output_summary(pid, "final")["ok"] is True
    store.asset_path(pid, verified.id).unlink()
    assert store.stage_output_summary(pid, "final")["ok"] is False
    # Starting a differently named replacement invalidates all historic final
    # evidence, so a failed re-render cannot leave a stale approvable master.
    fresh = store.save_asset(pid, AssetKind.final, "old-name.mp4", b"old-but-qc", meta={"qc": {"passed": True}})
    from app.services.orchestrator import Orchestrator
    new_output = store.asset_dir(pid, AssetKind.final) / "replacement.mp4"
    Orchestrator()._begin_final_render(pid, new_output)
    assert store.stage_output_summary(pid, "final")["ok"] is False
    assert all(asset.id != fresh.id for asset in store.list_assets(pid))
    assert unverified.id != verified.id


def test_run_project_repairs_stale_succeeded_manifest_evidence(client: TestClient):
    pid = make_project(client)
    from app.services.project_store import get_store
    store = get_store()
    # Simulate a legacy/corrupt manifest instead of using the guarded public API.
    meta = store.get(pid)
    meta.stages["copy"] = "succeeded"
    store._write_manifest(store._dir(pid), meta)

    # Avoid a real LLM: this test is specifically the persisted-evidence recheck.
    from app.services.orchestrator import get_orchestrator
    orchestrator = get_orchestrator()
    async def fake_copy(req):
        from app.models.schemas import AssetKind
        ref = store.save_asset(pid, AssetKind.copy, "recovered.md", "copy")
        store.set_artifact(pid, "copy_plan", ref.id)
        store.update_stage(pid, "copy", "succeeded")
        return ref
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(orchestrator, "generate_copy", fake_copy)
    try:
        response = client.post("/api/generate/run-project", json={"project_id": pid})
    finally:
        monkeypatch.undo()
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_approval"
    assert store.get(pid).stages["copy"] == "succeeded"


def test_assemble_route_bounds_generated_subtitle_to_visual_duration(client: TestClient):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")

    pid = make_project(client)
    from app.models.schemas import AssetKind
    from app.services.orchestrator import get_orchestrator
    from app.services.project_store import get_store

    store = get_store()
    source = store.asset_dir(pid, AssetKind.video) / "short.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=160x320:r=15:d=1.2", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-y", str(source),
    ], check=True)
    video = store.save_asset(pid, AssetKind.video, source.name, None)

    runner = get_orchestrator().ffmpeg
    runner.binary, runner.ffprobe_binary = "ffmpeg", "ffprobe"
    runner.sm_width, runner.sm_height, runner.sm_fps = 160, 320, 15
    runner.sm_vbitrate, runner.sm_abitrate = "300k", "64k"

    response = client.post("/api/generate/assemble", json={
        "project_id": pid,
        "video_asset_id": video.id,
        "subtitle_text": "A subtitle that must end with the visual master.",
        "output_name": "subtitle-bounded",
    })
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["qc"]["passed"] is True
    assert response.json()["meta"]["qc"]["probe"]["streams"]
    output = store.asset_path(pid, response.json()["id"])
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(output),
    ], check=True, capture_output=True, text=True)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    assert 1.1 <= duration <= 1.35, duration
