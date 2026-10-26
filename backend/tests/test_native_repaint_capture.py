"""Offline native ACE-Step repaint capture tests.

Fake HTTP transports prove request-byte identity, lifecycle recording, and
export integrity. No native service is contacted, no model is loaded, and no
live generation occurs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import zipfile
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.config import settings as settings_module
from app.models.music import MusicSettings
from app.models.schemas import AssetKind, ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.music_service import MusicConflict, MusicService
from app.services.profile_store import ProfileStore
from app.services.repaint_provider import AceRepaintClient
from app.services.take_export_service import TakeExportService

SR = 48000
CH = 2
SECONDS = 10.0
FRAMES = int(SECONDS * SR)


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
                repaint_enabled=True,
                repaint_crossfade_ms=10,
                repaint_min_free_vram_mib=1024,
                repaint_output_format="wav32",
            ),
        ),
    )
    ps._store = None
    music_module._service = None


def _settings() -> MusicSettings:
    return MusicSettings(
        musical_direction="Warm acoustic two-step",
        lyrics="[verse]\nA quiet room, a borrowed light.\n",
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
        values.append(0.5 * ((frame % 97) - 48) / 48.0)
        values.append(0.25 * ((frame % 53) - 26) / 26.0)
    return values


def _take(service: MusicService, pid: str) -> tuple[str, array, str]:
    settings = _settings()
    service.save_draft(pid, settings, "source")
    values = _source_values()
    data = music_module._wav_f32_bytes(values, SR, CH)
    digest = hashlib.sha256(data).hexdigest()
    asset = service.store.save_asset(
        pid, AssetKind.music, "source.wav", data,
        meta={"music": {
            "revision": 1,
            "effective_settings": settings.model_dump(mode="json"),
            "audio_sha256": digest,
        }},
    )
    service._append_journal(pid, {
        "kind": "attempt_started", "attempt_id": "source-attempt",
        "at": "2026-10-24T00:00:00+00:00", "revision": 1,
        "revision_name": "source",
        "effective_settings": settings.model_dump(mode="json"),
        "submission_cap": 4,
    })
    service._append_journal(pid, {
        "kind": "attempt_completed", "attempt_id": "source-attempt",
        "at": "2026-10-24T00:01:00+00:00", "asset_id": asset.id,
        "sha256": digest, "prompt_id": "source-prompt",
    })
    service.approve(pid, "approved source", 4)
    return asset.id, values, digest


def _proposal(service: MusicService, pid: str, asset_id: str,
              prompt: str = "make the fiddle gentler") -> str:
    return service.create_repaint_proposal(
        pid, source_asset_id=asset_id, start_s=2.0, end_s=4.0,
        prompt=prompt,
    )["proposal"]["proposal_id"]


def _gate(service: MusicService, pid: str) -> None:
    service.propose_repaint_capacity_gate(
        pid, measured_free_vram_mib=1024, gpu_index=1,
        competing_processes=[], note="isolated measured gate")
    service.approve_repaint_capacity_gate(
        pid, confirm=True, note="isolated approval")


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset(tmp_path, monkeypatch)
    service = MusicService(comfy=object(), router=object())
    pid = service.store.create(ProjectCreate(name="native capture", brief="x")).id
    asset_id, values, digest = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "offline capture authorization")
    _gate(service, pid)
    return service, pid, asset_id, proposal_id, values, digest


def _transport(source_bytes: bytes, *, task_id: str = "task-1",
               mode: str = "ok", counter: dict | None = None,
               captured: list[bytes] | None = None) -> httpx.MockTransport:
    counter = counter if counter is not None else {"release": 0, "query": 0}
    captured = captured if captured is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/release_task":
            counter["release"] += 1
            captured.append(request.content)
            if mode == "connect":
                raise httpx.ConnectError("isolated lost response")
            return httpx.Response(
                200, json={"data": {"task_id": task_id, "status": "queued"}},
                headers={"x-request-id": "fixture"})
        if request.url.path == "/query_result":
            counter["query"] += 1
            if mode == "status2":
                return httpx.Response(200, json={"data": [{
                    "task_id": task_id, "status": 2, "result": "failed"}],
                    "code": 200, "error": None})
            if mode == "empty" and counter["query"] == 1:
                return httpx.Response(200, json={"data": [], "code": 200})
            result = json.dumps([{
                "file": "/v1/audio?path=fixture",
                "status": 1,
                "create_time": 1,
            }])
            return httpx.Response(200, json={"data": [{
                "task_id": task_id, "status": 1, "result": result}],
                "code": 200, "error": None})
        if request.url.path == "/v1/audio":
            return httpx.Response(
                200, content=source_bytes,
                headers={"content-type": "audio/wav",
                         "set-cookie": "do-not-store",
                         "authorization": "do-not-store"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(source_bytes: bytes, *, task_id: str = "task-1",
            mode: str = "ok", counter: dict | None = None,
            captured: list[bytes] | None = None) -> AceRepaintClient:
    return AceRepaintClient(
        "http://native.invalid",
        transport=_transport(
            source_bytes, task_id=task_id, mode=mode,
            counter=counter, captured=captured),
        poll_interval_s=0,
    )


def _request_dir(service: MusicService, pid: str) -> Path:
    return service._state_dir(pid) / "requests"


def _files(service: MusicService, pid: str, attempt: str) -> dict[str, Path]:
    root = _request_dir(service, pid)
    return {
        name: root / f"{attempt}{suffix}"
        for name, suffix in {
            "request": ".native.request.json",
            "body": ".native.body",
            "environment": ".native.environment.json",
            "response": ".native.response.json",
            "response_body": ".native.response.body",
            "status": ".native.status.jsonl",
            "output": ".native.output.json",
            "output_body": ".native.output.body",
            "outcome": ".native.outcome.json",
            "events": ".native.events.jsonl",
        }.items()
    }


def test_native_client_has_no_unrecorded_generate_path():
    assert not hasattr(AceRepaintClient, "generate")


def test_native_exact_body_is_saved_and_transmitted_with_lifecycle(tmp_path, monkeypatch):
    service, pid, asset_id, proposal_id, _values, digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    captured: list[bytes] = []
    counter = {"release": 0, "query": 0}
    client = _client(source_bytes, captured=captured, counter=counter)
    result = service.submit_repaint(pid, proposal_id, provider=client)
    attempt = result["attempt_id"]
    directory = _request_dir(service, pid)
    files = _files(service, pid, attempt)
    assert result["status"] == "candidate"
    assert counter == {"release": 1, "query": 1}
    assert captured == [files["body"].read_bytes()]
    request = json.loads(files["request"].read_text(encoding="utf-8"))
    assert request["schema"] == "song_writer.native_repaint_request.v1"
    assert request["provider_kind"] == "native_ace_step_repaint"
    assert request["explicitly_not_comfyui"] is True
    assert request["serialization"]["frozen_before_dispatch"] is True
    assert request["serialization"]["rebuilt_for_dispatch"] is False
    assert request["cancellation"]["supported"] is False
    assert request["cancellation"]["status"] == "unavailable"
    assert request["source"]["sha256"] == digest
    assert request["conditioning"]["sha256"] == digest
    assert request["conditioning"]["duration_s"] == SECONDS
    assert request["conditioning"]["sample_rate"] == SR
    assert request["conditioning"]["channels"] == CH
    assert request["conditioning"]["source_mapping"] == {
        "clip_start_s": 0.0,
        "clip_end_s": SECONDS,
        "mask_start_s": 2.0,
        "mask_end_s": 4.0,
        "clip_offset_s": 0.0,
        "conditioning_is_full_source": True,
    }
    assert request["request_body_sha256"] == hashlib.sha256(captured[0]).hexdigest()
    assert request["fields"]["task_type"] == "repaint"
    assert request["fields"]["lyrics"] == _settings().lyrics
    assert request["fields"]["repainting_start"] == "2.0"
    assert request["fields"]["repainting_end"] == "4.0"
    assert request["fields"]["audio_format"] == "wav32"
    assert request["fields"]["seed"] == "20261024"
    assert request["fields"]["bpm"] == "80"
    assert request["fields"]["key_scale"] == "C major"
    assert request["fields"]["time_signature"] == "4"
    assert request["fields"]["vocal_language"] == "en"
    assert request["field_order"] == [
        "task_type", "prompt", "repainting_start", "repainting_end",
        "chunk_mask_mode", "repaint_mode", "audio_cover_strength",
        "audio_format", "lyrics", "seed", "use_random_seed", "bpm",
        "key_scale", "time_signature", "vocal_language",
    ]
    assert b'name="src_audio"' in captured[0]
    assert b'name="lyrics"' in captured[0]
    assert b"Maybe" not in captured[0]
    assert (directory / f"{attempt}.native.conditioning.wav").read_bytes() == source_bytes
    assert hashlib.sha256(
        (directory / f"{attempt}.native.conditioning.wav").read_bytes()).hexdigest() == digest
    response = json.loads(files["response"].read_text(encoding="utf-8"))
    assert response["status_code"] == 200
    assert response["task_id"] == "task-1"
    assert "set-cookie" not in {key.lower() for key in response["headers"]}
    assert "authorization" not in {key.lower() for key in response["headers"]}
    assert files["response_body"].is_file()
    assert len(files["status"].read_text(encoding="utf-8").splitlines()) == 1
    output = json.loads(files["output"].read_text(encoding="utf-8"))
    assert output["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert files["output_body"].read_bytes() == source_bytes
    outcome = json.loads(files["outcome"].read_text(encoding="utf-8"))
    assert outcome["status"] == "completed"
    assert outcome["links"]["native_request_path"]
    journal = service._journal_state(pid)
    completed = next(row for row in journal["rows"]
                     if row.get("kind") == "attempt_completed"
                     and row.get("attempt_id") == attempt)
    assert completed["task_id"] == "task-1"
    assert completed["native_request_body_sha256"] == request["request_body_sha256"]
    assert journal["submissions"] == 2
    assert service.store.asset_path(pid, asset_id).read_bytes() == source_bytes


def test_native_request_persistence_failure_blocks_dispatch_and_consumes_no_cap(
    tmp_path, monkeypatch,
):
    service, pid, _asset_id, proposal_id, values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, _asset_id).read_bytes()
    counter = {"release": 0, "query": 0}
    client = _client(source_bytes, counter=counter)

    def fail(*_args, **_kwargs):
        raise RuntimeError("isolated request storage failure")

    monkeypatch.setattr(service, "_persist_native_request", fail)
    with pytest.raises(MusicConflict, match="persistence failed"):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter == {"release": 0, "query": 0}
    assert service._journal_state(pid)["submissions"] == 1
    proposal = service.repaint_proposal(pid, proposal_id)
    assert proposal["status"] == "authorized"
    assert not list(_request_dir(service, pid).glob("*.native.body.tmp-*"))


def test_native_stale_source_is_rejected_before_dispatch(tmp_path, monkeypatch):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_path = service.store.asset_path(pid, asset_id)
    source_path.write_bytes(source_path.read_bytes() + b"changed")
    counter = {"release": 0, "query": 0}
    client = _client(source_path.read_bytes(), counter=counter)
    with pytest.raises(MusicConflict, match="source bytes changed"):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter == {"release": 0, "query": 0}
    assert service._journal_state(pid)["submissions"] == 1


def test_native_provider_failure_status_is_linked_and_consumes_one_allowance(
    tmp_path, monkeypatch,
):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    counter = {"release": 0, "query": 0}
    client = _client(source_bytes, mode="status2", counter=counter)
    with pytest.raises(MusicConflict, match="submission failed"):
        service.submit_repaint(pid, proposal_id, provider=client)
    proposal = service.repaint_proposal(pid, proposal_id)
    assert proposal["status"] == "failed"
    attempt = proposal["attempt_id"]
    files = _files(service, pid, attempt)
    assert files["response"].is_file()
    assert files["status"].read_text(encoding="utf-8").splitlines()
    assert files["outcome"].is_file()
    assert json.loads(files["outcome"].read_text(encoding="utf-8"))["status"] == "failed"
    assert not files["output"].exists()
    assert service._journal_state(pid)["submissions"] == 2
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter["release"] == 1


def test_native_empty_queue_is_recorded_before_success(tmp_path, monkeypatch):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    counter = {"release": 0, "query": 0}
    client = _client(source_bytes, mode="empty", counter=counter)
    result = service.submit_repaint(pid, proposal_id, provider=client)
    assert result["status"] == "candidate"
    assert counter == {"release": 1, "query": 2}
    status_rows = _files(service, pid, result["attempt_id"])["status"].read_text(
        encoding="utf-8").splitlines()
    assert len(status_rows) == 2
    first = json.loads(status_rows[0])
    assert first["json"]["data"] == []
    assert first["row"] is None


def test_native_timeout_is_uncertain_links_request_and_does_not_retry(
    tmp_path, monkeypatch,
):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    counter = {"release": 0, "query": 0}
    client = _client(source_bytes, mode="connect", counter=counter)
    with pytest.raises(MusicConflict, match="uncertainty hold"):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter == {"release": 1, "query": 0}
    assert service._load_state(pid).recovery_required is True
    proposal = service.repaint_proposal(pid, proposal_id)
    assert proposal["status"] == "uncertain"
    attempt = proposal["attempt_id"]
    files = _files(service, pid, attempt)
    assert files["request"].is_file()
    assert files["outcome"].is_file()
    uncertain = next(row for row in service._journal_state(pid)["rows"]
                     if row.get("kind") == "attempt_uncertain")
    assert uncertain["native_request_path"] == str(
        files["request"].relative_to(service.store.root / pid)).replace("\\", "/")
    with pytest.raises(MusicConflict, match="recovery is required"):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter == {"release": 1, "query": 0}
    recovered = service.recover_repaint(pid, proposal_id, True)
    assert recovered["recovery_required"] is False
    assert service.repaint_proposal(pid, proposal_id)["status"] == "failed"
    assert any(row.get("kind") == "recovery_confirmed"
               for row in service._journal_state(pid)["rows"])


def test_native_post_dispatch_recording_failure_is_uncertain(
    tmp_path, monkeypatch,
):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    counter = {"release": 0, "query": 0}

    def fail_submission(*_args, **_kwargs):
        raise OSError("isolated response storage failure")

    monkeypatch.setattr(service, "_persist_native_submission", fail_submission)
    client = _client(source_bytes, counter=counter)
    with pytest.raises(MusicConflict, match="uncertainty hold"):
        service.submit_repaint(pid, proposal_id, provider=client)
    assert counter["release"] == 1
    assert service.repaint_proposal(pid, proposal_id)["status"] == "uncertain"
    assert service._load_state(pid).recovery_required is True
    assert service._journal_state(pid)["submissions"] == 2


def test_native_concurrent_attempts_have_isolated_immutable_records(
    tmp_path, monkeypatch,
):
    service, pid, asset_id, _proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    first = _proposal(service, pid, asset_id, prompt="first isolated prompt")
    second = _proposal(service, pid, asset_id, prompt="second isolated prompt")
    service.authorize_repaint(pid, first, "first authorization")
    service.authorize_repaint(pid, second, "second authorization")
    first_bodies: list[bytes] = []
    second_bodies: list[bytes] = []
    clients = [
        _client(source_bytes, task_id="task-a", captured=first_bodies),
        _client(source_bytes, task_id="task-b", captured=second_bodies),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda item: service.submit_repaint(pid, item[0], provider=item[1]),
            [(first, clients[0]), (second, clients[1])]))
    attempts = [item["attempt_id"] for item in results]
    assert attempts[0] != attempts[1]
    first_record = json.loads(_files(service, pid, attempts[0])["request"].read_text(
        encoding="utf-8"))
    second_record = json.loads(_files(service, pid, attempts[1])["request"].read_text(
        encoding="utf-8"))
    assert first_record["request_body_sha256"] != second_record["request_body_sha256"]
    assert first_bodies[0] == _files(service, pid, attempts[0])["body"].read_bytes()
    assert second_bodies[0] == _files(service, pid, attempts[1])["body"].read_bytes()
    assert hashlib.sha256(first_bodies[0]).hexdigest() == first_record["request_body_sha256"]
    assert hashlib.sha256(second_bodies[0]).hexdigest() == second_record["request_body_sha256"]
    with pytest.raises(MusicConflict):
        service._write_exclusive_bytes(
            _files(service, pid, attempts[0])["body"], b"collision")


def test_native_export_includes_exact_records_and_verifies_hashes(
    tmp_path, monkeypatch,
):
    service, pid, asset_id, proposal_id, _values, _digest = _setup(tmp_path, monkeypatch)
    source_bytes = service.store.asset_path(pid, asset_id).read_bytes()
    client = _client(source_bytes, captured=[])
    result = service.submit_repaint(pid, proposal_id, provider=client)
    attempt = result["attempt_id"]
    export = TakeExportService(
        music=service, profiles=ProfileStore(tmp_path / "profiles"))
    audit = export.audit(pid, result["candidate"]["master_asset_id"])["audit"]
    assert audit["execution"]["provider_kind"] == "native_ace_step_repaint"
    assert audit["execution"]["native_request_retained"] is True
    assert audit["execution"]["exact_native_request_available"] is True
    assert audit["execution"]["native_task_id"] == "task-1"
    assert "native_loaded_model_bytes_verified" in audit["unavailable"]
    assert "native_cancellation_request_response" in audit["unavailable"]
    assert "comfyui_history_for_prompt" not in audit["unavailable"]
    assert audit["retained"]["native_conditioning"]["sha256"] == audit["source_audio"]["sha256"]
    content, filename, digest = export.package(pid, result["candidate"]["master_asset_id"])
    assert filename.startswith("take-")
    assert digest
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        required = {
            "manifest.json", "hashes.sha256", "execution/journal.jsonl",
            "execution/native_request.json",
            "execution/records/native_request_metadata.json",
            "execution/records/native_request_body.body",
            "execution/records/native_environment.json",
            "execution/records/native_response_metadata.json",
            "execution/records/native_response_body.body",
            "execution/records/native_status.jsonl",
            "execution/records/native_output.json",
            "execution/records/native_output_body.body",
            "execution/records/native_outcome.json",
            "execution/records/native_events.jsonl",
            "execution/records/native_conditioning.wav",
        }
        assert required.issubset(names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["execution"]["provider_kind"] == "native_ace_step_repaint"
        assert manifest["native_exact_execution_files"]
        assert manifest["exact_execution_files"]
        assert "submission_cap" not in manifest
        assert "approval" not in manifest
        assert "native_loaded_model_bytes_verified" in manifest["unavailable"]
        request = json.loads(archive.read(
            "execution/records/native_request_metadata.json"))
        assert request["provider_kind"] == "native_ace_step_repaint"
        assert request["request_body_sha256"] == hashlib.sha256(
            archive.read("execution/records/native_request_body.body")).hexdigest()
        assert request["serialization"]["boundary"] in archive.read(
            "execution/records/native_request_body.body").decode("utf-8", "replace")
        assert "submission_cap" not in json.dumps(request)
        package_text = "\n".join(
            archive.read(name).decode("utf-8", "replace")
            for name in names if not name.endswith((".wav", ".mp3", ".body")))
        assert "do-not-store" not in package_text
        assert "set-cookie" not in package_text.lower()
        for line in archive.read("hashes.sha256").decode("utf-8").splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        journal = archive.read("execution/journal.jsonl").decode("utf-8")
        assert "submission_cap" not in journal
        assert attempt in journal
