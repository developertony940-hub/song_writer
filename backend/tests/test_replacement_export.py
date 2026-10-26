"""Offline replacement-section export tests (no provider, no model, no apply)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from array import array
from pathlib import Path

import pytest

from app.config import get_settings
from app.config import settings as settings_module
from app.models.music import MusicSettings
from app.models.schemas import AssetKind, ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services import replacement_export as replacement_module
from app.services.music_service import (
    MusicService,
    _decode_pcm,
    _wav_f32_bytes,
)
from app.services.replacement_export import (
    ReplacementExportError,
    ReplacementExportService,
)
from app.services.repaint_provider import RepaintOutput, _repaint_form, RepaintRequest

SR = 48000
CH = 2
SECONDS = 16.0
FRAMES = int(SECONDS * SR)
START = 5.0
END = 10.0
CONTEXT_START = 3.0
CONTEXT_END = 12.0
TARGET = 5.0
BRIDGE_LINES = [
    "Maybe those old friends are still out there somewhere,",
    "Maybe the same old band still plays the songs we shared.",
    "And maybe we've got a few more miles on these old boots,",
    "But, baby, there's still a two-step left in me and you.",
    "We don't have to be young again,",
    "We don't have to turn back time,",
    "Just let me hold you like I did back then,",
    "And let that old fiddle play one more time.",
]
LYRICS = (
    "[verse]\nA short test verse.\n\n"
    "[bridge]\n" + "\n".join(BRIDGE_LINES) + "\n\n"
    "[final chorus]\nA short test chorus.\n"
)


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(
            base,
            projects_root=tmp_path,
            music=dataclasses.replace(
                base.music,
                live_generation_authorized=False,
                repaint_enabled=False,
                repaint_output_format="wav32",
            ),
        ),
    )
    ps._store = None
    music_module._service = None
    replacement_module._service = None


def _settings() -> MusicSettings:
    return MusicSettings(
        musical_direction="Warm acoustic country two-step",
        lyrics=LYRICS,
        duration_s=SECONDS,
        language="en",
        bpm=80,
        time_signature="4",
        key_scale="C major",
        generate_audio_codes=True,
        seed=20261024,
    )


def _source_values() -> array:
    values = array("f")
    for frame in range(FRAMES):
        left = 0.3 * ((frame % 97) - 48) / 48.0
        right = 0.2 * ((frame % 53) - 26) / 26.0
        values.append(left)
        values.append(right)
    return values


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset(tmp_path, monkeypatch)
    service = MusicService(comfy=object(), router=object())
    settings = _settings()
    service.save_draft(pid := service.store.create(
        ProjectCreate(name="replacement export", brief="fixture")).id, settings, "source")
    values = _source_values()
    data = _wav_f32_bytes(values, SR, CH)
    digest = hashlib.sha256(data).hexdigest()
    asset = service.store.save_asset(
        pid, AssetKind.music, "source.wav", data,
        meta={"music": {
            "revision": 1,
            "effective_settings": settings.model_dump(mode="json"),
            "audio_sha256": digest,
            "checkpoint_sha256": "67b0f43aa5c51c840bd0228e6a935d8ff416ec87e5df2fc0637da17a561252bc",
        }},
    )
    service._append_journal(pid, {
        "kind": "attempt_started", "attempt_id": "source-attempt",
        "at": "2026-10-25T00:00:00+00:00", "revision": 1,
        "revision_name": "source",
        "effective_settings": settings.model_dump(mode="json"),
        "engine": "ace-step-1.5",
        "checkpoint": "ace_step_1.5_turbo_aio.safetensors",
        "workflow": "ace_step_1_5_aio",
        "submission_cap": 4,
    })
    service._append_journal(pid, {
        "kind": "attempt_completed", "attempt_id": "source-attempt",
        "at": "2026-10-25T00:01:00+00:00", "asset_id": asset.id,
        "sha256": digest, "prompt_id": "source-prompt",
    })
    service.save_section_boundary(
        pid,
        asset_id=asset.id,
        section="Bridge",
        occurrence=1,
        arrangement_index=6,
        start_s=START,
        end_s=END,
        suggested_start_s=4.0,
        suggested_end_s=8.0,
    )
    return service, pid, asset.id, digest, values


def _request(pid: str, asset_id: str, **changes):
    from app.models.replacement import ReplacementExportRequest

    data = {
        "project_id": pid,
        "asset_id": asset_id,
        "section": "Bridge",
        "occurrence": 1,
        "target_duration_s": TARGET,
        "bridge_lyrics": "\n".join(BRIDGE_LINES),
        "performance_guidance": (
            "Restrained intimate male country lead; deliver all eight lines; "
            "keep the two-step pocket; no new hook or tempo change."
        ),
        "context_start_s": CONTEXT_START,
        "context_end_s": CONTEXT_END,
        "target_duration_reviewed": True,
        "context_reviewed": True,
        "context_note": "Reviewed 2s lead-in and 2s lead-out around the Bridge.",
    }
    data.update(changes)
    return ReplacementExportRequest(**data)


def test_capability_is_fixed_duration_only_and_names_the_comfyui_gap():
    svc = ReplacementExportService(music=object())
    capability = svc.capability()
    assert capability.provider == "ACE-Step 1.5 native repaint"
    assert capability.source_conditioning is True
    assert capability.duration_mode == "fixed_to_conditioning_source_duration"
    assert capability.supports_longer_replacement is False
    assert capability.supports_variable_duration is False
    assert capability.comfyui_workflow_supports_repaint is False
    assert capability.native_lyrics_conditioning_supported is True
    assert capability.export_client_can_send_lyrics_and_settings is True
    assert any("generate_music.py" in item for item in capability.evidence)
    assert any("nodes_ace.py" in item for item in capability.evidence)


def test_repaint_form_carries_lyrics_seed_and_conditioning_settings(tmp_path):
    request = RepaintRequest(
        source_path=tmp_path / "source.wav",
        source_sha256="0" * 64,
        start_s=2.0,
        end_s=7.0,
        prompt="Keep the pocket",
        settings={},
        output_format="wav32",
        lyrics="line one\nline two",
        seed=20261024,
        bpm=80,
        key_scale="C major",
        time_signature="4",
        language="en",
    )
    form = _repaint_form(request)
    assert form["task_type"] == "repaint"
    assert form["lyrics"] == "line one\nline two"
    assert form["seed"] == "20261024"
    assert form["use_random_seed"] == "false"
    assert form["bpm"] == "80"
    assert form["key_scale"] == "C major"
    assert form["time_signature"] == "4"
    assert form["vocal_language"] == "en"
    assert "audio_duration" not in form


def test_prepare_records_exact_inputs_handles_and_does_not_mutate_the_project(tmp_path, monkeypatch):
    service, pid, asset_id, digest, _values = _fixture(tmp_path, monkeypatch)
    before_state = service._load_state(pid).model_dump(mode="json")
    before_assets = (service.store.root / pid / "assets.json").read_bytes()
    svc = ReplacementExportService(music=service)
    manifest = svc.prepare(pid, _request(pid, asset_id))

    assert manifest["status"] == "prepared"
    assert manifest["apply_allowed"] is False
    assert manifest["source"]["asset_id"] == asset_id
    assert manifest["source"]["audio_sha256"] == digest
    assert manifest["source"]["duration_s"] == SECONDS
    assert manifest["source"]["engine"] == "ace-step-1.5"
    assert manifest["source"]["checkpoint"] == "ace_step_1.5_turbo_aio.safetensors"
    assert manifest["source"]["workflow"] == "ace_step_1_5_aio"
    assert manifest["source"]["checkpoint_sha256"] == "67b0f43aa5c51c840bd0228e6a935d8ff416ec87e5df2fc0637da17a561252bc"
    assert manifest["conditioning"]["source_conditioned"] is True
    assert manifest["conditioning"]["repaint_start_s"] == 2.0
    assert manifest["conditioning"]["repaint_end_s"] == 7.0
    assert manifest["boundary"]["start_s"] == START
    assert manifest["boundary"]["end_s"] == END
    assert manifest["handles"] == {
        "source_start_s": START,
        "source_end_s": END,
        "export_start_s": CONTEXT_START,
        "export_end_s": CONTEXT_END,
        "export_duration_s": CONTEXT_END - CONTEXT_START,
        "replacement_offset_in_export_s": START - CONTEXT_START,
        "replacement_end_in_export_s": END - CONTEXT_START,
        "lead_in_s": START - CONTEXT_START,
        "lead_out_s": CONTEXT_END - END,
    }
    assert manifest["lyrics"]["lines"] == BRIDGE_LINES
    assert manifest["lyrics"]["line_count"] == 8
    assert manifest["continuity_label"] == "singer/melody continuity unverified"
    assert manifest["provider_capability"]["supports_longer_replacement"] is False
    form = manifest["provider_request"]["form"]
    assert form["repainting_start"] == "2.0"
    assert form["repainting_end"] == "7.0"
    assert form["lyrics"] == "\n".join(BRIDGE_LINES)
    assert form["seed"] == "20261024"
    assert form["bpm"] == "80"
    assert form["key_scale"] == "C major"
    assert form["time_signature"] == "4"
    assert form["vocal_language"] == "en"
    assert form["audio_format"] == "wav32"
    assert "audio_duration" not in form
    assert manifest["capacity_plan"]["admission_floor_mib"] == 20480
    assert manifest["authorization_request"]["limits"]["attempts"] == 1
    assert manifest["authorization_request"]["limits"]["retries"] == 0
    assert manifest["authorization_request"]["limits"]["apply_to_song"] is False
    assert manifest["authorization_request"]["output_sha256"] is None
    directory = service.store.root / pid / "music" / "replacement-exports" / manifest["export_id"]
    assert (directory / "conditioning_source.wav").is_file()
    assert json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["export_id"] == manifest["export_id"]
    after_state = service._load_state(pid).model_dump(mode="json")
    assert after_state == before_state
    assert (service.store.root / pid / "assets.json").read_bytes() == before_assets


def test_longer_target_is_refused_without_whole_song_substitution(tmp_path, monkeypatch):
    service, pid, asset_id, _digest, _values = _fixture(tmp_path, monkeypatch)
    svc = ReplacementExportService(music=service)
    with pytest.raises(ReplacementExportError, match="longer replacement is unsupported"):
        svc.prepare(pid, _request(pid, asset_id, target_duration_s=7.0))
    root = service.store.root / pid / "music" / "replacement-exports"
    assert not root.exists() or not any(root.iterdir())


def test_review_flags_and_stale_boundary_are_refused(tmp_path, monkeypatch):
    service, pid, asset_id, _digest, _values = _fixture(tmp_path, monkeypatch)
    svc = ReplacementExportService(music=service)
    with pytest.raises(ReplacementExportError, match="target duration"):
        svc.prepare(pid, _request(pid, asset_id, target_duration_reviewed=False))
    with pytest.raises(ReplacementExportError, match="context interval"):
        svc.prepare(pid, _request(pid, asset_id, context_reviewed=False))

    state = service._load_state(pid)
    state.section_boundaries[0].source_sha256 = "f" * 64
    service._save_state(pid, state)
    with pytest.raises(ReplacementExportError, match="stale"):
        svc.prepare(pid, _request(pid, asset_id))


def test_package_candidate_writes_hashes_handles_and_preserves_state(tmp_path, monkeypatch):
    service, pid, asset_id, digest, _values = _fixture(tmp_path, monkeypatch)
    svc = ReplacementExportService(music=service)
    manifest = svc.prepare(pid, _request(pid, asset_id))
    export_id = manifest["export_id"]
    directory = service.store.root / pid / "music" / "replacement-exports" / export_id
    clip, sample_rate, channels = _decode_pcm(directory / "conditioning_source.wav")
    assert sample_rate == SR and channels == CH
    output = array("f", clip)
    start = int(round((START - CONTEXT_START) * sample_rate))
    end = int(round((END - CONTEXT_START) * sample_rate))
    for index in range(start * channels, end * channels):
        output[index] = clip[index] * 0.5
    before_state = service._load_state(pid).model_dump(mode="json")
    before_assets = (service.store.root / pid / "assets.json").read_bytes()
    source_path = service.store.asset_path(pid, asset_id)
    source_before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    packaged = svc.package_candidate(
        pid, export_id, RepaintOutput(samples=output, sample_rate=sample_rate, channels=channels))

    assert packaged["status"] == "candidate"
    output_data = packaged["output"]
    assert output_data["status"] == "candidate"
    assert output_data["export_duration_s"] == CONTEXT_END - CONTEXT_START
    assert output_data["replacement_duration_s"] == TARGET
    assert output_data["provider_output_duration_s"] == CONTEXT_END - CONTEXT_START
    assert output_data["verification"]["pcm_preserved_before_replacement"] is True
    assert output_data["verification"]["pcm_preserved_after_replacement"] is True
    for name in [
        "export_with_handles.wav", "replacement.wav",
        "lead_in_reference.wav", "lead_out_reference.wav",
    ]:
        assert (directory / name).is_file()
        assert output_data["files"][name] == name
    assert output_data["export_sha256"] == hashlib.sha256(
        (directory / "export_with_handles.wav").read_bytes()).hexdigest()
    assert output_data["replacement_sha256"] == hashlib.sha256(
        (directory / "replacement.wav").read_bytes()).hexdigest()
    assert output_data["lead_in_sha256"] == hashlib.sha256(
        (directory / "lead_in_reference.wav").read_bytes()).hexdigest()
    assert output_data["lead_out_sha256"] == hashlib.sha256(
        (directory / "lead_out_reference.wav").read_bytes()).hexdigest()
    checks = (directory / "checksums.sha256").read_text(encoding="utf-8")
    assert "export_with_handles.wav" in checks
    assert "replacement.wav" in checks
    assert service._load_state(pid).model_dump(mode="json") == before_state
    assert (service.store.root / pid / "assets.json").read_bytes() == before_assets
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_before == digest
    with pytest.raises(ReplacementExportError, match="already packaged"):
        svc.package_candidate(
            pid, export_id, RepaintOutput(samples=output, sample_rate=sample_rate, channels=channels))


def test_package_rejects_wrong_duration_and_changed_pcm_outside(tmp_path, monkeypatch):
    service, pid, asset_id, _digest, _values = _fixture(tmp_path, monkeypatch)
    svc = ReplacementExportService(music=service)
    manifest = svc.prepare(pid, _request(pid, asset_id))
    export_id = manifest["export_id"]
    directory = service.store.root / pid / "music" / "replacement-exports" / export_id
    clip, sample_rate, channels = _decode_pcm(directory / "conditioning_source.wav")
    with pytest.raises(ReplacementExportError, match="length differs"):
        svc.package_candidate(
            pid, export_id,
            RepaintOutput(samples=array("f", clip[:-channels]),
                          sample_rate=sample_rate, channels=channels))
    changed = array("f", clip)
    changed[0] = 0.75
    with pytest.raises(ReplacementExportError, match="outside the replacement region"):
        svc.package_candidate(
            pid, export_id,
            RepaintOutput(samples=changed, sample_rate=sample_rate, channels=channels))

def test_api_capability_prepare_get_and_list(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    service, pid, asset_id, _digest, _values = _fixture(tmp_path, monkeypatch)
    music_module._service = service
    replacement_module._service = ReplacementExportService(music=service)
    client = TestClient(create_app())
    capability = client.get(f"/api/music/{pid}/replacement-exports/capability")
    assert capability.status_code == 200
    assert capability.json()["supports_longer_replacement"] is False
    body = _request(pid, asset_id).model_dump(mode="json")
    prepared = client.post(f"/api/music/{pid}/replacement-exports", json=body)
    assert prepared.status_code == 200, prepared.text
    export_id = prepared.json()["export_id"]
    fetched = client.get(f"/api/music/{pid}/replacement-exports/{export_id}")
    assert fetched.status_code == 200
    assert fetched.json()["export_id"] == export_id
    listing = client.get(f"/api/music/{pid}/replacement-exports")
    assert listing.status_code == 200
    assert listing.json()["exports"][0]["export_id"] == export_id
    missing = client.get(f"/api/music/{pid}/replacement-exports/ffffffffffff")
    assert missing.status_code == 404
    refused = client.post(
        f"/api/music/{pid}/replacement-exports",
        json=_request(pid, asset_id, target_duration_s=9.0).model_dump(mode="json"))
    assert refused.status_code == 409
    assert "longer replacement is unsupported" in refused.json()["detail"]
