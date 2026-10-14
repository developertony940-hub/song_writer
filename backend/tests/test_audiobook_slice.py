"""Phase 1 audiobook slice tests.

All synthesis is a deterministic in-memory fake; no TTS service is contacted.
Assembly exercises the configured FFmpeg binary for WAV master + MP3 delivery.

Evidence classification:
  * Simulated: every synthesis here is fake PCM. These tests prove software
    behavior (authorization hold, coverage, deterministic chunks, cancellation
    recovery, cross-project/restart blocking, stale-assembly rejection, and
    byte-bound listen-back approval). They prove nothing about audio quality.
  * Live quality (three distinct human voices, naturalness, loudness) requires a
    separately authorized listening test and is NOT covered here.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from app.models.audiobook import (
    Exclusion,
    ImportChapterRequest,
    Speaker,
    SpeakerSpan,
)
from app.models.schemas import ProjectCreate
from app.services.audiobook import (
    AudiobookError,
    GenerationNotAuthorized,
    RecoveryBlocked,
    get_audiobook,
)
from app.services.model_router import CapacityHeld, HoldPersistenceError, ModelRouter

CHAPTER = (
    "CHAPTER ONE\n"
    "The rain fell hard that night.\n"
    '"Is anyone there?" Marcus called.\n'
    '"I am here," said Elena.\n'
)
SPEAKERS = [
    Speaker(name="narrator", voice="af_heart"),
    Speaker(name="marcus", voice="am_michael"),
    Speaker(name="elena", voice="af_bella"),
]
NEEDLES = [
    ("s1", "narrator", "The rain fell hard that night.\n"),
    ("s2", "marcus", '"Is anyone there?" Marcus called.\n'),
    ("s3", "elena", '"I am here," said Elena.\n'),
]
EVIDENCE = "operator log: worker PID 4321 exited; /queue length 0 verified at 12:00 UTC"


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as settings_module
    from app.config.settings import get_settings
    from app.services import orchestrator as orch_module
    from app.services import project_store as ps
    from app.services.audiobook import service as audiobook_service
    from app.services.model_router import reset_router

    monkeypatch.setattr(settings_module, "_cached",
                        dataclasses.replace(get_settings(), projects_root=tmp_path))
    ps._store = None
    orch_module._orch = None
    audiobook_service._service = None
    reset_router()


def _restart():
    """Simulate a process restart: rebuild the router and service singletons.

    ``projects_root`` is unchanged, so a persisted capacity hold must be
    reloaded from disk rather than kept in memory.
    """
    from app.services import audiobook as audiobook_pkg
    from app.services.model_router import reset_router

    reset_router()
    audiobook_pkg.service._service = None
    return get_audiobook()


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset(tmp_path, monkeypatch)
    return get_audiobook()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as result:
        yield result


def make_project(service) -> str:
    return service.store.create(ProjectCreate(name="book", brief="b")).id


def build_annotations():
    spans, exclusions = [], []
    start = CHAPTER.index("CHAPTER ONE")
    end = CHAPTER.index("\n", start) + 1
    exclusions.append(Exclusion(start_char=start, end_char=end, reason="chapter heading"))
    for span_id, speaker, needle in NEEDLES:
        index = CHAPTER.index(needle)
        spans.append(SpeakerSpan(id=span_id, speaker=speaker, start_char=index, end_char=index + len(needle)))
    return spans, exclusions


def prepare(service, pid):
    service.import_chapter(pid, ImportChapterRequest(text=CHAPTER))
    service.set_speakers(pid, SPEAKERS)
    spans, exclusions = build_annotations()
    service.set_annotations(pid, spans, exclusions)


def _wav(text: str, sample_rate: int = 24000) -> bytes:
    frames = max(1, int(sample_rate * (0.03 + 0.004 * len(text))))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * (i / sample_rate))))
            for i in range(frames)
        ))
    return buffer.getvalue()


class FakeSynth:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    async def __call__(self, text: str, voice: str, speed: float) -> bytes:
        self.calls.append((text, voice, speed))
        return _wav(text)


def _cancel_after_one(service, pid):
    """Render once then cancel without confirming stop; returns the result."""
    counter = {"n": 0}

    async def synth(text, voice, speed):
        counter["n"] += 1
        return _wav(text)

    return asyncio.run(service.render(
        pid, synth=synth, should_cancel=lambda: counter["n"] >= 1, confirm_stopped=False))


# --- import, coverage, planning ------------------------------------------

def test_import_locks_chapter_and_records_hash(service):
    pid = make_project(service)
    state = service.import_chapter(pid, ImportChapterRequest(text=CHAPTER))
    assert state.manuscript.locked is True
    assert state.manuscript.char_count == len(CHAPTER)
    assert state.manuscript.sha256
    with pytest.raises(AudiobookError):
        service.import_chapter(pid, ImportChapterRequest(text="different"))


def test_coverage_requires_complete_ordered_spans(service):
    pid = make_project(service)
    prepare(service, pid)

    spans, exclusions = build_annotations()
    with pytest.raises(AudiobookError) as gap:
        service.set_annotations(pid, [s for s in spans if s.id != "s2"], exclusions)
    assert "gap" in str(gap.value)

    with pytest.raises(AudiobookError) as order:
        service.set_annotations(pid, list(reversed(spans)), exclusions)
    assert "increasing source order" in str(order.value)

    overlapping = [spans[0].model_copy(update={"end_char": spans[1].end_char}), *spans[1:]]
    with pytest.raises(AudiobookError) as overlap:
        service.set_annotations(pid, overlapping, exclusions)
    assert "overlap" in str(overlap.value)

    service.set_annotations(pid, spans, exclusions)


def test_unresolved_attribution_blocks_plan_and_render(service):
    pid = make_project(service)
    prepare(service, pid)
    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"needs_review": True, "confidence": 0.4})
    service.set_annotations(pid, spans, exclusions)
    with pytest.raises(AudiobookError):
        service.plan_chunks(pid)


def test_deterministic_chunks_and_selective_regeneration(service):
    pid = make_project(service)
    prepare(service, pid)
    first = [c.id for c in service.plan_chunks(pid).chunks]
    second = [c.id for c in service.plan_chunks(pid).chunks]
    assert first == second and len(first) == 3

    synth = FakeSynth()
    asyncio.run(service.render(pid, synth=synth))
    assert len(synth.calls) == 3

    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"speaker": "narrator"})
    service.set_annotations(pid, spans, exclusions)
    state = service.plan_chunks(pid)
    rendered_ids = {c.span_id for c in state.chunks if c.status.value == "rendered"}
    assert rendered_ids == {"s1", "s3"}

    synth2 = FakeSynth()
    result = asyncio.run(service.render(pid, synth=synth2))
    assert result["rendered"] == 1
    assert synth2.calls[0][1] == "af_heart"


def test_render_resumes_and_skips_rendered(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))
    second = FakeSynth()
    result = asyncio.run(service.render(pid, synth=second))
    assert result["rendered"] == 0 and second.calls == []


def test_three_distinct_voices_are_dispatched(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    synth = FakeSynth()
    asyncio.run(service.render(pid, synth=synth))
    assert {call[1] for call in synth.calls} == {"af_heart", "am_michael", "af_bella"}


# --- live-generation hold -------------------------------------------------

def test_real_render_refused_while_generation_unauthorized(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    # No injected synth => a real render request, which must be refused.
    with pytest.raises(GenerationNotAuthorized):
        asyncio.run(service.render(pid))
    # Fake synthesis remains available for isolated verification.
    result = asyncio.run(service.render(pid, synth=FakeSynth()))
    assert result["rendered"] == 3


def test_real_render_allowed_when_explicitly_authorized(service, monkeypatch):
    from app.config import settings as settings_module
    from app.config.settings import get_settings

    base = get_settings()
    monkeypatch.setattr(settings_module, "_cached", dataclasses.replace(
        base, audiobook=dataclasses.replace(base.audiobook, live_generation_authorized=True)))

    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    calls = []

    async def fake_synth(text, voice, speed):
        calls.append((text, voice))
        return _wav(text)

    monkeypatch.setattr(service.tts, "synthesize", fake_synth)
    result = asyncio.run(service.render(pid))
    assert result["rendered"] == 3 and len(calls) == 3


# --- cancellation / recovery safety --------------------------------------

def test_unconfirmed_cancel_blocks_cross_project_and_survives_restart(service):
    pid1 = make_project(service)
    prepare(service, pid1)
    service.plan_chunks(pid1)
    result = _cancel_after_one(service, pid1)
    assert result["cancelled"] is True and result["blocked"] is True

    # Recorded on the shared router, not just this project's state file.
    hold = service.router.held("kokoro-82m")
    assert hold is not None and hold["holder"] == pid1
    assert (service.store.root / ".router_holds.json").is_file()

    # A competing project cannot acquire the same capacity.
    pid2 = make_project(service)
    prepare(service, pid2)
    service.plan_chunks(pid2)
    with pytest.raises(RecoveryBlocked):
        asyncio.run(service.render(pid2, synth=FakeSynth()))

    # The hold is on disk, so a simulated restart still blocks acquisition.
    restarted = _restart()
    with pytest.raises(RecoveryBlocked):
        asyncio.run(restarted.render(pid2, synth=FakeSynth()))


def test_clear_recovery_requires_documented_stop_and_drain_evidence(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    _cancel_after_one(service, pid)

    # A bare acknowledgement / missing drain / thin evidence are all refused.
    with pytest.raises(AudiobookError):
        service.clear_recovery(pid, worker_stopped=True, queue_drained=False, evidence=EVIDENCE)
    with pytest.raises(AudiobookError):
        service.clear_recovery(pid, worker_stopped=True, queue_drained=True, evidence="ok")
    assert service.router.held("kokoro-82m") is not None

    state = service.clear_recovery(pid, worker_stopped=True, queue_drained=True, evidence=EVIDENCE)
    assert state.recovery.blocked is False
    assert service.router.held("kokoro-82m") is None

    # Capacity is usable again across projects.
    pid2 = make_project(service)
    prepare(service, pid2)
    service.plan_chunks(pid2)
    assert asyncio.run(service.render(pid2, synth=FakeSynth()))["rendered"] == 3


# --- shared-router validation (single-process) ---------------------------
#
# The launch configuration is single-process (run.ps1: uvicorn without
# --workers). These checks prove in-process behavior only; they do NOT claim
# cross-process safety (see closeout report).

def test_social_video_acquisition_unaffected_without_holds(service):
    from app.services.model_router import get_router

    router = get_router()
    assert router.holds() == {}
    prof = asyncio.run(router.acquire("wan21_t2v_14b_fp8", "vid-1"))
    assert prof.slot.value == 1
    assert router.status()["slots"]["1"]["current_model"] == "wan21_t2v_14b_fp8"
    asyncio.run(router.release("wan21_t2v_14b_fp8", "vid-1"))
    asyncio.run(router.acquire("flux_dev_fp8", "img-1"))
    asyncio.run(router.release("flux_dev_fp8", "img-1"))


def test_held_capacity_rejects_social_video_and_isolates_other_slot(service):
    from app.services.model_router import get_router

    router = get_router()
    router.hold("wan21_t2v_14b_fp8", "held-job", reason="unconfirmed cancel", holder="proj-x")
    # A competing acquisition on the held GPU1 slot is refused.
    with pytest.raises(CapacityHeld):
        asyncio.run(router.acquire("flux_dev_fp8", "img-2"))
    # The unrelated GPU0 slot is unaffected.
    asyncio.run(router.acquire("kokoro-82m", "tts-1"))
    asyncio.run(router.release("kokoro-82m", "tts-1"))
    # Clearing the hold restores acquisition.
    router.clear_hold("wan21_t2v_14b_fp8", holder="proj-x")
    asyncio.run(router.acquire("wan21_t2v_14b_fp8", "vid-3"))
    asyncio.run(router.release("wan21_t2v_14b_fp8", "vid-3"))


def test_hold_file_write_failure_fails_safe(tmp_path):
    # A directory where the hold file should be makes every write fail.
    blocked = tmp_path / "holds-as-dir"
    blocked.mkdir()
    router = ModelRouter(holds_path=blocked)
    with pytest.raises(HoldPersistenceError):
        router.hold("kokoro-82m", "job", reason="unconfirmed cancel", holder="p1")
    # The in-memory hold remains, so this process still refuses acquisition.
    assert router.held("kokoro-82m") is not None
    with pytest.raises(CapacityHeld):
        asyncio.run(router.acquire("kokoro-82m", "job2"))


def test_hold_file_read_failure_fails_closed(tmp_path):
    bad = tmp_path / "holds.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    router = ModelRouter(holds_path=bad)
    assert router.holds_unreadable() is True
    with pytest.raises(CapacityHeld):
        asyncio.run(router.acquire("kokoro-82m", "job"))


# --- stale-assembly rejection --------------------------------------------

def test_stale_chunks_cannot_enter_assembly(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))

    # (1) annotations changed without re-planning: the chunk set is stale.
    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"speaker": "narrator"})
    service.set_annotations(pid, spans, exclusions)
    with pytest.raises(AudiobookError) as stale:
        service.assemble(pid)
    assert "stale" in str(stale.value)

    # (2) current chunks, but a rendered chunk file was swapped on disk.
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))
    state = service.load(pid)
    target = service._base(pid) / state.chunks[0].audio_file
    target.write_bytes(_wav("tampered chunk bytes"))
    with pytest.raises(AudiobookError) as changed:
        service.assemble(pid)
    assert "changed since it was rendered" in str(changed.value)


# --- assembly, listen-back, approval, verification -----------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required for MP3 delivery")
def test_listen_before_approve_and_byte_bound_approval(service):
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))

    state = service.assemble(pid, pause_ms=200)
    base = service._base(pid)
    assert (base / "master.wav").is_file() and (base / "master.mp3").is_file()
    assert state.master.wav_sha256 and state.master.mp3_sha256
    assert service.verify_approval(pid)["approved"] is False

    # Approval may not precede listening.
    with pytest.raises(AudiobookError):
        service.approve(pid)
    # A review must attest to both files.
    with pytest.raises(AudiobookError):
        service.review(pid, listened_wav=True, listened_mp3=False)

    service.review(pid, listened_wav=True, listened_mp3=True, note="listened to both")
    service.approve(pid)
    assert service.verify_approval(pid)["approved"] is True

    # Changing the delivered bytes invalidates the review and the approval.
    with (base / "master.mp3").open("ab") as handle:
        handle.write(b"\x00")
    check = service.verify_approval(pid)
    assert check["approved"] is False
    assert any("mp3 bytes changed" in reason for reason in check["reasons"])
    with pytest.raises(AudiobookError):
        service.approve(pid)  # review no longer matches current bytes

    # Re-review then re-approve succeeds.
    service.review(pid, listened_wav=True, listened_mp3=True)
    service.approve(pid)
    assert service.verify_approval(pid)["approved"] is True

    # Changing an annotation invalidates approval.
    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"speaker": "narrator"})
    service.set_annotations(pid, spans, exclusions)
    assert service.verify_approval(pid)["approved"] is False


def test_approve_requires_a_master(service):
    pid = make_project(service)
    prepare(service, pid)
    with pytest.raises(AudiobookError):
        service.approve(pid)


# --- API surface ----------------------------------------------------------

def test_api_editor_surface(client):
    pid = client.post("/api/projects", json={"name": "api-book", "brief": "b"}).json()["id"]

    imported = client.post(f"/api/audiobook/{pid}/chapter", json={"text": CHAPTER})
    assert imported.status_code == 200, imported.text
    assert imported.json()["manuscript"]["locked"] is True

    shown = client.get(f"/api/audiobook/{pid}/chapter")
    assert shown.status_code == 200 and shown.json()["text"] == CHAPTER

    spoken = client.put(f"/api/audiobook/{pid}/speakers",
                        json={"speakers": [s.model_dump() for s in SPEAKERS]})
    assert spoken.status_code == 200, spoken.text

    spans, exclusions = build_annotations()
    annotated = client.put(f"/api/audiobook/{pid}/annotations", json={
        "spans": [s.model_dump() for s in spans], "exclusions": [e.model_dump() for e in exclusions]})
    assert annotated.status_code == 200, annotated.text

    bad = client.put(f"/api/audiobook/{pid}/annotations", json={
        "spans": [s.model_dump() for s in spans if s.id != "s2"],
        "exclusions": [e.model_dump() for e in exclusions]})
    assert bad.status_code == 409

    planned = client.post(f"/api/audiobook/{pid}/chunks/plan")
    assert planned.status_code == 200, planned.text
    assert len(planned.json()["chunks"]) == 3

    status = client.get(f"/api/audiobook/{pid}/status")
    assert status.status_code == 200
    assert status.json()["coverage_errors"] == []
    assert status.json()["verification"]["approved"] is False
    assert status.json()["live_generation_authorized"] is False

    # A real render request is refused server-side (403), not merely hidden.
    refused = client.post(f"/api/audiobook/{pid}/render", json={"force": False})
    assert refused.status_code == 403, refused.text
