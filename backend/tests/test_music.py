"""Synthetic tests for the bounded ACE-Step music workflow.

These tests prove request/state/authorization/provenance/cancellation wiring.
They do not submit a live ComfyUI prompt and do not establish live audio quality.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import re
import shutil
import wave
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings as settings_module
from app.config.settings import get_settings
from app.main import create_app
from app.models.music import (
    MusicCritiqueRequest,
    MusicCritiqueSuggestion,
    MusicSettings,
    MusicVariantPlanItem,
    MusicVariantRequest,
)
from app.models.profiles import ArtistProfileCreateRequest
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import orchestrator as orch_module
from app.services import project_store as ps
from app.services import take_export_service as take_export_module
from app.services.comfyui_client import ComfyUIClient, ComfyUIError, ComfyUICancelled
from app.services.model_router import ModelRouter, ModelSwitchRequired, Slot, reset_router
from app.services.profile_service import ProfileService
from app.services.profile_store import ProfileStore
from app.services.take_export_service import TakeExportService
from app.services.music_service import (
    MusicApprovalRequired,
    MusicCapReached,
    MusicConflict,
    MusicGenerationFailed,
    MusicNotAuthorized,
    MusicRecoveryRequired,
    MusicService,
)


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 1200)
    return buffer.getvalue()


class FakeRouter:
    def __init__(self) -> None:
        self.acquire_calls: list[tuple[str, str, bool]] = []
        self.release_calls: list[tuple[str, str, bool]] = []
        self._holds: dict[str, dict] = {}
        self.current_model: str | None = None

    async def acquire(self, model: str, job_id: str, *, allow_unload: bool = True) -> None:
        self.acquire_calls.append((model, job_id, allow_unload))
        self.current_model = model

    async def release(self, model: str, job_id: str, unload: bool = False) -> None:
        self.release_calls.append((model, job_id, unload))

    def status(self) -> dict:
        return {
            "slots": {"1": {"current_model": self.current_model}},
            "holds": dict(self._holds),
        }

    def hold(self, model: str, job_id: str, *, reason: str, holder: str | None = None) -> dict:
        record = {"slot": 1, "model": model, "job_id": job_id, "reason": reason, "holder": holder}
        self._holds["1"] = record
        return record

    def clear_hold(self, model: str, *, holder: str | None = None) -> bool:
        return self._holds.pop("1", None) is not None

    def held(self, model: str) -> dict | None:
        return self._holds.get("1")


class FakeComfy:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict]] = []
        self.cancelled: list[str] = []
        self.block = False
        self.submit_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.journal_path: Path | None = None

    async def status(self) -> dict:
        return {"online": True, "base_url": "fake", "gpus": []}

    async def object_info(self) -> dict:
        return {}

    async def submit_music(self, workflow: str, patches: dict) -> str:
        if self.journal_path is not None:
            rows = [
                json.loads(line)
                for line in self.journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert any(row.get("kind") == "attempt_started" for row in rows)
        self.submissions.append((workflow, patches))
        if self.submit_error is not None:
            raise self.submit_error
        return f"prompt-{len(self.submissions)}"

    async def wait_music(self, prompt_id: str, *, should_cancel=None, cancel_timeout_s: float = 10.0) -> dict:
        if self.block:
            while True:
                if should_cancel is not None and should_cancel():
                    raise ComfyUICancelled("cancelled")
                await asyncio.sleep(0.01)
        if self.wait_error is not None:
            raise self.wait_error
        if should_cancel is not None and should_cancel():
            raise ComfyUICancelled("cancelled")
        return {"outputs": {"8": {"audio": [{"filename": "fake.wav", "subfolder": "", "type": "output"}]}}}

    async def fetch_music(self, result: dict, *, prompt_id: str = "") -> dict:
        if self.fetch_error is not None:
            raise self.fetch_error
        return {"filename": "fake.wav", "bytes": _wav_bytes(), "prompt_id": prompt_id}

    async def cancel_prompt(self, prompt_id: str) -> dict:
        self.cancelled.append(prompt_id)
        return {"interrupt_status": 200, "queue_status": 200}


class GraphFakeComfy(FakeComfy):
    def __init__(self) -> None:
        super().__init__()
        self.graph_submissions: list[tuple[dict, str | None]] = []

    async def submit_music_graph(self, workflow: dict, client_id: str | None = None) -> str:
        self.graph_submissions.append((workflow, client_id))
        if self.submit_error is not None:
            raise self.submit_error
        return "graph-prompt-1"

    async def submit_music(self, workflow: str, patches: dict) -> str:
        raise AssertionError("resolved graph path must be used when available")


class BodyFakeComfy(FakeComfy):
    def __init__(self) -> None:
        super().__init__()
        self.body_submissions: list[tuple[bytes, str | None]] = []
        self.history_queries: list[str] = []

    async def system_stats(self) -> dict:
        return {
            "system": {
                "comfyui_version": "test-0.0.1",
                "comfy_package_versions": [{"name": "test-package", "installed": "1"}],
                "python_version": "test-python",
                "pytorch_version": "test-torch",
                "argv": ["test"],
            },
            "devices": [{"name": "test-cuda", "index": 0}],
        }

    async def submit_prompt_body(self, body: bytes, client_id: str | None = None) -> dict:
        self.body_submissions.append((body, client_id))
        if self.submit_error is not None:
            raise self.submit_error
        payload = json.dumps({"prompt_id": "body-prompt-1"}).encode()
        return {
            "status_code": 200,
            "headers": {"date": "test-server-date", "content-type": "application/json"},
            "body": payload,
            "json": {"prompt_id": "body-prompt-1"},
            "prompt_id": "body-prompt-1",
            "observed_at": "test-client-date",
            "client_id": client_id,
        }

    async def history(self, prompt_id: str) -> dict:
        self.history_queries.append(prompt_id)
        return {"outputs": {"8": {"audio": [{"filename": "fake.wav"}]}},
                "status": {"status_str": "success", "completed": True}}


def _ready_runtime():
    async def runtime() -> dict:
        live = get_settings().music.live_generation_authorized
        return {
            "ready": True,
            "offline_ready": True,
            "capacity_verified": True,
            "capacity_override_authorized": False,
            "capacity_ready": True,
            "capacity_label": "verified",
            "live_ready": live,
            "errors": [],
            "live_generation_authorized": live,
            "comfyui_online": True,
            "checkpoint_available": True,
            "workflow_valid": True,
        }
    return runtime


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool) -> None:
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(
            base,
            projects_root=tmp_path,
            music=dataclasses.replace(base.music, live_generation_authorized=live),
        ),
    )
    ps._store = None
    orch_module._orch = None
    music_module._service = None
    reset_router()


def _service(fake: FakeComfy | None = None, router: FakeRouter | None = None) -> MusicService:
    service = MusicService(comfy=fake or FakeComfy(), router=router or FakeRouter())
    service.runtime = _ready_runtime()
    return service


def _settings() -> MusicSettings:
    return MusicSettings(
        musical_direction="Warm acoustic pop with intimate female vocal",
        lyrics="[verse]\nA quiet room, a borrowed light,\nA melody that learns to write.\n",
        duration_s=12.0,
        language="en",
        bpm=100,
        time_signature="4",
        key_scale="C major",
        generate_audio_codes=True,
        seed=7,
    )


def _project(service: MusicService) -> str:
    return service.store.create(ProjectCreate(name="music-test", brief="synthetic")).id


def test_live_hold_refuses_before_comfy_submission(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "test approval", 1)

    with pytest.raises(MusicNotAuthorized):
        asyncio.run(service.generate(pid))
    assert fake.submissions == []


def test_successful_generation_provenance_cap_and_audio_approval(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved lyrics", 1)

    status = asyncio.run(service.generate(pid))
    assert status["current_attempt"]["status"] == "completed"
    assert len(fake.submissions) == 1
    assert service.router.acquire_calls[-1] == ("ace-step-1.5", service.router.acquire_calls[-1][1], False)
    assert status["journal"]["submissions"] == 1
    assert status["journal"]["completed"] == 1
    assert len(status["assets"]) == 1
    asset = status["assets"][0]
    provenance = asset["meta"]["provenance"]
    assert provenance["models"]["checkpoint"] == "ace_step_1.5_turbo_aio.safetensors"
    assert provenance["workflows"][0]["name"] == "ace_step_1_5_aio"
    assert asset["meta"]["music"]["lyrics_sha256"]
    assert asset["meta"]["music"]["settings_sha256"]
    started = [row for row in service._read_journal(pid)
               if row["kind"] == "attempt_started"][0]
    completed = [row for row in service._read_journal(pid)
                 if row["kind"] == "attempt_completed"][0]
    assert started["api_request_retained"] is True
    request_path = service.store.root / pid / started["api_request_path"]
    assert request_path.is_file()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["endpoint"] == "/prompt"
    assert request["method"] == "POST"
    assert request["payload"]["client_id"] == started["api_request_client_id"]
    assert request["payload"]["prompt"]["6"]["inputs"]["seed"] == 7
    assert request["payload"]["prompt"]["3"]["inputs"]["lyrics"]
    assert request["prompt_sha256"] == started["api_request_prompt_sha256"]
    assert "submission_cap" not in request
    assert completed["api_request_sha256"] == started["api_request_sha256"]
    assert completed["api_request_path"] == started["api_request_path"]
    assert asset["meta"]["music"]["api_request_sha256"] == started["api_request_sha256"]
    assert asset["meta"]["music"]["api_request_prompt_sha256"] == started["api_request_prompt_sha256"]
    take = service.takes(pid)["takes"][-1]
    assert take["api_request_retained"] is True
    assert take["api_request_sha256"] == started["api_request_sha256"]

    approved = asyncio.run(service.approve_audio(pid, asset["id"], "exact bytes approved"))
    assert approved["audio_approval"]["asset_id"] == asset["id"]
    assert approved["audio_approval"]["audio_sha256"] == approved["current_attempt"]["audio_sha256"]

    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))

    # A fresh service instance reads the same durable journal.
    restarted = _service(FakeComfy())
    with pytest.raises(MusicCapReached):
        asyncio.run(restarted.generate(pid))


def test_draft_change_invalidates_approval(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 1)
    changed = _settings().model_copy(update={"lyrics": "[verse]\nChanged lyric line here.\n"})
    service.save_draft(pid, changed)
    status = asyncio.run(service.status(pid))
    assert status["approval"] is None
    with pytest.raises(MusicApprovalRequired):
        asyncio.run(service.generate(pid))


def test_failure_is_journaled_and_manual_retry_uses_the_cap(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 2)
    fake.fetch_error = ComfyUIError("synthetic post-render failure")

    with pytest.raises(MusicGenerationFailed):
        asyncio.run(service.generate(pid))
    failed = asyncio.run(service.status(pid))
    assert failed["journal"]["failed"] == 1
    assert failed["journal"]["submissions"] == 1
    assert failed["can_generate"] is True
    failed_row = [row for row in service._read_journal(pid)
                  if row["kind"] == "attempt_failed"][0]
    assert failed_row["api_request_retained"] is True
    assert (service.store.root / pid / failed_row["api_request_path"]).is_file()
    assert failed_row["api_request_sha256"]

    fake.fetch_error = None
    completed = asyncio.run(service.generate(pid))
    assert completed["current_attempt"]["status"] == "completed"
    assert completed["journal"]["submissions"] == 2
    assert len(fake.submissions) == 2
    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))


def test_cancel_marks_uncertain_then_recovery_allows_next_attempt(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 2)
    fake.block = True

    async def scenario():
        task = asyncio.create_task(service.generate(pid))
        for _ in range(200):
            if fake.submissions:
                break
            await asyncio.sleep(0.01)
        assert fake.submissions
        await service.cancel(pid)
        with pytest.raises(MusicRecoveryRequired):
            await task
        status = await service.status(pid)
        assert status["recovery_required"] is True
        assert status["current_attempt"]["status"] == "uncertain"
        with pytest.raises(MusicRecoveryRequired):
            await service.generate(pid)
        fake.block = False
        recovered = await service.recover(pid, True)
        assert recovered["recovery_required"] is False
        completed = await service.generate(pid)
        assert completed["current_attempt"]["status"] == "completed"
        assert completed["journal"]["submissions"] == 2

    asyncio.run(scenario())


def test_stale_audio_approval_is_rejected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 1)
    status = asyncio.run(service.generate(pid))
    asset = status["assets"][0]
    changed = _settings().model_copy(update={"lyrics": "[verse]\nDifferent approved words.\n"})
    service.save_draft(pid, changed)
    with pytest.raises(MusicApprovalRequired):
        asyncio.run(service.approve_audio(pid, asset["id"], "stale"))

    with pytest.raises(MusicConflict):
        asyncio.run(service.recover(pid, True))


def test_browser_html_and_unauthorized_route(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    music_module._service = service
    client = TestClient(create_app())
    html = client.get("/").text
    assert 'data-tab="music"' in html
    assert 'data-panel="music"' in html
    assert 'id="music-generate-btn"' in html
    assert 'id="music-approve-btn"' in html
    assert 'id="music-player"' in html
    assert 'id="music-download"' in html
    assert 'id="music-blockers"' in html

    created = client.post("/api/projects", json={
        "name": "browser-music", "brief": "", "brand_voice": "professional",
        "target_audience": "", "notes": "",
    })
    assert created.status_code == 200
    pid = created.json()["id"]
    draft = client.put(f"/api/music/{pid}/draft", json={
        "project_id": pid, "settings": _settings().model_dump(mode="json"),
    })
    assert draft.status_code == 200
    approved = client.post(f"/api/music/{pid}/approve", json={
        "project_id": pid, "note": "browser approved", "submission_cap": 1,
    })
    assert approved.status_code == 200
    gen = client.post(f"/api/music/{pid}/generate", json={"project_id": pid})
    assert gen.status_code == 403
    status = client.get(f"/api/music/{pid}")
    assert status.status_code == 200
    assert status.json()["runtime"]["ready"] is True
    assert status.json()["can_generate"] is False


def test_workflow_structure_matches_installed_comfyui_schemas(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    try:
        object_info = httpx.get("http://127.0.0.1:8188/object_info", timeout=30).json()
    except Exception as exc:
        pytest.skip(f"ComfyUI unavailable: {exc}")
    workflow = json.loads(
        (Path(__file__).resolve().parents[2] / "comfyui" / "workflows" / "ace_step_1_5_aio.json")
        .read_text(encoding="utf-8")
    )
    service = _service()
    errors = service._validate_workflow(workflow, object_info)
    assert errors == []
    options = object_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    assert "ace_step_1.5_turbo_aio.safetensors" in options


def test_workflow_validation_rejects_unknown_nodes(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    assert service._validate_workflow({"1": {"class_type": "MissingNode", "inputs": {}}}, {})

PROPOSED_LYRICS = """[verse]
The hallway light is thinning out,
A kettle hums, the room is still.
I fold the day into my hands
And leave the window open wide.

[chorus]
Stay with me through the quiet part,
Where every word is slow to start.
We do not need the whole wide sky,
Just enough to feel alive.
"""

PROPOSED_SETTINGS = {
    "musical_direction": "Warm acoustic pop, intimate female vocal, fingerpicked guitar, soft brushed drums, hopeful restraint, mid-tempo.",
    "lyrics": PROPOSED_LYRICS,
    "duration_s": 30.0,
    "language": "en",
    "bpm": 96,
    "time_signature": "4",
    "key_scale": "C major",
    "generate_audio_codes": True,
    "seed": 20261024,
}
PROPOSED_LYRICS_SHA = "b9d6fcca1939a6a99cbf9f94c7693c1c554e1b8dd3082ffebc3a2dcaf1b78623"
PROPOSED_SETTINGS_SHA = "00c05c64f3c84d6092a7e34ac5c51fd702cfdc20b27eb4b8716ddb7f74b8691d"


def test_proposed_first_test_hashes_match_persisted_draft(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    music_module._service = service
    client = TestClient(create_app())
    created = client.post("/api/projects", json={
        "name": "first-live-preflight", "brief": "", "brand_voice": "professional",
        "target_audience": "", "notes": "",
    })
    assert created.status_code == 200
    pid = created.json()["id"]
    saved = client.put(f"/api/music/{pid}/draft", json={
        "project_id": pid, "settings": PROPOSED_SETTINGS,
    })
    assert saved.status_code == 200
    state_path = tmp_path / pid / "music" / ".state" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    draft = state["draft"]
    assert draft["lyrics_sha256"] == PROPOSED_LYRICS_SHA
    assert draft["settings_sha256"] == PROPOSED_SETTINGS_SHA
    assert draft["settings"]["lyrics"].startswith("[verse]\n")
    assert draft["settings"]["lyrics"].endswith("Just enough to feel alive.\n")
    assert draft["settings"]["bpm"] == 96
    assert draft["settings"]["seed"] == 20261024
    assert draft["settings"]["duration_s"] == 30.0
    assert draft["settings"]["key_scale"] == "C major"
    assert state["approval"] is None
    assert state["current_attempt"] is None


def test_capacity_gate_blocks_authorized_generation_without_verification(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 1)

    async def unverified_runtime():
        return {
            "ready": True,
            "offline_ready": True,
            "capacity_verified": False,
            "capacity_override_authorized": False,
            "capacity_ready": False,
            "capacity_label": "unverified",
            "live_ready": False,
            "errors": [],
            "live_generation_authorized": True,
            "comfyui_online": True,
            "checkpoint_available": True,
            "workflow_valid": True,
        }

    service.runtime = unverified_runtime
    with pytest.raises(Exception) as exc:
        asyncio.run(service.generate(pid))
    assert "capacity is not verified" in str(exc.value)
    assert fake.submissions == []


@pytest.mark.parametrize("mode", ["failed", "rejected", "timeout"])
def test_cap_one_counts_non_success_outcome_and_blocks_retry(tmp_path, monkeypatch, mode):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved for one bounded attempt", 1)
    if mode == "rejected":
        fake.submit_error = ComfyUIError("synthetic ComfyUI rejection")
        expected_error = MusicGenerationFailed
    elif mode == "timeout":
        fake.wait_error = ComfyUIError("Timed out waiting for ComfyUI prompt")
        expected_error = MusicRecoveryRequired
    else:
        fake.fetch_error = ComfyUIError("synthetic post-render failure")
        expected_error = MusicGenerationFailed

    with pytest.raises(expected_error):
        asyncio.run(service.generate(pid))
    status = asyncio.run(service.status(pid))
    assert status["journal"]["submissions"] == 1
    assert len(fake.submissions) == 1
    if mode == "timeout":
        assert status["journal"]["uncertain"] == 1
        assert status["recovery_required"] is True
        assert status["capacity_hold"] is not None
        with pytest.raises(MusicRecoveryRequired):
            asyncio.run(service.generate(pid))
        recovered = asyncio.run(service.recover(pid, True))
        assert recovered["recovery_required"] is False
        assert recovered["capacity_hold"] is None
    else:
        assert status["journal"]["failed"] == 1
    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))
    assert len(fake.submissions) == 1


def test_cap_one_counts_uncertain_outcome_and_blocks_retry_after_recovery(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved for one bounded attempt", 1)
    fake.block = True

    async def scenario():
        task = asyncio.create_task(service.generate(pid))
        for _ in range(200):
            if fake.submissions:
                break
            await asyncio.sleep(0.01)
        assert fake.submissions
        await service.cancel(pid)
        with pytest.raises(MusicRecoveryRequired):
            await task
        status = await service.status(pid)
        assert status["journal"]["submissions"] == 1
        assert status["journal"]["uncertain"] == 1
        assert status["capacity_hold"] is not None
        with pytest.raises(MusicRecoveryRequired):
            await service.generate(pid)
        recovered = await service.recover(pid, True)
        assert recovered["recovery_required"] is False
        assert recovered["capacity_hold"] is None
        with pytest.raises(MusicCapReached):
            await service.generate(pid)
        assert len(fake.submissions) == 1
        assert len(fake.cancelled) >= 1

    asyncio.run(scenario())


def test_actual_runtime_labels_live_capacity_unverified(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    try:
        httpx.get("http://127.0.0.1:8188/system_stats", timeout=10).raise_for_status()
    except Exception as exc:
        pytest.skip(f"ComfyUI unavailable: {exc}")
    async def scenario():
        comfy = ComfyUIClient()
        try:
            service = MusicService(comfy=comfy, router=FakeRouter())
            return await service.runtime()
        finally:
            await comfy.close()

    runtime = asyncio.run(scenario())
    assert runtime["ready"] is True
    assert runtime["offline_ready"] is True
    assert runtime["capacity_verified"] is False
    assert runtime["capacity_override_authorized"] is False
    assert runtime["capacity_ready"] is False
    assert runtime["capacity_label"] == "unverified"
    assert runtime["live_ready"] is False
    assert runtime["live_generation_authorized"] is False


def test_route_cap_one_blocks_second_generation(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    music_module._service = service
    client = TestClient(create_app())
    created = client.post("/api/projects", json={
        "name": "route-cap-one", "brief": "", "brand_voice": "professional",
        "target_audience": "", "notes": "",
    })
    assert created.status_code == 200
    pid = created.json()["id"]
    assert client.put(f"/api/music/{pid}/draft", json={
        "project_id": pid, "settings": _settings().model_dump(mode="json"),
    }).status_code == 200
    assert client.post(f"/api/music/{pid}/approve", json={
        "project_id": pid, "note": "one bounded attempt", "submission_cap": 1,
    }).status_code == 200
    first = client.post(f"/api/music/{pid}/generate", json={"project_id": pid})
    assert first.status_code == 200
    assert first.json()["current_attempt"]["status"] == "completed"
    assert len(fake.submissions) == 1
    second = client.post(f"/api/music/{pid}/generate", json={"project_id": pid})
    assert second.status_code == 409
    assert "lifetime submission cap" in second.json()["detail"]
    assert len(fake.submissions) == 1


def test_cancel_after_timeout_cleans_up_prompt_without_new_dispatch(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "one bounded attempt", 1)
    fake.wait_error = ComfyUIError("Timed out waiting for ComfyUI prompt")

    with pytest.raises(MusicRecoveryRequired):
        asyncio.run(service.generate(pid))
    status = asyncio.run(service.status(pid))
    assert status["current_attempt"]["status"] == "uncertain"
    assert status["current_attempt"]["prompt_id"] == "prompt-1"
    assert status["journal"]["submissions"] == 1
    assert status["recovery_required"] is True
    assert status["capacity_hold"] is not None

    cleanup = asyncio.run(service.cancel(pid))
    assert cleanup["prompt_id"] == "prompt-1"
    assert cleanup["prior_status"] == "uncertain"
    assert fake.cancelled == ["prompt-1"]
    assert len(fake.submissions) == 1
    recovered = asyncio.run(service.recover(pid, True))
    assert recovered["recovery_required"] is False
    assert recovered["capacity_hold"] is None
    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))
    assert len(fake.submissions) == 1


def test_shared_comfyui_switch_is_refused_without_unload(tmp_path):
    async def scenario():
        router = ModelRouter(holds_path=tmp_path / "holds.json")
        unload_calls: list[str] = []

        async def unloader():
            unload_calls.append("called")
            return {"verified": True}

        router.register_unloader(1, unloader)
        await router.acquire("wan21_i2v_14b_fp8", "prior-job")
        await router.release("wan21_i2v_14b_fp8", "prior-job", unload=False)
        with pytest.raises(ModelSwitchRequired):
            await router.acquire("ace-step-1.5", "music-job", allow_unload=False)
        assert unload_calls == []
        status = router.status()
        assert status["slots"][str(int(Slot.GPU1))]["current_model"] == "wan21_i2v_14b_fp8"

    asyncio.run(scenario())


def test_capacity_override_env_does_not_set_verified(monkeypatch):
    monkeypatch.setenv("LOCALAI_MUSIC__LIVE_GENERATION_AUTHORIZED", "true")
    monkeypatch.setenv("LOCALAI_MUSIC__CAPACITY_OVERRIDE_AUTHORIZED", "true")
    monkeypatch.delenv("LOCALAI_MUSIC__CAPACITY_VERIFIED", raising=False)
    monkeypatch.setattr(settings_module, "_cached", None)
    cfg = get_settings().music
    assert cfg.live_generation_authorized is True
    assert cfg.capacity_override_authorized is True
    assert cfg.capacity_verified is False


def test_capacity_override_is_labeled_experimental_not_verified(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "offline override", 1)

    async def runtime() -> dict:
        return {
            "ready": True,
            "offline_ready": True,
            "capacity_verified": False,
            "capacity_override_authorized": True,
            "capacity_ready": True,
            "capacity_label": "experimental override (not verified)",
            "live_ready": True,
            "errors": [],
            "live_generation_authorized": True,
            "comfyui_online": True,
            "checkpoint_available": True,
            "workflow_valid": True,
        }

    service.runtime = runtime
    status = asyncio.run(service.status(pid))
    assert status["runtime"]["capacity_verified"] is False
    assert status["runtime"]["capacity_override_authorized"] is True
    assert status["runtime"]["capacity_ready"] is True
    assert status["runtime"]["capacity_label"] == "experimental override (not verified)"
    assert status["can_generate"] is True
    assert status["blockers"] == []


def test_status_explains_model_switch_without_unloading(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "model switch check", 1)
    service.router.current_model = "wan21_i2v_14b_fp8"

    status = asyncio.run(service.status(pid))
    assert status["can_generate"] is False
    assert any(b["code"] == "model_switch_required" for b in status["blockers"])
    assert "will not unload" in status["blocked_reason"]
    assert "wan21_i2v_14b_fp8" in status["blocked_reason"]


def test_generate_model_switch_error_has_actionable_message(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()

    class SwitchRouter(FakeRouter):
        async def acquire(self, model: str, job_id: str, *, allow_unload: bool = True) -> None:
            raise ModelSwitchRequired(Slot.GPU1, "wan21_i2v_14b_fp8", model)

    service = _service(fake, SwitchRouter())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "model switch check", 1)

    with pytest.raises(MusicConflict) as exc:
        asyncio.run(service.generate(pid))
    assert "will not unload" in str(exc.value)
    assert "wan21_i2v_14b_fp8" in str(exc.value)
    assert fake.submissions == []


def test_status_explains_recovery_blocker(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "recovery check", 1)
    service.router.hold("ace-step-1.5", "prior-job", reason="uncertain prior job")

    status = asyncio.run(service.status(pid))
    assert status["can_generate"] is False
    assert any(b["code"] == "recovery_required" for b in status["blockers"])
    assert "Verify the ComfyUI job stopped" in status["blocked_reason"]


def test_status_explains_missing_approval_and_exhausted_cap(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())

    before = asyncio.run(service.status(pid))
    assert before["can_generate"] is False
    assert any(b["code"] == "approval_required" for b in before["blockers"])

    service.approve(pid, "cap one", 1)
    asyncio.run(service.generate(pid))
    after = asyncio.run(service.status(pid))
    assert after["can_generate"] is False
    assert any(b["code"] == "cap_exhausted" for b in after["blockers"])
    assert "cannot submit again" in after["blocked_reason"]


def test_render_stage_status_does_not_shadow_dom_helper():
    app_js = (
        Path(__file__).resolve().parents[2] / "frontend" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    start = app_js.index("function renderStageStatus() {")
    cursor = app_js.index("{", start)
    depth = 0
    end = None
    for index in range(cursor, len(app_js)):
        char = app_js[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end is not None
    body = app_js[start:end]
    assert 'const box = document.getElementById("stage-status")' in body
    assert "box.append(badge)" in body
    assert 'el("span"' in body
    assert re.search(r"\b(?:const|let|var)\s+el\b", body) is None


# --- v2 additions: settings knobs, arrangement, preflight ------------------

def test_music_settings_v2_field_validation():
    with pytest.raises(ValidationError):
        MusicSettings(
            musical_direction="x" * 8, lyrics="l" * 8, arrangement={"verse": "   "})
    with pytest.raises(ValidationError):
        MusicSettings(
            musical_direction="x" * 8, lyrics="l" * 8, temperature=2.5)
    with pytest.raises(ValidationError):
        MusicSettings(
            musical_direction="x" * 8, lyrics="l" * 8, top_p=1.5)
    with pytest.raises(ValidationError):
        MusicSettings(
            musical_direction="x" * 8, lyrics="l" * 8, cfg_scale=0.0)


def test_duration_accepts_up_to_ten_minutes():
    base = _settings()
    payload = base.model_dump(mode="json")
    payload["duration_s"] = 308.0
    assert MusicSettings.model_validate(payload).duration_s == 308.0
    payload["duration_s"] = 600.0
    assert MusicSettings.model_validate(payload).duration_s == 600.0
    from pydantic import ValidationError
    payload["duration_s"] = 600.1
    with pytest.raises(ValidationError):
        MusicSettings.model_validate(payload)


def test_settings_hash_ignores_unset_v2_fields():
    base = _settings()
    v1_fields = (
        "musical_direction", "lyrics", "duration_s", "language", "bpm",
        "time_signature", "key_scale", "generate_audio_codes", "seed",
    )
    legacy_payload = {name: getattr(base, name) for name in v1_fields}
    legacy_payload.pop("lyrics")
    expected = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert music_module._settings_hash(base) == expected

    with_arrangement = base.model_copy(update={"arrangement": {"verse": "swell"}})
    assert music_module._settings_hash(with_arrangement) != expected
    with_knob = base.model_copy(update={"temperature": 1.0})
    assert music_module._settings_hash(with_knob) != expected


def test_preflight_flags_unknown_empty_and_orphan_sections(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    status = asyncio.run(service.status(pid))
    assert status["preflight"] == []

    settings = _settings().model_copy(update={
        "lyrics": (
            "[wobbulator]\n[verse]\nA quiet room, a borrowed light,\n"
            "[chorus]\n\n[outro]\nOne last line to hold the night,\n"
        ),
        "arrangement": {"coda": "softly"},
    })
    saved = service.save_draft(pid, settings)
    codes = {warning["code"] for warning in saved["preflight"]}
    assert codes == {"unknown_section", "empty_section", "arrangement_orphan"}
    orphan = next(w for w in saved["preflight"] if w["code"] == "arrangement_orphan")
    assert orphan["section"] == "coda"
    unknown = next(w for w in saved["preflight"] if w["code"] == "unknown_section")
    assert unknown["section"] == "wobbulator"

    status = asyncio.run(service.status(pid))
    assert {warning["code"] for warning in status["preflight"]} == codes


def test_preflight_flags_lyric_density(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)

    dense = _settings().model_copy(update={
        "lyrics": "[verse]\nLine one takes time,\nLine two takes time,\nLine three takes time,\n",
    })
    saved = service.save_draft(pid, dense)
    assert "lyric_density_high" in {w["code"] for w in saved["preflight"]}

    sparse = _settings().model_copy(update={"duration_s": 30.0})
    saved = service.save_draft(pid, sparse)
    assert "lyric_density_low" in {w["code"] for w in saved["preflight"]}


def test_arrangement_and_knobs_reach_workflow_patches(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    settings = MusicSettings(
        musical_direction="Warm acoustic pop with intimate female vocal",
        lyrics="[verse]\nA quiet room, a borrowed light,\nA melody that learns to write.\n",
        duration_s=12.0,
        language="en",
        bpm=100,
        time_signature="4",
        key_scale="C major",
        generate_audio_codes=True,
        seed=7,
        arrangement={"verse": "soft arpeggio, brushed drums"},
        temperature=1.1,
        top_p=0.85,
        cfg_scale=3.0,
    )
    service.save_draft(pid, settings)
    service.approve(pid, "arranged", 1)

    status = asyncio.run(service.generate(pid))
    _, patches = fake.submissions[0]
    node3 = patches["3"]
    assert node3["tags"] == (
        "Warm acoustic pop with intimate female vocal; "
        "verse: soft arpeggio, brushed drums")
    assert node3["temperature"] == 1.1
    assert node3["top_p"] == 0.85
    assert node3["cfg_scale"] == 3.0
    asset = next(
        item for item in status["assets"]
        if item["meta"]["music"]["attempt_id"] == status["current_attempt"]["id"])
    music = asset["meta"]["music"]
    assert music["temperature"] == 1.1
    assert music["cfg_scale"] == 3.0
    assert music["overrides"] == {}


def test_default_knobs_stay_out_of_workflow_patches(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "plain", 1)

    asyncio.run(service.generate(pid))
    _, patches = fake.submissions[0]
    assert "temperature" not in patches["3"]
    assert "top_p" not in patches["3"]
    assert "cfg_scale" not in patches["3"]
    assert patches["3"]["tags"] == "Warm acoustic pop with intimate female vocal"


# --- v2 additions: critique registry ----------------------------------------

def _critique_request(pid: str, summary: str) -> MusicCritiqueRequest:
    return MusicCritiqueRequest(
        project_id=pid,
        summary=summary,
        suggestions=[MusicCritiqueSuggestion(
            target="verse 1",
            kind="lyric",
            current="A quiet room, a borrowed light,",
            alternatives=["A quiet room, a guttering light,"],
            rationale="sharpens the image",
        )],
        key_candidates=["A minor", "C major"],
        refined_direction="Warmer folk pop, softer upper register.",
    )


def test_critique_key_candidates_and_suggestions_validated():
    with pytest.raises(ValidationError):
        MusicCritiqueRequest(
            project_id="p",
            summary="Some summary text.",
            suggestions=[MusicCritiqueSuggestion(
                target="t", kind="lyric", alternatives=["a"], rationale="r")],
            key_candidates=["H minor"],
        )
    with pytest.raises(ValidationError):
        MusicCritiqueSuggestion(
            target="t", kind="lyric", alternatives=["", "x"], rationale="r")
    with pytest.raises(ValidationError):
        MusicCritiqueSuggestion(
            target="t", kind="lyric", alternatives=[], rationale="r")


def test_critique_saved_bound_and_drop_oldest(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(base, music=dataclasses.replace(base.music, max_critiques=2)),
    )
    service = _service()
    pid = _project(service)

    with pytest.raises(MusicApprovalRequired):
        service.save_critique(pid, _critique_request(pid, "No draft yet."))

    service.save_draft(pid, _settings())
    first = service.save_critique(pid, _critique_request(pid, "First critique summary."))
    second = service.save_critique(pid, _critique_request(pid, "Second critique summary."))
    third = service.save_critique(pid, _critique_request(pid, "Third critique summary."))

    state = service._load_state(pid)
    assert [record.summary for record in state.critiques] == [
        "Second critique summary.", "Third critique summary."]
    assert first["draft_version"] == 1
    assert first["lyrics_sha256"] == state.draft.lyrics_sha256
    assert first["settings_sha256"] == state.draft.settings_sha256

    detail = service.get_critique(pid, second["critique_id"])
    assert detail["summary"] == "Second critique summary."
    assert len(detail["suggestions"]) == 1
    with pytest.raises(KeyError):
        service.get_critique(pid, first["critique_id"])

    status = asyncio.run(service.status(pid))
    assert [item["summary"] for item in status["critiques"]] == [
        "Second critique summary.", "Third critique summary."]
    assert all("suggestions" not in item for item in status["critiques"])
    assert status["critiques"][0]["suggestion_count"] == 1
    assert status["critiques"][0]["key_candidates"] == ["A minor", "C major"]


# --- v2 additions: lyric variant registry -----------------------------------

def test_variant_register_get_and_status_summary(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())

    variant = service.register_variant(pid, MusicVariantRequest(
        project_id=pid,
        name="verse alt",
        section="verse",
        occurrence=1,
        text="A crowded room, a stolen night,\r\nA melody that learns to fight.",
        note="punchier",
    ))
    assert variant["name"] == "verse alt"
    assert variant["text"] == "A crowded room, a stolen night,\nA melody that learns to fight."
    state = service._load_state(pid)
    assert variant["lyrics_sha256"] == state.draft.lyrics_sha256

    detail = service.get_variant(pid, "verse alt")
    assert detail["stale"] is False
    assert detail["text"] == variant["text"]

    status = asyncio.run(service.status(pid))
    summary = status["variants"][0]
    assert summary["name"] == "verse alt"
    assert summary["section"] == "verse"
    assert summary["occurrence"] == 1
    assert summary["note"] == "punchier"
    assert summary["stale"] is False
    assert "text" not in summary


def test_variant_conflicts(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)

    with pytest.raises(MusicApprovalRequired):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="early", section="verse", occurrence=1,
            text="Alternate line."))

    service.save_draft(pid, _settings())
    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="verse alt", section="verse", occurrence=1,
        text="Alternate verse text."))

    with pytest.raises(MusicConflict):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="verse alt", section="verse", occurrence=1,
            text="Duplicate name."))
    with pytest.raises(MusicConflict):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="bridge alt", section="bridge", occurrence=1,
            text="A bridge line."))
    with pytest.raises(MusicConflict):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="verse far", section="verse", occurrence=2,
            text="Too far."))
    with pytest.raises(MusicConflict):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="tagged", section="verse", occurrence=1,
            text="[chorus]\nTagged text."))
    with pytest.raises(MusicConflict):
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="blank", section="verse", occurrence=1,
            text="\n\n"))

    with pytest.raises(KeyError):
        service.get_variant(pid, "missing")
    with pytest.raises(KeyError):
        service.promote_variant(pid, "missing")


def test_variant_registry_full(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(base, music=dataclasses.replace(base.music, max_variants=1)),
    )
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())

    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="one", section="verse", occurrence=1,
        text="First alternate line."))
    with pytest.raises(MusicConflict) as exc:
        service.register_variant(pid, MusicVariantRequest(
            project_id=pid, name="two", section="verse", occurrence=1,
            text="Second alternate line."))
    assert "full" in str(exc.value)


def test_variant_promote_rewrites_section_and_invalidates(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 1)

    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="verse alt", section="verse", occurrence=1,
        text="A crowded room, a stolen night,\nA melody that learns to fight."))
    result = service.promote_variant(pid, "verse alt")
    assert result["promoted_variant"] == {
        "name": "verse alt", "section": "verse", "occurrence": 1}

    state = service._load_state(pid)
    assert state.draft.version == 2
    assert state.draft.settings.lyrics == (
        "[verse]\nA crowded room, a stolen night,\n"
        "A melody that learns to fight.\n")
    assert state.approval is None

    status = asyncio.run(service.status(pid))
    assert status["variants"][0]["stale"] is True
    assert status["approval"] is None


def test_variant_promote_replaces_correct_occurrence_block(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    settings = _settings().model_copy(update={
        "lyrics": (
            "[verse]\nFirst verse line.\n\n[chorus]\nChorus line.\n\n"
            "[verse]\nSecond verse line.\n"
        ),
    })
    service.save_draft(pid, settings)
    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="v2 alt", section="verse", occurrence=2,
        text="Rewritten second verse."))
    service.promote_variant(pid, "v2 alt")

    state = service._load_state(pid)
    assert state.draft.settings.lyrics == (
        "[verse]\nFirst verse line.\n\n[chorus]\nChorus line.\n\n"
        "[verse]\nRewritten second verse.\n"
    )


# --- v2 additions: variant sweep plans ----------------------------------------

def test_plan_items_require_valid_overrides():
    with pytest.raises(ValidationError):
        MusicVariantPlanItem(label="noop")
    with pytest.raises(ValidationError):
        MusicVariantPlanItem(label="bad key", key_scale="H major")
    assert MusicVariantPlanItem(label="ok", seed=5).seed == 5


def test_plan_validation_on_approve(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())

    with pytest.raises(MusicConflict):
        service.approve(pid, "dup labels", 2, [
            MusicVariantPlanItem(label="a", seed=1),
            MusicVariantPlanItem(label="a", seed=2),
        ])
    with pytest.raises(MusicConflict):
        service.approve(pid, "plan too big", 1, [
            MusicVariantPlanItem(label="a", seed=1),
            MusicVariantPlanItem(label="b", seed=2),
        ])

    service.approve(pid, "valid plan", 2, [MusicVariantPlanItem(label="a", seed=1)])
    state = service._load_state(pid)
    assert [item.label for item in state.approval.variant_plan] == ["a"]


def test_plan_labels_apply_overrides_once_then_base(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    saved = service.save_draft(pid, _settings())
    approved_settings_sha = saved["draft"]["settings_sha256"]
    plan = [
        MusicVariantPlanItem(
            label="one", seed=101, key_scale="A minor", bpm=88,
            temperature=1.2, top_p=0.9, cfg_scale=2.5),
        MusicVariantPlanItem(label="two", seed=202, bpm=140),
    ]
    service.approve(pid, "plan approval", 3, plan)

    with pytest.raises(MusicConflict) as exc:
        asyncio.run(service.generate(pid))
    assert "plan_label" in str(exc.value)
    assert "one, two" in str(exc.value)

    status = asyncio.run(service.generate(pid, "one"))
    assert status["current_attempt"]["plan_label"] == "one"
    assert status["executed_plan_labels"] == ["one"]

    _, patches = fake.submissions[0]
    assert patches["3"]["seed"] == 101
    assert patches["3"]["keyscale"] == "A minor"
    assert patches["3"]["bpm"] == 88
    assert patches["3"]["temperature"] == 1.2
    assert patches["3"]["top_p"] == 0.9
    assert patches["3"]["cfg_scale"] == 2.5
    assert patches["6"]["seed"] == 101

    started = [row for row in service._read_journal(pid) if row["kind"] == "attempt_started"]
    assert started[0]["plan_label"] == "one"
    assert started[0]["overrides"] == {
        "seed": 101, "key_scale": "A minor", "bpm": 88,
        "temperature": 1.2, "top_p": 0.9, "cfg_scale": 2.5,
    }
    assert started[0]["effective"]["seed"] == 101
    assert started[0]["effective"]["bpm"] == 88

    asset = next(
        item for item in status["assets"]
        if item["meta"]["music"]["attempt_id"] == status["current_attempt"]["id"])
    music = asset["meta"]["music"]
    assert music["plan_label"] == "one"
    assert music["seed"] == 101
    assert music["key_scale"] == "A minor"
    assert music["bpm"] == 88
    assert music["temperature"] == 1.2
    assert music["overrides"]["seed"] == 101
    assert music["settings_sha256"] == approved_settings_sha

    with pytest.raises(MusicConflict) as exc:
        asyncio.run(service.generate(pid, "one"))
    assert "already executed" in str(exc.value)
    with pytest.raises(MusicConflict) as exc:
        asyncio.run(service.generate(pid, "three"))
    assert "not part of the approved variant plan" in str(exc.value)

    status = asyncio.run(service.generate(pid, "two"))
    assert status["executed_plan_labels"] == ["one", "two"]
    _, patches = fake.submissions[1]
    assert patches["3"]["seed"] == 202
    assert patches["3"]["bpm"] == 140

    status = asyncio.run(service.generate(pid))
    assert status["executed_plan_labels"] == ["one", "two"]
    _, patches = fake.submissions[2]
    assert patches["3"]["seed"] == 7
    assert patches["3"]["bpm"] == 100
    assert "temperature" not in patches["3"]

    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))


# --- v2 additions: approval mismatch detail and v1 state ----------------------

def test_approval_mismatch_distinguishes_lyrics_and_settings(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "ok", 1)

    state = service._load_state(pid)
    state.draft.settings = state.draft.settings.model_copy(update={
        "lyrics": "[verse]\nDifferent words entirely now.\n"})
    service._save_state(pid, state)

    status = asyncio.run(service.status(pid))
    blocker = next(
        b for b in status["blockers"] if b["code"] == "approval_required")
    assert "lyrics changed after approval" in blocker["message"]
    with pytest.raises(MusicApprovalRequired) as exc:
        asyncio.run(service.generate(pid))
    assert "lyrics changed after approval" in str(exc.value)

    service.approve(pid, "reapprove", 1)
    state = service._load_state(pid)
    state.draft.settings = state.draft.settings.model_copy(update={"bpm": 96})
    service._save_state(pid, state)

    status = asyncio.run(service.status(pid))
    blocker = next(
        b for b in status["blockers"] if b["code"] == "approval_required")
    assert "settings changed after approval" in blocker["message"]


def test_v1_state_file_upgrades_in_memory(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())

    path = service._state_path(pid)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("critiques")
    raw.pop("variants")
    raw.pop("revisions")
    raw.pop("preferred_take")
    raw.pop("repaint_proposals")
    raw.pop("section_boundaries")
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    state = service._load_state(pid)
    assert state.schema_version == 5
    assert state.critiques == []
    assert state.variants == []
    assert state.revisions == []
    assert state.preferred_take is None
    assert state.repaint_proposals == []
    assert state.section_boundaries == []

    status = asyncio.run(service.status(pid))
    assert status["critiques"] == []
    assert status["variants"] == []
    assert status["executed_plan_labels"] == []
    assert status["preferred_take"] is None


def test_router_exposes_critique_and_variant_endpoints(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())
    app = create_app()
    client = TestClient(app)

    critique = client.post(f"/api/music/{pid}/critique", json={
        "project_id": pid,
        "summary": "Chorus could lift higher.",
        "suggestions": [{
            "target": "chorus", "kind": "lyric", "current": "",
            "alternatives": ["Let it rise"], "rationale": "lift",
        }],
        "key_candidates": ["D major"],
    })
    assert critique.status_code == 200
    critique_id = critique.json()["critique_id"]
    assert client.get(f"/api/music/{pid}/critique/{critique_id}").json()[
        "summary"] == "Chorus could lift higher."
    assert client.get(f"/api/music/{pid}/critique/missing").status_code == 404

    variant = client.put(f"/api/music/{pid}/variant", json={
        "project_id": pid, "name": "verse alt", "section": "verse", "occurrence": 1,
        "text": "API alternate verse line.", "note": "",
    })
    assert variant.status_code == 200
    assert client.get(f"/api/music/{pid}/variant/verse%20alt").json()[
        "text"] == "API alternate verse line."
    assert client.get(f"/api/music/{pid}/variant/missing").status_code == 404

    promote = client.post(
        f"/api/music/{pid}/variant/verse%20alt/promote", json={"project_id": pid})
    assert promote.status_code == 200
    assert promote.json()["promoted_variant"]["name"] == "verse alt"

    mismatch = client.put(f"/api/music/{pid}/variant", json={
        "project_id": "other", "name": "x2", "section": "verse", "occurrence": 1,
        "text": "text here.", "note": "",
    })
    assert mismatch.status_code == 422


# --- v3 additions: revisions, takes, preferred selection ---------------------

def test_revision_chain_records_parents_sources_and_names(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)

    first = service.save_draft(pid, _settings(), "first cut")
    assert first["draft"]["version"] == 1
    assert first["draft"]["revision_name"] == "first cut"
    assert first["revision_created"] is True

    same = service.save_draft(pid, _settings(), "ignored rename")
    assert same["draft"]["version"] == 1
    assert same["revision_created"] is False
    assert same["draft"]["revision_name"] == "first cut"

    changed = service.save_draft(pid, _settings().model_copy(update={"bpm": 96}), "second cut")
    assert changed["draft"]["version"] == 2
    assert changed["revision_created"] is True

    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="verse alt", section="verse", occurrence=1,
        text="A brand new alternate verse.", note=""))
    promoted = service.promote_variant(pid, "verse alt")
    assert promoted["draft"]["version"] == 3
    assert promoted["revision_created"] is True

    revisions = service.revisions(pid)
    assert revisions["current_version"] == 3
    assert [r["version"] for r in revisions["revisions"]] == [1, 2, 3]
    assert [r["name"] for r in revisions["revisions"]] == [
        "first cut", "second cut", "variant verse alt"]
    assert [r["parent_version"] for r in revisions["revisions"]] == [None, 1, 2]
    assert [r["source"] for r in revisions["revisions"]] == ["draft", "draft", "promote"]

    detail = service.revision_detail(pid, 1)
    assert detail["name"] == "first cut"
    assert detail["settings"]["seed"] == 7
    with pytest.raises(KeyError):
        service.revision_detail(pid, 99)


def test_takes_preserve_exact_settings_revision_and_feedback(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)

    original = _settings()
    service.save_draft(pid, original, "first cut")
    service.save_critique(pid, _critique_request(pid, "Verse imagery is strong but static."))
    service.approve(pid, "approved v1", 2)
    first = asyncio.run(service.generate(pid))
    asset1 = first["current_attempt"]["asset_id"]
    asyncio.run(service.approve_audio(pid, asset1, "take 1 approved"))

    changed = original.model_copy(update={
        "lyrics": "[verse]\nA quiet room, a guttering light,\nA melody that learns to write.\n"})
    service.save_draft(pid, changed, "second cut")
    service.save_critique(pid, _critique_request(pid, "Guttering light lands better."))
    service.approve(pid, "approved v2", 2)
    second = asyncio.run(service.generate(pid))
    asset2 = second["current_attempt"]["asset_id"]

    started = [row for row in service._read_journal(pid) if row["kind"] == "attempt_started"]
    assert [row["revision"] for row in started] == [1, 2]
    assert [row["revision_name"] for row in started] == ["first cut", "second cut"]
    for row, expected in zip(started, [original, changed]):
        exact = row["effective_settings"]
        assert exact["lyrics"] == expected.lyrics
        assert exact["seed"] == expected.seed
        assert exact["bpm"] == expected.bpm

    meta1 = service.store.get_asset(pid, asset1).meta["music"]
    assert meta1["revision"] == 1
    assert meta1["revision_name"] == "first cut"
    assert meta1["effective_settings"]["lyrics"] == original.lyrics

    takes = service.takes(pid)
    assert [t["revision"] for t in takes["takes"]] == [1, 2]
    take1 = takes["takes"][0]
    take2 = takes["takes"][1]
    assert take1["asset_id"] == asset1
    assert take1["audio_approval"]["note"] == "take 1 approved"
    assert take1["preferred"] is False
    assert take1["critiques"][0]["summary"] == "Verse imagery is strong but static."
    assert take2["preferred"] is False
    assert take2["critiques"][0]["summary"] == "Guttering light lands better."

    preferred = service.prefer_take(pid, asset2, "tightest take")
    assert preferred["revision"] == 2
    takes = service.takes(pid)
    assert takes["takes"][0]["preferred"] is False
    assert takes["takes"][1]["preferred"] is True
    assert takes["preferred_take"]["asset_id"] == asset2
    assert takes["takes"][0]["audio_approval"]["note"] == "take 1 approved"
    status = asyncio.run(service.status(pid))
    assert status["preferred_take"]["asset_id"] == asset2
    assert status["preferred_take"]["note"] == "tightest take"


def test_preferred_take_selection_rules(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 2)
    first = asyncio.run(service.generate(pid))
    asset1 = first["current_attempt"]["asset_id"]
    second = asyncio.run(service.generate(pid))
    asset2 = second["current_attempt"]["asset_id"]
    asyncio.run(service.approve_audio(pid, asset2, "take 2 approved"))

    with pytest.raises(MusicConflict):
        service.prefer_take(pid, asset1, "   ")
    with pytest.raises(MusicConflict):
        service.prefer_take(pid, "missing-asset", "not a take")

    first_pick = service.prefer_take(pid, asset1, "warmer intro")
    assert first_pick["attempt_id"] == first["current_attempt"]["id"]
    reselect = service.prefer_take(pid, asset2, "tighter vocal")
    assert reselect["asset_id"] == asset2

    service.store.asset_path(pid, asset2).write_bytes(b"tampered audio bytes")
    with pytest.raises(MusicConflict):
        service.prefer_take(pid, asset2, "tampered again")

    back = service.prefer_take(pid, asset1, "warmer intro wins")
    assert back["asset_id"] == asset1
    takes = service.takes(pid)
    assert [t["preferred"] for t in takes["takes"]] == [True, False]
    assert takes["takes"][1]["audio_approval"]["note"] == "take 2 approved"
    meta2 = service.store.get_asset(pid, asset2).meta
    assert meta2["music_audio_approval"]["note"] == "take 2 approved"
    assert meta2["music_preferred_take"]["note"] == "tighter vocal"


def test_revision_retention_limit_fails_clearly(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(base, music=dataclasses.replace(base.music, max_revisions=2)),
    )
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings(), "v1 name")
    service.save_draft(pid, _settings().model_copy(update={"bpm": 96}), "v2 name")

    with pytest.raises(MusicConflict) as exc:
        service.save_draft(pid, _settings().model_copy(update={"bpm": 120}), "v3 blocked")
    assert "retention limit (2) reached" in str(exc.value)
    assert "parent of v2" in str(exc.value)
    assert "never deleted automatically" in str(exc.value)
    assert "music.max_revisions" in str(exc.value)

    revisions = service.revisions(pid)
    assert [r["version"] for r in revisions["revisions"]] == [1, 2]
    assert revisions["current_version"] == 2
    assert service.revision_detail(pid, 1)["name"] == "v1 name"
    status = asyncio.run(service.status(pid))
    assert status["draft"]["version"] == 2
    assert status["draft"]["settings"]["bpm"] == 96
    assert status["draft"]["revision_name"] == "v2 name"

    same = service.save_draft(pid, _settings().model_copy(update={"bpm": 96}))
    assert same["revision_created"] is False


def test_revision_retention_names_take_and_approval_references(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(base, music=dataclasses.replace(base.music, max_revisions=2)),
    )
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    original = _settings()
    service.save_draft(pid, original, "v1 name")
    service.approve(pid, "approved v1", 1)
    first = asyncio.run(service.generate(pid))
    asset1 = first["current_attempt"]["asset_id"]
    service.prefer_take(pid, asset1, "keeper")

    service.save_draft(pid, original.model_copy(update={"bpm": 96}), "v2 name")
    service.approve(pid, "approved v2", 1)

    with pytest.raises(MusicConflict) as exc:
        service.save_draft(pid, original.model_copy(update={"bpm": 120}), "v3 blocked")
    message = str(exc.value)
    assert "retention limit (2) reached" in message
    assert "v1" in message
    assert "1 take" in message
    assert "preferred take" in message
    assert "parent of v2" in message
    assert "approved draft" not in message

    takes = service.takes(pid)
    assert [t["revision"] for t in takes["takes"]] == [1]
    assert takes["takes"][0]["asset_id"] == asset1
    assert service.revision_detail(pid, 1)["settings"]["bpm"] == original.bpm


def test_restore_creates_new_revision_without_overwriting_history(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    original = _settings()
    service.save_draft(pid, original, "first cut")
    service.approve(pid, "approved v1", 2)
    first = asyncio.run(service.generate(pid))
    asset1 = first["current_attempt"]["asset_id"]
    changed = original.model_copy(update={"bpm": 96})
    service.save_draft(pid, changed, "second cut")
    service.approve(pid, "approved v2", 2)
    second = asyncio.run(service.generate(pid))
    asset2 = second["current_attempt"]["asset_id"]
    service.prefer_take(pid, asset1, "the take to keep")
    bytes1 = service.store.asset_path(pid, asset1).read_bytes()
    bytes2 = service.store.asset_path(pid, asset2).read_bytes()

    restored = service.restore_revision(pid, 1)
    assert restored["revision_created"] is True
    assert restored["restored_from"] == 1
    assert restored["draft"]["version"] == 3
    assert restored["draft"]["revision_name"] == "restored from v1"
    assert restored["draft"]["settings"]["bpm"] == original.bpm

    registry = service.revisions(pid)
    assert [r["version"] for r in registry["revisions"]] == [1, 2, 3]
    assert [r["source"] for r in registry["revisions"]] == ["draft", "draft", "restore"]
    assert registry["revisions"][2]["parent_version"] == 2
    detail1 = service.revision_detail(pid, 1)
    assert detail1["name"] == "first cut"
    assert detail1["settings"]["bpm"] == original.bpm

    takes = service.takes(pid)
    assert [t["revision"] for t in takes["takes"]] == [1, 2]
    assert takes["preferred_take"]["asset_id"] == asset1
    assert [t["preferred"] for t in takes["takes"]] == [True, False]
    assert service.store.asset_path(pid, asset1).read_bytes() == bytes1
    assert service.store.asset_path(pid, asset2).read_bytes() == bytes2

    status = asyncio.run(service.status(pid))
    assert status["approval"] is None

    noop = service.restore_revision(pid, 3)
    assert noop["revision_created"] is False
    assert noop["restored_from"] == 3
    assert asyncio.run(service.status(pid))["draft"]["version"] == 3
    with pytest.raises(KeyError):
        service.restore_revision(pid, 99)


def test_preferred_take_and_audio_approval_stay_independent(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings())
    service.approve(pid, "approved", 2)
    first = asyncio.run(service.generate(pid))
    asset1 = first["current_attempt"]["asset_id"]
    second = asyncio.run(service.generate(pid))
    asset2 = second["current_attempt"]["asset_id"]

    pick = service.prefer_take(pid, asset1, "picked without audio approval")
    assert pick["asset_id"] == asset1
    takes = service.takes(pid)
    assert takes["takes"][0]["preferred"] is True
    assert takes["takes"][0]["audio_approval"] is None
    assert takes["takes"][1]["preferred"] is False
    assert service._load_state(pid).audio_approval is None

    asyncio.run(service.approve_audio(pid, asset2, "approved but not preferred"))
    takes = service.takes(pid)
    assert takes["takes"][0]["preferred"] is True
    assert takes["takes"][0]["audio_approval"] is None
    assert takes["takes"][1]["preferred"] is False
    assert takes["takes"][1]["audio_approval"]["note"] == "approved but not preferred"
    assert service._load_state(pid).preferred_take.asset_id == asset1

    meta2 = service.store.get_asset(pid, asset2).meta
    assert "music_preferred_take" not in meta2
    assert meta2["music_audio_approval"]["note"] == "approved but not preferred"


def test_legacy_takes_are_preserved_with_evidence_labels(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings(), "modern cut")
    service.approve(pid, "approved", 2)
    first = asyncio.run(service.generate(pid))
    modern_asset = first["current_attempt"]["asset_id"]

    legacy_rows = [
        {
            "kind": "attempt_started",
            "attempt_id": "legacy-attempt",
            "at": "2025-01-01T00:00:00+00:00",
            "lyrics_sha256": "0" * 64,
            "settings_sha256": "0" * 64,
            "engine": "ace-step",
            "workflow": "ace_step_1_5_aio.json",
        },
        {
            "kind": "attempt_completed",
            "attempt_id": "legacy-attempt",
            "at": "2025-01-01T00:01:00+00:00",
            "asset_id": "legacy-asset",
            "sha256": "1" * 64,
            "prompt_id": "legacy-prompt",
        },
    ]
    journal_path = service._journal_path(pid)
    with journal_path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in legacy_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    takes = service.takes(pid)
    assert [t["attempt_id"] for t in takes["takes"]] == [
        first["current_attempt"]["id"], "legacy-attempt"]
    modern, legacy = takes["takes"]
    assert modern["legacy"] is False
    assert modern["take_provenance"]["uncertain"] is False
    assert modern["revision"] == 1
    assert modern["asset_id"] == modern_asset
    assert legacy["legacy"] is True
    assert legacy["revision"] is None
    assert legacy["effective_settings"] is None
    assert legacy["critiques"] == []
    provenance = legacy["take_provenance"]
    assert provenance["basis"] == "durable journal completion row (pre-v3 schema)"
    assert provenance["uncertain"] is True
    assert "none is reconstructed" in provenance["detail"]

    registry = service.revisions(pid)
    assert registry["revisions"][0]["take_count"] == 1

    with pytest.raises(MusicConflict):
        service.prefer_take(pid, "legacy-asset", "cannot verify missing bytes")


def test_revision_compare_fields_and_lyrics(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings(), "first cut")
    service.save_draft(pid, _settings().model_copy(update={
        "bpm": 96,
        "lyrics": (
            "[verse]\nA quiet room, a guttering light,\n"
            "A melody that learns to write.\n[chorus]\nLet it rise into the night.\n"),
    }), "second cut")

    diff = service.revision_compare(pid, 1, 2)
    assert [c["field"] for c in diff["settings_changes"]] == ["bpm"]
    assert diff["settings_changes"][0]["from"] == 100
    assert diff["settings_changes"][0]["to"] == 96
    replace = next(op for op in diff["lyrics_diff"] if op["op"] == "replace")
    assert replace["from_lines"] == ["A quiet room, a borrowed light,"]
    assert replace["to_lines"] == ["A quiet room, a guttering light,"]
    insert = next(op for op in diff["lyrics_diff"] if op["op"] == "insert")
    assert insert["from_lines"] == []
    assert insert["to_lines"] == ["[chorus]", "Let it rise into the night."]

    same = service.revision_compare(pid, 2, 2)
    assert same["settings_changes"] == []
    assert same["lyrics_diff"] == [{"op": "equal", "count": 5}]

    with pytest.raises(KeyError):
        service.revision_compare(pid, 1, 99)


def test_submission_caps_survive_edits_promotion_and_plan_exhaustion(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    plan = [
        MusicVariantPlanItem(label="one", seed=101),
        MusicVariantPlanItem(label="two", seed=202),
    ]
    service.save_draft(pid, _settings())
    service.approve(pid, "approved with plan", 2, plan)
    asyncio.run(service.generate(pid, "one"))
    asyncio.run(service.generate(pid, "two"))

    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))
    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid, "one"))

    service.register_variant(pid, MusicVariantRequest(
        project_id=pid, name="verse alt", section="verse", occurrence=1,
        text="A brand new alternate verse.", note=""))
    service.promote_variant(pid, "verse alt")

    with pytest.raises(MusicConflict):
        service.approve(pid, "cannot raise", 3)
    service.approve(pid, "reapproved v2", 2)
    with pytest.raises(MusicCapReached):
        asyncio.run(service.generate(pid))

    state = service._load_state(pid)
    assert state.lifetime_cap == 2
    assert service._journal_state(pid)["submissions"] == 2
    status = asyncio.run(service.status(pid))
    assert status["effective_cap"] == 2
    assert any(b["code"] == "cap_exhausted" for b in status["blockers"])
    takes = service.takes(pid)
    assert len(takes["takes"]) == 2


def test_every_music_form_control_reaches_the_workflow(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    settings = MusicSettings(
        musical_direction="Warm acoustic pop with intimate female vocal",
        lyrics=(
            "[verse]\nA quiet room, a borrowed light,\n"
            "[chorus]\nA melody that learns to write.\n"),
        duration_s=48.0,
        language="es",
        bpm=132,
        time_signature="3",
        key_scale="A minor",
        generate_audio_codes=False,
        seed=918273645,
        arrangement={
            "verse": "soft arpeggio, brushed drums",
            "chorus": "full band, layered harmonies",
        },
        temperature=1.25,
        top_p=0.7,
        cfg_scale=4.5,
    )
    service.save_draft(pid, settings, "control sweep")
    service.approve(pid, "control sweep approved", 1)
    status = asyncio.run(service.generate(pid))
    _, patches = fake.submissions[0]
    node3 = patches["3"]
    assert node3["tags"] == (
        "Warm acoustic pop with intimate female vocal; "
        "verse: soft arpeggio, brushed drums; chorus: full band, layered harmonies")
    assert node3["lyrics"] == settings.lyrics
    assert node3["seed"] == 918273645
    assert node3["bpm"] == 132
    assert node3["duration"] == 48.0
    assert node3["timesignature"] == "3"
    assert node3["language"] == "es"
    assert node3["keyscale"] == "A minor"
    assert node3["generate_audio_codes"] is False
    assert node3["temperature"] == 1.25
    assert node3["top_p"] == 0.7
    assert node3["cfg_scale"] == 4.5
    assert patches["5"] == {"seconds": 48.0}
    assert patches["6"] == {"seed": 918273645}
    assert patches["8"]["filename_prefix"] == (
        f"music/{pid}-{status['current_attempt']['id']}")

    asset = next(
        item for item in status["assets"]
        if item["id"] == status["current_attempt"]["asset_id"])
    music = asset["meta"]["music"]
    assert music["revision"] == 1
    assert music["revision_name"] == "control sweep"
    assert music["effective_settings"]["lyrics"] == settings.lyrics
    assert music["effective_settings"]["cfg_scale"] == 4.5
    assert music["effective_settings"]["arrangement"] == settings.arrangement


def test_arrangement_plan_round_trip_revisions_and_approval_invalidation(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service()
    pid = _project(service)

    plain = _settings()
    service.save_draft(pid, plain, "plain cut")
    service.approve(pid, "plain approved", 2)
    assert asyncio.run(service.status(pid))["approval"] is not None

    arranged = plain.model_copy(update={
        "arrangement": {
            "verse": "soft arpeggio, brushed drums, ~20s, intimate and restrained"}})
    result = service.save_draft(pid, arranged, "arranged cut")
    assert result["revision_created"] is True
    status = asyncio.run(service.status(pid))
    assert status["approval"] is None
    blocker = next(b for b in status["blockers"] if b["code"] == "approval_required")
    assert "reapprove" in blocker["message"] or "Approve" in blocker["message"]
    with pytest.raises(MusicApprovalRequired):
        asyncio.run(service.generate(pid))

    service.approve(pid, "arranged approved", 2)
    unchanged = service.save_draft(pid, arranged, "ignored")
    assert unchanged["revision_created"] is False
    assert asyncio.run(service.status(pid))["approval"] is not None

    retuned = arranged.model_copy(update={
        "arrangement": {
            "verse": "fingerpicked guitar, brushed drums, ~25s, warmer and brighter"}})
    result = service.save_draft(pid, retuned, "retuned cut")
    assert result["revision_created"] is True
    assert asyncio.run(service.status(pid))["approval"] is None
    service.approve(pid, "retuned approved", 2)

    registry = service.revisions(pid)
    assert [r["version"] for r in registry["revisions"]] == [1, 2, 3]
    assert registry["current_version"] == 3
    assert service.revision_detail(pid, 2)["settings"]["arrangement"] == {
        "verse": "soft arpeggio, brushed drums, ~20s, intimate and restrained"}
    assert service.revision_detail(pid, 3)["settings"]["arrangement"] == {
        "verse": "fingerpicked guitar, brushed drums, ~25s, warmer and brighter"}
    diff = service.revision_compare(pid, 2, 3)
    change = next(c for c in diff["settings_changes"] if c["field"] == "arrangement")
    assert change["from"]["verse"].startswith("soft arpeggio")
    assert change["to"]["verse"].startswith("fingerpicked guitar")
    assert diff["lyrics_diff"] == [{"op": "equal", "count": 3}]

    reloaded = _service()
    status = asyncio.run(reloaded.status(pid))
    assert status["draft"]["version"] == 3
    assert status["draft"]["revision_name"] == "retuned cut"
    assert status["draft"]["settings"]["arrangement"] == {
        "verse": "fingerpicked guitar, brushed drums, ~25s, warmer and brighter"}
    assert status["approval"] is not None


def test_arrangement_plan_table_shape_round_trips_through_revisions(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    pid = _project(service)
    plan = {
        "intro": "Target 0:00\u20130:12; acoustic guitar, fiddle, pedal steel.",
        "verse 1": "Target 0:12\u20130:36; restrained accompaniment, intimate storytelling.",
        "chorus": "Target 0:36\u20131:00 and 1:24\u20131:48; fuller two-step rhythm, nostalgic lift.",
        "verse 2": "Target 1:00\u20131:24; gently build warmth as family and time unfold.",
        "instrumental": "Target 1:48\u20132:00; fiddle and pedal steel trade phrases.",
        "bridge": "Target 2:00\u20132:24; pull back, tender invitation to dance again.",
        "final chorus": "Target 2:24\u20132:54; fullest emotional delivery.",
    }
    settings = _settings().model_copy(update={
        "arrangement": plan,
        "lyrics": (
            "[intro]\nInstrumental open.\n[verse]\nA quiet room, a borrowed light,\n"
            "[chorus]\nA melody that learns to write.\n"),
    })
    first = service.save_draft(pid, settings, "table plan")
    assert first["draft"]["settings"]["arrangement"] == plan

    warnings = first["preflight"]
    orphan_sections = {
        w["message"].split("for [", 1)[1].split("]", 1)[0]
        for w in warnings if w.get("code") == "arrangement_orphan"
    }
    assert "verse 1" in orphan_sections
    assert "verse 2" in orphan_sections
    assert "chorus" not in orphan_sections

    reloaded = _service()
    status = asyncio.run(reloaded.status(pid))
    assert status["draft"]["settings"]["arrangement"] == plan
    assert status["draft"]["revision_name"] == "table plan"

    retimed = dict(plan)
    retimed["chorus"] = (
        "Target 0:36\u20131:02 and 1:24\u20131:50; fuller two-step rhythm, brighter lift.")
    service.save_draft(pid, settings.model_copy(update={"arrangement": retimed}), "retimed")
    assert service.revision_detail(pid, 1)["settings"]["arrangement"] == plan
    assert service.revision_detail(pid, 2)["settings"]["arrangement"] == retimed
    diff = service.revision_compare(pid, 1, 2)
    change = next(c for c in diff["settings_changes"] if c["field"] == "arrangement")
    assert change["from"]["chorus"] == plan["chorus"]
    assert change["to"]["chorus"] == retimed["chorus"]
    assert "\u2013" in change["to"]["chorus"]


def test_browser_music_revision_and_take_controls(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=False)
    service = _service()
    music_module._service = service
    client = TestClient(create_app())
    html = client.get("/").text
    for element_id in [
        "music-arrangement-info", "music-arrangement-text", "music-arrangement-rows",
        "music-arrangement-new-section", "music-arrangement-add",
        "music-arrangement-preview",
        "music-temperature", "music-topp", "music-cfg",
        "music-preflight", "music-revision-btn", "music-revision-name",
        "music-revisions", "music-compare-from", "music-compare-to",
        "music-compare-btn", "music-compare-out",
        "music-takes", "music-prefer-note", "music-preferred",
    ]:
        assert f'id="{element_id}"' in html
    assert 'id="music-arrangement"' not in html
    assert "Arrangement Plan" in html
    assert "app.js?v=46" in html
    assert "app.css?v=14" in html

    repo_root = Path(__file__).resolve().parents[2]
    js = (repo_root / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Export take package" in js
    assert "takes/${t.asset_id}/package" in js
    form_body = js[js.index("function musicFormSettings"):js.index("function fillMusicForm")]
    for key in [
        "musical_direction", "lyrics", "duration_s", "language", "bpm",
        "time_signature", "key_scale", "generate_audio_codes", "seed",
        "arrangement", "temperature", "top_p", "cfg_scale",
    ]:
        assert key in form_body
    fill_body = js[js.index("function fillMusicForm"):js.index("async function refreshMusic")]
    for key in ["arrangement", "temperature", "top_p", "cfg_scale", "seed"]:
        assert key in fill_body
    assert "/revisions" in js
    assert "/takes" in js
    assert "/take/prefer" in js
    assert "revision_name" in js
    assert "revision_created" in js
    assert "from_version" in js
    assert "to_version" in js

    editor_body = js[js.index("let musicArrangementState"):js.index("function musicFormSettings")]
    assert "matchAll" in editor_body
    assert "[a-zA-Z0-9 _-]" in editor_body
    assert "musicEffectiveTagsPreview" in editor_body
    assert 'document.getElementById("music-arrangement-rows")' in editor_body
    assert 'document.getElementById("music-arrangement-info")' in editor_body
    assert 'document.getElementById("music-arrangement-preview")' in editor_body
    assert 'document.getElementById("music-arrangement-text")' in editor_body
    assert "splitMusicArrangementLine" in editor_body
    assert "parseMusicArrangementText" in editor_body
    assert "arrangementDictFromText" in editor_body
    assert 'line.indexOf("\\t")' in editor_body
    assert 'line.indexOf("|")' in editor_body
    # Numeric timing guidance must not be silently rejected. Node behavior tests
    # cover "Final Chorus: 1:00-1:20 lift" and exact section identities.
    assert "if (section && directive)" in editor_body
    assert "merged into one guidance" in editor_body
    assert "normalizeArrangementDirective" in editor_body
    assert "Line notes:" in editor_body
    assert "not saved" in editor_body
    assert "appended to the ACE-Step tags prompt as prose guidance" in editor_body
    assert "reference-only" in editor_body
    assert "approximate" in editor_body
    assert "separate from the sung lyrics" in editor_body
    assert "no tag" in editor_body
    assert js.count("renderMusicArrangement") >= 4
    assert js.rstrip().endswith("renderMusicArrangement();")
    assert 'addEventListener("input", renderMusicArrangement)' in js
    assert 'addEventListener("input", updateMusicArrangementPreview)' in js
    assert 'getElementById("music-arrangement-add").addEventListener' in js
    assert "MUSIC_SECTION_NAME_RE.test" in js
    assert 'music-arrangement-text").addEventListener("input", renderMusicArrangement' in js
    assert "musicArrangementProblems" in js
    form_arr = js[js.index("function musicFormSettings"):js.index("function fillMusicForm")]
    assert "arrangementDictFromText" in form_arr
    fill_arr = js[js.index("function fillMusicForm"):js.index("async function refreshMusic")]
    assert '${section}\\t${directive}' in fill_arr
    assert "parseMusicArrangement(" not in js
    assert "serializeMusicArrangement(" not in js

    assert "function restoreMusicRevision" in js
    assert "revisions/${version}/restore" in js
    assert "restoreMusicRevision(r.version, btn)" in js
    assert "already matches the current draft" in js
    approve_body = js[
        js.index('getElementById("music-approve-btn").addEventListener'):
        js.index('getElementById("music-generate-btn").addEventListener')]
    assert "musicFormSettings()" in approve_body
    assert "/draft" in approve_body
    assert approve_body.index("/draft") < approve_body.index("/approve")
    assert 'toast(`${label}${planNote}`, "ok")' in approve_body
    assert "Generate is blocked" not in approve_body
    assert "musicArrangementProblems" in approve_body
    assert "validateMusicArrangementForSave();" in approve_body
    assert approve_body.index("validateMusicArrangementForSave();") < approve_body.index("/draft")
    assert 'music-approve-btn").disabled = false' in js
    assert "approved the exact on-screen lyrics/settings" in approve_body
    save_body = js[
        js.index('getElementById("music-save-btn").addEventListener'):
        js.index('getElementById("music-approve-btn").addEventListener')]
    assert "saved as v${result.draft.version}" in save_body
    assert "validateMusicArrangementForSave();" in save_body
    assert save_body.index("validateMusicArrangementForSave();") < save_body.index("/draft")
    takes_body = js[js.index("async function refreshMusicTakes"):js.index("async function preferMusicTake")]
    assert 't.legacy' in takes_body
    assert "take_provenance" in takes_body
    assert "preserved from" in takes_body
    assert "legacy" in takes_body

    pid = _project(service)
    service.save_draft(pid, _settings(), "route check")
    assert client.get(f"/api/music/{pid}/revisions").json()["current_version"] == 1
    assert client.get(f"/api/music/{pid}/revisions/1").json()["name"] == "route check"
    assert client.get(f"/api/music/{pid}/revisions/99").status_code == 404
    compare = client.get(
        f"/api/music/{pid}/revisions/compare?from_version=1&to_version=1")
    assert compare.status_code == 200
    assert compare.json()["settings_changes"] == []
    assert client.get(f"/api/music/{pid}/takes").json()["takes"] == []
    prefer = client.post(f"/api/music/{pid}/take/prefer", json={
        "project_id": pid, "asset_id": "missing", "note": "not a take"})
    assert prefer.status_code == 409

    renamed = client.put(f"/api/music/{pid}/draft", json={
        "project_id": pid,
        "settings": _settings().model_dump(mode="json"),
        "revision_name": "api revision",
    })
    assert renamed.status_code == 200
    assert renamed.json()["revision_created"] is False
    assert client.get(f"/api/music/{pid}/revisions/1").json()["name"] == "route check"

    invalid_name = client.put(f"/api/music/{pid}/draft", json={
        "project_id": pid,
        "settings": _settings().model_dump(mode="json"),
        "revision_name": "",
    })
    assert invalid_name.status_code == 422

    assert client.post(f"/api/music/{pid}/revisions/99/restore").status_code == 404
    service.save_draft(pid, _settings().model_copy(update={"bpm": 96}), "changed cut")
    restore = client.post(f"/api/music/{pid}/revisions/1/restore")
    assert restore.status_code == 200
    assert restore.json()["revision_created"] is True
    assert restore.json()["restored_from"] == 1
    assert restore.json()["draft"]["revision_name"] == "restored from v1"
    assert client.get(f"/api/music/{pid}/revisions/1").json()["name"] == "route check"
    assert client.get(f"/api/music/{pid}/revisions").json()["current_version"] == 3
    noop = client.post(f"/api/music/{pid}/revisions/3/restore")
    assert noop.status_code == 200
    assert noop.json()["revision_created"] is False

def test_export_take_package_reuses_journal_and_profiles_without_allowances(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch, live=True)
    fake = BodyFakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings())
    service.approve(pid, "package approval", 1)
    status = asyncio.run(service.generate(pid))
    asset_id = status["assets"][0]["id"]
    service.save_section_boundary(
        pid, asset_id=asset_id, section="Bridge", occurrence=1,
        arrangement_index=6, start_s=0.01, end_s=0.04,
        suggested_start_s=0.01, suggested_end_s=0.04)
    profiles = ProfileStore(tmp_path / "profiles")
    profile_service = ProfileService(store=profiles, music=service)
    profile = profile_service.create_from_take(ArtistProfileCreateRequest(
        project_id=pid, asset_id=asset_id, name="Package Artist",
        artist_description="User-added archival note",
        vocal_description="User-added vocal note",
        production_defaults={"language": "en"},
    ))["profile"]
    export = TakeExportService(music=service, profiles=profiles)
    audit = export.audit(pid, asset_id)["audit"]
    assert audit["source_audio"]["verified"] is True
    assert audit["execution"]["api_request_retained"] is True
    assert audit["retained"]["engine"] == "ace-step-1.5"
    assert audit["retained"]["checkpoint_sha256"]
    assert audit["retained"]["workflow_template_sha256_recorded"]
    assert audit["retained"]["effective_settings"]["lyrics"] == _settings().lyrics
    assert audit["reconstructed"]["rendered_prompt"]["matches_recorded"] is True
    assert audit["current_only"]["matches_recorded_template"] is True
    assert audit["artist_profiles"][0]["id"] == profile["id"]
    assert audit["source_bound_markers"]["boundaries"][0]["section"] == "Bridge"
    assert "comfyui_queue_timestamps" in audit["unavailable"]
    assert "approval" in audit["excluded_fields"]
    assert "submission_cap" in audit["excluded_fields"]

    content, filename, digest = export.package(pid, asset_id)
    assert filename == f"take-{asset_id}-package.zip"
    assert digest
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "hashes.sha256" in names
        assert "execution/journal.jsonl" in names
        assert "execution/api_request.json" in names
        assert "settings/effective_settings.json" in names
        assert "markers/section_boundaries.json" in names
        assert "audio/ace-step-" in " ".join(names)
        assert f"artist-profiles/{profile['id']}/profile.json" in names
        assert any(name.startswith(f"artist-profiles/{profile['id']}/versions/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["excluded_fields"]
        assert manifest["package_files"]
        assert manifest["exact_execution_files"]
        assert "execution/records/request_body.body" in names
        assert "execution/records/response_metadata.json" in names
        assert "execution/records/history.json" in names
        assert "execution/records/output.json" in names
        assert "execution/records/outcome.json" in names
        assert "execution/records/environment.json" in names
        assert "execution/journal.jsonl" in manifest["redacted_files"]
        assert manifest["execution"]["exact_request_available"] is True
        original_body = archive.read("execution/records/request_body.body")
        assert hashlib.sha256(original_body).hexdigest() == manifest["execution"]["exact_request_body_sha256"]
        assert "approval" not in manifest
        assert "submission_cap" not in manifest
        archived_settings = json.loads(archive.read("settings/effective_settings.json"))
        asset_meta = service.store.get_asset(pid, asset_id).meta["music"]
        assert archived_settings == asset_meta["effective_settings"]
        assert archived_settings["lyrics"] == _settings().lyrics
        assert archived_settings["bpm"] == _settings().bpm
        assert "submission_cap" not in archived_settings
        journal = archive.read("execution/journal.jsonl").decode("utf-8")
        assert "submission_cap" not in journal
        request = json.loads(archive.read("execution/api_request.json"))
        assert request["endpoint"] == "/prompt"
        assert request["payload"]["prompt"]["6"]["inputs"]["seed"] == 7
        checks = {}
        for line in archive.read("hashes.sha256").decode("utf-8").splitlines():
            expected, name = line.split("  ", 1)
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            checks[name] = actual == expected
        assert checks and all(checks.values())

    music_module._service = service
    take_export_module._service = export
    client = TestClient(create_app())
    audit_response = client.get(f"/api/music/{pid}/takes/{asset_id}/provenance")
    assert audit_response.status_code == 200
    assert audit_response.json()["attempt_id"] == status["current_attempt"]["id"]
    package_response = client.get(f"/api/music/{pid}/takes/{asset_id}/package")
    assert package_response.status_code == 200
    assert package_response.headers["content-type"] == "application/zip"
    assert f"take-{asset_id}-package.zip" in package_response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(package_response.content)) as archive:
        assert "manifest.json" in archive.namelist()
    assert client.get(
        f"/api/music/{pid}/takes/ffffffffffff/provenance").status_code == 404

def test_exact_request_bytes_are_saved_and_transmitted_unmodified(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch, live=True)
    fake = BodyFakeComfy()
    service = _service(fake)
    pid = _project(service)
    exact_settings = _settings().model_copy(update={
        "arrangement": {"verse": "soft arpeggio", "chorus": "full band"},
    })
    service.save_draft(pid, exact_settings)
    service.approve(pid, "exact body", 1)
    asyncio.run(service.generate(pid))
    started = [row for row in service._read_journal(pid)
               if row["kind"] == "attempt_started"][0]
    completed = [row for row in service._read_journal(pid)
                 if row["kind"] == "attempt_completed"][0]
    request_path = service.store.root / pid / started["api_request_path"]
    body_path = service.store.root / pid / started["api_request_body_path"]
    request_bytes = request_path.read_bytes()
    body_bytes = body_path.read_bytes()
    request = json.loads(request_bytes)
    assert len(fake.body_submissions) == 1
    transmitted_body, transmitted_client = fake.body_submissions[0]
    assert transmitted_body == body_bytes
    assert hashlib.sha256(transmitted_body).hexdigest() == request["request_body_sha256"]
    transmitted = json.loads(transmitted_body)
    assert transmitted["client_id"] == transmitted_client
    assert transmitted_client == started["api_request_client_id"]
    assert transmitted["prompt"] == request["payload"]["prompt"]
    assert transmitted["prompt"]["3"]["inputs"]["lyrics"] == _settings().lyrics
    assert transmitted["prompt"]["3"]["inputs"]["tags"] == (
        "Warm acoustic pop with intimate female vocal; "
        "verse: soft arpeggio; chorus: full band")
    assert request["generation_inputs"]["tags"] == (
        "Warm acoustic pop with intimate female vocal; "
        "verse: soft arpeggio; chorus: full band")
    assert hashlib.sha256(
        json.dumps(transmitted["prompt"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == request["prompt_sha256"]
    assert request["serialization"]["order_preserved"] is True
    assert request["source_revision"] == 1
    assert request["input_files"] == []
    assert request["missing_input_files"] == []
    assert request["applied_patches_sha256"]
    assert request["generation_inputs"]["resolved_node3_inputs"]["seed"] == 7
    assert request_path.read_bytes() == request_bytes
    assert body_path.read_bytes() == body_bytes
    assert completed["response_path"]
    assert completed["history_path"]
    assert completed["output_path"]
    assert completed["outcome_path"]
    assert completed["environment_path"]
    response = json.loads(
        (service.store.root / pid / completed["response_path"]).read_text(encoding="utf-8"))
    assert response["prompt_id"] == "body-prompt-1"
    assert response["client_observed_at"] == "test-client-date"
    assert response["server_date"] == "test-server-date"
    history = json.loads(
        (service.store.root / pid / completed["history_path"]).read_text(encoding="utf-8"))
    assert history["complete"] is True
    assert "outputs" in history["result"]
    output = json.loads(
        (service.store.root / pid / completed["output_path"]).read_text(encoding="utf-8"))
    assert output["sha256"] == completed["sha256"]
    assert output["measured_properties"]["status"] in {"measured", "unavailable"}
    with pytest.raises(MusicConflict):
        service._persist_api_request(
            pid, started["attempt_id"], get_settings().music.workflow,
            service._patches(pid, started["attempt_id"], _settings()), _settings())
    assert request_path.read_bytes() == request_bytes
    assert body_path.read_bytes() == body_bytes


def test_request_persistence_failure_blocks_dispatch_without_consuming_cap(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "persist failure", 1)

    def fail(*_args, **_kwargs):
        raise MusicConflict("synthetic request persistence failure")

    service._persist_api_request = fail
    with pytest.raises(MusicConflict):
        asyncio.run(service.generate(pid))
    assert fake.submissions == []
    status = asyncio.run(service.status(pid))
    assert status["journal"]["submissions"] == 0
    assert status["journal"]["failed"] == 0
    assert status["approval"] is not None
    assert status["lifetime_cap"] == 1
    assert service.router.release_calls
    requests_dir = service._state_dir(pid) / "requests"
    assert not requests_dir.exists() or not any(requests_dir.iterdir())


def test_uncertain_record_retains_request_link(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    fake = FakeComfy()
    service = _service(fake)
    pid = _project(service)
    fake.journal_path = service._journal_path(pid)
    service.save_draft(pid, _settings())
    service.approve(pid, "uncertain request", 1)
    fake.wait_error = httpx.ReadTimeout("synthetic wait timeout")
    with pytest.raises(MusicRecoveryRequired):
        asyncio.run(service.generate(pid))
    row = [row for row in service._read_journal(pid)
           if row["kind"] == "attempt_uncertain"][0]
    assert row["api_request_retained"] is True
    assert row["api_request_sha256"]
    assert (service.store.root / pid / row["api_request_path"]).is_file()

def test_execution_records_are_isolated_per_attempt(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service()
    pid = _project(service)
    service.save_draft(pid, _settings())
    patches_a = service._patches(pid, "attempt-a", _settings())
    patches_b = service._patches(pid, "attempt-b", _settings())
    request_a, body_a = service._persist_api_request(
        pid, "attempt-a", get_settings().music.workflow, patches_a, _settings(),
        source_revision=1)
    request_b, body_b = service._persist_api_request(
        pid, "attempt-b", get_settings().music.workflow, patches_b, _settings(),
        source_revision=1)
    assert request_a["api_request_path"] != request_b["api_request_path"]
    assert request_a["api_request_body_path"] != request_b["api_request_body_path"]
    payload_a = json.loads(body_a)
    payload_b = json.loads(body_b)
    assert payload_a["client_id"] != payload_b["client_id"]
    assert payload_a["prompt"]["3"]["inputs"] == payload_b["prompt"]["3"]["inputs"]
    assert payload_a["prompt"]["8"]["inputs"]["filename_prefix"] != payload_b["prompt"]["8"]["inputs"]["filename_prefix"]
    assert request_a["api_request_body_sha256"] != request_b["api_request_body_sha256"]
    with pytest.raises(MusicConflict):
        service._persist_api_request(
            pid, "attempt-a", get_settings().music.workflow, patches_a, _settings(),
            source_revision=1)
    assert (service.store.root / pid / request_a["api_request_body_path"]).read_bytes() == body_a

def test_historical_missing_execution_records_are_labeled_redacted(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch, live=True)
    service = _service(FakeComfy())
    pid = _project(service)
    service.save_draft(pid, _settings())
    service.approve(pid, "legacy package", 1)
    status = asyncio.run(service.generate(pid))
    asset_id = status["assets"][0]["id"]
    shutil.rmtree(service._state_dir(pid) / "requests")
    export = TakeExportService(
        music=service, profiles=ProfileStore(tmp_path / "profiles"))
    audit = export.audit(pid, asset_id)["audit"]
    assert audit["execution"]["exact_request_available"] is False
    assert audit["execution"]["exact_records"] == {}
    assert audit["execution"]["journal_is_redacted"] is True
    assert "exact_resolved_comfyui_api_request" in audit["unavailable"]
    assert "comfyui_history_for_prompt" in audit["unavailable"]
    content, filename, _digest = export.package(pid, asset_id)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert "execution/api_request.json" not in names
        assert not any(name.startswith("execution/records/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["exact_execution_files"] == []
        assert manifest["redacted_files"] == ["execution/journal.jsonl"]
        journal = archive.read("execution/journal.jsonl").decode("utf-8")
        assert "submission_cap" not in journal
        assert "submission_cap" in manifest["excluded_fields"]
