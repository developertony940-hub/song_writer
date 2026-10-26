"""Offline proof for the bounded audition journal and runner.

All synthesis and HTTP calls here are fakes. No TTS service, model service, or
production project directory is contacted or written.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from app.models.audiobook import Chunk, ImportChapterRequest, Speaker, SpeakerSpan
from app.models.schemas import ProjectCreate
from app.services.audiobook.audition import (
    Attempt,
    AuditionCapReached,
    AuditionJournal,
    AuditionRunner,
    AuditionStopped,
    AuditionUncertain,
)
from app.services.audiobook.service import GenerationNotAuthorized, get_audiobook
from app.services.tts_client import TTSClient

MANUSCRIPT = (
    "The studio lights dimmed as the last take loaded.\n"
    '"Tell me we are still recording," Marcus asked.\n'
    '"We are," Elena said, "and the room is finally quiet."\n'
    "Outside, the rain kept time on the glass.\n"
)
SPEAKERS = [
    Speaker(name="narrator", voice="af_heart"),
    Speaker(name="marcus", voice="am_michael"),
    Speaker(name="elena", voice="af_bella"),
]
SPANS = [
    SpeakerSpan(id="n1", speaker="narrator", start_char=0, end_char=50),
    SpeakerSpan(id="m1", speaker="marcus", start_char=50, end_char=98),
    SpeakerSpan(id="e1", speaker="elena", start_char=98, end_char=153),
    SpeakerSpan(id="n2", speaker="narrator", start_char=153, end_char=195),
]
SPEAKERS_BY_NAME = {speaker.name: speaker for speaker in SPEAKERS}


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 240)
    return buffer.getvalue()


class FakeService:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.dispatched: list[str] = []

    async def render(self, pid: str, *, synth, on_chunk_attempt, on_chunk_result) -> dict:
        for chunk in self.chunks:
            speaker = SPEAKERS_BY_NAME[chunk.speaker]
            on_chunk_attempt(chunk, speaker)
            self.dispatched.append(chunk.id)
            try:
                wav = await synth(chunk.text, speaker.voice, speaker.speed)
            except Exception as exc:
                on_chunk_result(chunk, None, exc)
                raise
            on_chunk_result(chunk, wav, None)
        return {"dispatched": list(self.dispatched)}


def _chunk(chunk_id: str, span_id: str, speaker: str, text: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        span_id=span_id,
        speaker=speaker,
        text=text,
        start_char=0,
        end_char=len(text),
    )


def test_attempt_is_durable_before_dispatch_and_failure_stops(tmp_path: Path) -> None:
    journal_path = tmp_path / "listening-attempts.jsonl"
    chunks = [
        _chunk("c1", "n1", "narrator", "one"),
        _chunk("c2", "m1", "marcus", "two"),
        _chunk("c3", "e1", "elena", "three"),
    ]
    service = FakeService(chunks)
    runner = AuditionRunner(service, journal_path, cap=4)
    calls: list[str] = []

    async def synth(text: str, voice: str, speed: float) -> bytes:
        started = [row for row in _rows(journal_path) if row["kind"] == "attempt_started"]
        assert started[-1]["chunk_id"] == chunks[len(calls)].id
        calls.append(text)
        if text == "two":
            raise RuntimeError("synthetic TTS failure")
        return _wav_bytes()

    with pytest.raises(AuditionStopped):
        asyncio.run(runner.render("pid", synth=synth))

    assert service.dispatched == ["c1", "c2"]
    assert calls == ["one", "two"]
    rows = _rows(journal_path)
    assert [row["kind"] for row in rows] == [
        "attempt_started",
        "attempt_completed",
        "attempt_started",
        "attempt_failed",
    ]
    assert [row["ordinal"] for row in rows] == [1, 1, 2, 2]

    # A restart sees the recorded failure and refuses before any dispatch.
    with pytest.raises(AuditionStopped):
        AuditionRunner(service, journal_path, cap=4)
    assert service.dispatched == ["c1", "c2"]


def test_lifetime_cap_is_enforced_across_restarts(tmp_path: Path) -> None:
    journal_path = tmp_path / "listening-attempts.jsonl"
    chunks = [
        _chunk("c1", "n1", "narrator", "one"),
        _chunk("c2", "m1", "marcus", "two"),
        _chunk("c3", "e1", "elena", "three"),
    ]
    service = FakeService(chunks)
    runner = AuditionRunner(service, journal_path, cap=2)

    async def synth(text: str, voice: str, speed: float) -> bytes:
        return _wav_bytes()

    with pytest.raises(AuditionCapReached):
        asyncio.run(runner.render("pid", synth=synth))

    assert service.dispatched == ["c1", "c2"]
    started = [row for row in _rows(journal_path) if row["kind"] == "attempt_started"]
    assert len(started) == 2

    # A new runner reads the cap from the durable journal, not memory.
    with pytest.raises(AuditionCapReached):
        AuditionRunner(service, journal_path, cap=2)
    assert service.dispatched == ["c1", "c2"]


def test_duplicate_chunk_retry_is_blocked(tmp_path: Path) -> None:
    journal_path = tmp_path / "listening-attempts.jsonl"
    service = FakeService([_chunk("c1", "n1", "narrator", "one")])
    runner = AuditionRunner(service, journal_path, cap=4)

    async def synth(text: str, voice: str, speed: float) -> bytes:
        return _wav_bytes()

    asyncio.run(runner.render("pid", synth=synth))
    assert service.dispatched == ["c1"]

    async def should_not_run(text: str, voice: str, speed: float) -> bytes:
        raise AssertionError("duplicate chunk was dispatched")

    restarted = AuditionRunner(service, journal_path, cap=4)
    with pytest.raises(AuditionStopped):
        asyncio.run(restarted.render("pid", synth=should_not_run))
    assert service.dispatched == ["c1"]
    assert len(_rows(journal_path)) == 2


def test_malformed_audio_is_recorded_as_failure_and_stops(tmp_path: Path) -> None:
    journal_path = tmp_path / "listening-attempts.jsonl"
    service = FakeService([_chunk("c1", "n1", "narrator", "one")])
    runner = AuditionRunner(service, journal_path, cap=4)

    async def synth(text: str, voice: str, speed: float) -> bytes:
        return b"not-a-wav"

    with pytest.raises(AuditionStopped):
        asyncio.run(runner.render("pid", synth=synth))

    assert service.dispatched == ["c1"]
    rows = _rows(journal_path)
    assert [row["kind"] for row in rows] == ["attempt_started", "attempt_failed"]
    with pytest.raises(AuditionStopped):
        AuditionRunner(service, journal_path, cap=4)


def test_incomplete_attempt_blocks_restart(tmp_path: Path) -> None:
    journal_path = tmp_path / "listening-attempts.jsonl"
    journal = AuditionJournal(journal_path)
    journal.started(Attempt(
        ordinal=1,
        chunk_id="c1",
        span_id="n1",
        speaker="narrator",
        voice="af_heart",
        chars=3,
    ))

    with pytest.raises(AuditionUncertain):
        AuditionRunner(FakeService([]), journal_path, cap=4)


def test_tts_client_has_no_hidden_http_retry() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="busy")

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9880") as client:
            tts = TTSClient(client=client)
            with pytest.raises(httpx.HTTPStatusError):
                await tts.synthesize("hello", voice="af_heart", speed=1.0)

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.fixture()
def audition_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.config import settings as settings_module
    from app.config.settings import get_settings
    from app.services import orchestrator as orch_module
    from app.services import project_store as ps
    from app.services.audiobook import service as audiobook_service
    from app.services.model_router import reset_router

    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(get_settings(), projects_root=tmp_path),
    )
    ps._store = None
    orch_module._orch = None
    audiobook_service._service = None
    reset_router()

    service = get_audiobook()
    pid = service.store.create(ProjectCreate(name="audition", brief="voice dispatch")).id
    service.import_chapter(pid, ImportChapterRequest(text=MANUSCRIPT, filename="audition.txt"))
    service.set_speakers(pid, SPEAKERS)
    service.set_annotations(pid, SPANS, [])
    service.plan_chunks(pid)
    journal_path = service._base(pid) / "listening-attempts.jsonl"
    return service, pid, journal_path


def test_real_render_path_records_four_attempts_then_hits_cap(audition_env) -> None:
    service, pid, journal_path = audition_env
    runner = AuditionRunner(service, journal_path, cap=4)
    calls: list[tuple[str, str, float]] = []

    async def synth(text: str, voice: str, speed: float) -> bytes:
        calls.append((text, voice, speed))
        return _wav_bytes()

    result = asyncio.run(runner.render(pid, synth=synth))
    assert result["rendered"] == 4
    assert len(calls) == 4

    rows = _rows(journal_path)
    starts = [row for row in rows if row["kind"] == "attempt_started"]
    completed = [row for row in rows if row["kind"] == "attempt_completed"]
    assert [row["ordinal"] for row in starts] == [1, 2, 3, 4]
    assert [row["ordinal"] for row in completed] == [1, 2, 3, 4]
    assert [row["voice"] for row in starts] == ["af_heart", "am_michael", "af_bella", "af_heart"]

    with pytest.raises(AuditionCapReached):
        AuditionRunner(service, journal_path, cap=4)


def test_runner_preserves_live_generation_hold(audition_env) -> None:
    service, pid, journal_path = audition_env
    runner = AuditionRunner(service, journal_path, cap=4)

    with pytest.raises(GenerationNotAuthorized):
        asyncio.run(runner.render(pid))

    assert not journal_path.exists()
