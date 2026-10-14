"""Phase 1 audiobook slice tests.

All synthesis is a deterministic in-memory fake; no TTS service is contacted.
Assembly exercises the configured FFmpeg binary for MP3 only. Tests run against
isolated temporary project roots.
"""
from __future__ import annotations

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
from app.services.audiobook import AudiobookError, RecoveryBlocked, get_audiobook

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

    # removing a span leaves a gap
    spans, exclusions = build_annotations()
    with pytest.raises(AudiobookError) as gap:
        service.set_annotations(pid, [s for s in spans if s.id != "s2"], exclusions)
    assert "gap" in str(gap.value)

    # reordering is rejected
    with pytest.raises(AudiobookError) as order:
        service.set_annotations(pid, list(reversed(spans)), exclusions)
    assert "increasing source order" in str(order.value)

    # overlap is rejected
    overlapping = [spans[0].model_copy(update={"end_char": spans[1].end_char}), *spans[1:]]
    with pytest.raises(AudiobookError) as overlap:
        service.set_annotations(pid, overlapping, exclusions)
    assert "overlap" in str(overlap.value)

    # the exact, complete set is accepted
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
    assert first == second and len(first) == 3  # one chunk per short span

    import asyncio
    synth = FakeSynth()
    asyncio.run(service.render(pid, synth=synth))
    assert len(synth.calls) == 3

    # Reassigning only s2 changes only s2's chunk; rendered audio is carried over
    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"speaker": "narrator"})
    service.set_annotations(pid, spans, exclusions)
    state = service.plan_chunks(pid)
    rendered_ids = {c.span_id for c in state.chunks if c.status.value == "rendered"}
    assert rendered_ids == {"s1", "s3"}

    synth2 = FakeSynth()
    result = asyncio.run(service.render(pid, synth=synth2))
    assert result["rendered"] == 1  # only the changed s2 chunk
    assert synth2.calls[0][1] == "af_heart"  # narrator voice


def test_render_resumes_and_skips_rendered(service):
    import asyncio
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))
    second = FakeSynth()
    result = asyncio.run(service.render(pid, synth=second))
    assert result["rendered"] == 0 and second.calls == []


def test_cancellation_unconfirmed_blocks_then_clear_recovers(service):
    import asyncio
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)

    counter = {"n": 0}

    async def synth(text, voice, speed):
        counter["n"] += 1
        return _wav(text)

    result = asyncio.run(service.render(
        pid, synth=synth, should_cancel=lambda: counter["n"] >= 1, confirm_stopped=False))
    assert result["cancelled"] is True and result["blocked"] is True

    with pytest.raises(RecoveryBlocked):
        asyncio.run(service.render(pid, synth=FakeSynth()))

    service.clear_recovery(pid, "operator confirmed the TTS worker is idle")
    final = asyncio.run(service.render(pid, synth=FakeSynth()))
    assert final["blocked"] is False
    assert all(c["status"] == "rendered" for c in final["state"]["chunks"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required for MP3 delivery")
def test_assemble_wav_and_mp3_and_byte_bound_approval(service):
    import asyncio
    pid = make_project(service)
    prepare(service, pid)
    service.plan_chunks(pid)
    asyncio.run(service.render(pid, synth=FakeSynth()))

    state = service.assemble(pid, pause_ms=200)
    base = service._base(pid)
    assert (base / "master.wav").is_file() and (base / "master.mp3").is_file()
    assert state.master.wav_sha256 and state.master.mp3_sha256

    assert service.verify_approval(pid)["approved"] is False  # not approved yet
    service.approve(pid)
    assert service.verify_approval(pid)["approved"] is True

    # tampering with the MP3 bytes invalidates approval
    with (base / "master.mp3").open("ab") as handle:
        handle.write(b"\x00")
    check = service.verify_approval(pid)
    assert check["approved"] is False
    assert any("mp3 bytes changed" in reason for reason in check["reasons"])

    # re-approve, then change an annotation: approval is invalidated
    service.approve(pid)
    assert service.verify_approval(pid)["approved"] is True
    spans, exclusions = build_annotations()
    spans[1] = spans[1].model_copy(update={"speaker": "narrator"})
    service.set_annotations(pid, spans, exclusions)
    assert service.verify_approval(pid)["approved"] is False


def test_approve_requires_a_master(service):
    pid = make_project(service)
    prepare(service, pid)
    with pytest.raises(AudiobookError):
        service.approve(pid)


def test_api_editor_surface(client):
    pid = client.post("/api/projects", json={"name": "api-book", "brief": "b"}).json()["id"]

    imported = client.post(f"/api/audiobook/{pid}/chapter", json={"text": CHAPTER})
    assert imported.status_code == 200, imported.text
    assert imported.json()["manuscript"]["locked"] is True

    spoken = client.put(f"/api/audiobook/{pid}/speakers",
                        json={"speakers": [s.model_dump() for s in SPEAKERS]})
    assert spoken.status_code == 200, spoken.text

    spans, exclusions = build_annotations()
    annotated = client.put(f"/api/audiobook/{pid}/annotations", json={
        "spans": [s.model_dump() for s in spans], "exclusions": [e.model_dump() for e in exclusions]})
    assert annotated.status_code == 200, annotated.text

    bad = client.put(f"/api/audiobook/{pid}/annotations", json={
        "spans": [s.model_dump() for s in spans if s.id != "s2"], "exclusions": [e.model_dump() for e in exclusions]})
    assert bad.status_code == 409

    planned = client.post(f"/api/audiobook/{pid}/chunks/plan")
    assert planned.status_code == 200, planned.text
    assert len(planned.json()["chunks"]) == 3

    status = client.get(f"/api/audiobook/{pid}/status")
    assert status.status_code == 200
    assert status.json()["coverage_errors"] == []
    assert status.json()["verification"]["approved"] is False
