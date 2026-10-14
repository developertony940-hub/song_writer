from __future__ import annotations
import asyncio
import dataclasses
from pathlib import Path

import httpx
import pytest


def run(coro): return asyncio.run(coro)


def test_assemble_uses_timeline_inputs_and_runs_a_quality_gate(tmp_path: Path, monkeypatch):
    from app.services.ffmpeg_runner import FFmpegRunner
    runner = FFmpegRunner()
    captured: list[list[str]] = []
    async def fake_run(args, cwd=None):
        captured.append(args)
        Path(args[-1]).write_bytes(b"output")
        return 0, ""
    async def fake_probe(path): return 2.0
    async def fake_qc(path, duration):
        runner._qc_by_path[str(path.resolve())] = {"passed": True, "duration_s": duration}
        return runner._qc_by_path[str(path.resolve())]
    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "probe_duration", fake_probe)
    monkeypatch.setattr(runner, "quality_gate", fake_qc)
    paths = {name: tmp_path / name for name in ("video.mp4", "voice.wav", "music.wav", "subs.vtt")}
    for path in paths.values(): path.write_bytes(b"fixture")
    for voice, music, subs in ((False, False, False), (True, False, False),
                               (False, True, False), (False, False, True),
                               (True, True, True)):
        output = tmp_path / f"out-{voice}-{music}-{subs}.mp4"
        run(runner.assemble(video_path=paths["video.mp4"], audio_path=paths["voice.wav"] if voice else None,
                            music_path=paths["music.wav"] if music else None,
                            subtitles_path=paths["subs.vtt"] if subs else None, output_path=output))
        # Timeline commands always start with colour + silence base inputs;
        # optional source tracks appear only when supplied.
        timeline = [args for args in captured if "-filter_complex" in args][-1]
        assert any(item.startswith("color=c=black") for item in timeline)
        assert any(item.startswith("anullsrc=r=") for item in timeline)
        assert any("adelay=" in item for item in timeline) == (voice or music)
        if subs:
            assert "1:s:0" in captured[-1]  # legacy VTT is muxed as the second mux input


def test_assemble_requires_visual_input(tmp_path: Path):
    from app.services.ffmpeg_runner import FFmpegRunner
    voice = tmp_path / "voice.wav"; voice.write_bytes(b"fixture")
    with pytest.raises(RuntimeError, match="visual video input"):
        run(FFmpegRunner().assemble(video_path=None, audio_path=voice, music_path=None,
                                    subtitles_path=None, output_path=tmp_path / "out.mp4"))


def test_lmstudio_validates_models_before_post():
    from app.services.lmstudio_client import LMStudioClient, LMStudioModelUnavailable
    calls: list[str] = []
    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "available-model"}]})
        return httpx.Response(500)
    client = LMStudioClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock"))
    with pytest.raises(LMStudioModelUnavailable):
        run(client.chat_text("hello", model="not-loaded"))
    assert calls == ["/models"]
    run(client.close())


def test_preflight_validates_workflow_nodes_and_models(tmp_path: Path, monkeypatch):
    from app.services.comfyui_client import ComfyUIClient
    from app.services.preflight import RuntimePreflight
    from app.config.settings import get_settings
    from app.config import settings as settings_module
    monkeypatch.setattr(settings_module, "_cached", dataclasses.replace(get_settings(), projects_root=tmp_path))
    comfy = ComfyUIClient(httpx.AsyncClient(base_url="http://mock"))
    object_info = {}
    for workflow in comfy.configured_workflows().values():
        contract = comfy.workflow_contract(workflow)
        for class_name in contract["node_classes"]:
            object_info.setdefault(class_name, {"input": {"required": {}}})
        for model in contract["models"]:
            required = object_info[model["class_type"]]["input"]["required"]
            choices = required.setdefault(model["input"], [[]])[0]
            if model["filename"] not in choices:
                choices.append(model["filename"])
    async def comfy_status(): return {"online": True}
    async def comfy_info(): return object_info
    monkeypatch.setattr(comfy, "status", comfy_status); monkeypatch.setattr(comfy, "object_info", comfy_info)
    class GoodLM:
        default_model = "copy"; vision_model = "vision"
        async def status(self): return {"online": True, "loaded_models": ["copy", "vision"]}
    class GoodTTS:
        async def status(self): return {"online": True}
    class GoodFF:
        async def status(self): return {"online": True, "ffprobe_available": True}
    result = run(RuntimePreflight(lmstudio=GoodLM(), comfyui=comfy, tts=GoodTTS(), ffmpeg=GoodFF()).run())
    assert result["ready"], result
    # Break a filename in the live dropdown: the same workflow must fail closed.
    object_info["UNETLoader"]["input"]["required"]["unet_name"] = [["other.safetensors"]]
    result = run(RuntimePreflight(lmstudio=GoodLM(), comfyui=comfy, tts=GoodTTS(), ffmpeg=GoodFF()).run())
    assert not result["ready"]
    assert "missing model" in " ".join(result["checks"]["comfyui"]["errors"])


def test_auto_run_reloads_saved_copy_and_never_auto_approves(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    from app.services.orchestrator import Orchestrator
    from app.services.project_store import get_store
    from app.services import project_store as ps
    from app.config.settings import get_settings
    from app.config import settings as settings_module
    monkeypatch.setattr(settings_module, "_cached", dataclasses.replace(get_settings(), projects_root=tmp_path))
    ps._store = None
    store = get_store()
    project = store.create(ProjectCreate(name="Test", brief="brief")); store.set_mode(project.id, "auto")
    class Router:
        async def acquire(self, *a): pass
        async def release(self, *a, **k): pass
    class LMS:
        default_model = "copy"
        async def chat_text(self, *a, **k): return "# Heading\nNarration"
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.store, orchestrator.router, orchestrator.lms = store, Router(), LMS()
    report = run(orchestrator.run_project(project.id))
    current = store.get(project.id)
    assert report["status"] == "awaiting_approval"
    assert current.stages["copy"] == "succeeded"
    assert current.artifacts["copy_plan"]
    with pytest.raises(ValueError): store.update_stage(project.id, "images", "approved")
    store.update_stage(project.id, "copy", "approved")
    report = run(orchestrator.run_project(project.id))
    assert report["stage"] == "images"
    assert store.get(project.id).stages["images"] != "approved"
