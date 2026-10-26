"""Single-run audition journal and guard for the audiobook TTS path.

This module is deliberately not a job system. It wraps one
``AudiobookService.render`` call and provides:

* a durable, append-only attempt journal written before each synth dispatch;
* a lifetime submission cap counted from that journal across restarts;
* fail-closed handling of any failed or incomplete attempt.

No HTTP retry logic lives here. The caller supplies the synth callable.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.models.audiobook import Chunk, Speaker
from app.services.audiobook.service import AudiobookService

Synth = Callable[[str, str, float], Awaitable[bytes]]


class AuditionError(RuntimeError):
    """The audition cannot start or continue safely."""


class AuditionCapReached(AuditionError):
    """The lifetime submission cap is exhausted."""


class AuditionStopped(AuditionError):
    """A prior or current attempt failed or is uncertain."""


class AuditionUncertain(AuditionStopped):
    """An attempt started but has no recorded terminal outcome."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_wav(wav: bytes) -> None:
    """Fail the submission before the chunk can enter assembly."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as handle:
            params = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate())
            frames = handle.getnframes()
    except (wave.Error, EOFError) as exc:
        raise AuditionError("synth returned an unreadable WAV") from exc
    if params != (1, 2, 24000) or frames <= 0:
        raise AuditionError(
            f"synth returned unexpected WAV format {params} with {frames} frames")


@dataclass(frozen=True)
class Attempt:
    ordinal: int
    chunk_id: str
    span_id: str
    speaker: str
    voice: str
    chars: int


class AuditionJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except (OSError, ValueError) as exc:
            raise AuditionUncertain(f"attempt journal is unreadable: {self.path}") from exc

    def started(self, attempt: Attempt) -> None:
        self._append({
            "kind": "attempt_started",
            "at": _now(),
            "ordinal": attempt.ordinal,
            "chunk_id": attempt.chunk_id,
            "span_id": attempt.span_id,
            "speaker": attempt.speaker,
            "voice": attempt.voice,
            "chars": attempt.chars,
        })

    def completed(self, attempt: Attempt, wav: bytes) -> None:
        self._append({
            "kind": "attempt_completed",
            "at": _now(),
            "ordinal": attempt.ordinal,
            "chunk_id": attempt.chunk_id,
            "bytes": len(wav),
            "sha256": hashlib.sha256(wav).hexdigest(),
        })

    def failed(self, attempt: Attempt, error: str) -> None:
        self._append({
            "kind": "attempt_failed",
            "at": _now(),
            "ordinal": attempt.ordinal,
            "chunk_id": attempt.chunk_id,
            "error": error[:1000],
        })


@dataclass(frozen=True)
class JournalState:
    submissions: int
    failed: tuple[dict[str, Any], ...]
    uncertain: tuple[int, ...]
    started_chunks: tuple[str, ...]


def inspect_journal(rows: list[dict[str, Any]]) -> JournalState:
    started: dict[int, dict[str, Any]] = {}
    terminal: set[int] = set()
    failed: list[dict[str, Any]] = []
    invalid = False
    for row in rows:
        if not isinstance(row, dict):
            invalid = True
            continue
        kind = row.get("kind")
        ordinal = row.get("ordinal")
        if not isinstance(ordinal, int) or ordinal < 1:
            invalid = True
            continue
        if kind == "attempt_started":
            if ordinal in started or not isinstance(row.get("chunk_id"), str) or not row["chunk_id"]:
                invalid = True
                continue
            started[ordinal] = row
        elif kind in {"attempt_completed", "attempt_failed"}:
            if ordinal not in started or ordinal in terminal:
                invalid = True
                continue
            terminal.add(ordinal)
            if kind == "attempt_failed":
                failed.append(row)
        else:
            invalid = True
    if invalid:
        raise AuditionUncertain("attempt journal contains inconsistent rows")
    uncertain = tuple(order for order in sorted(started) if order not in terminal)
    return JournalState(
        submissions=len(started),
        failed=tuple(failed),
        uncertain=uncertain,
        started_chunks=tuple(row["chunk_id"] for row in started.values()),
    )


class AuditionRunner:
    """One bounded audition over an existing AudiobookService."""

    def __init__(self, service: AudiobookService, journal_path: Path, *, cap: int) -> None:
        if cap < 1:
            raise ValueError("audition cap must be at least 1")
        self.service = service
        self.journal = AuditionJournal(Path(journal_path))
        self.cap = cap
        self._current: Attempt | None = None
        self._submissions = 0
        self._started_chunks: set[str] = set()
        self._ensure_ready()

    def _ensure_ready(self) -> None:
        state = inspect_journal(self.journal.rows())
        if state.failed:
            first = state.failed[0]
            raise AuditionStopped(
                f"attempt {first.get('ordinal')} failed; this audition is stopped")
        if state.uncertain:
            raise AuditionUncertain(
                "attempt(s) without terminal outcome: "
                + ", ".join(str(order) for order in state.uncertain))
        if state.submissions >= self.cap:
            raise AuditionCapReached(
                f"lifetime submission cap reached ({state.submissions}/{self.cap})")
        self._submissions = state.submissions
        self._started_chunks = set(state.started_chunks)

    def _before_dispatch(self, chunk: Chunk, speaker: Speaker) -> None:
        if self._current is not None:
            raise AuditionUncertain("previous attempt has no recorded outcome")
        if self._submissions >= self.cap:
            raise AuditionCapReached(
                f"lifetime submission cap reached ({self._submissions}/{self.cap})")
        if chunk.id in self._started_chunks:
            raise AuditionStopped(
                f"chunk {chunk.id} already has a recorded attempt; "
                "a retry requires separate authorization")
        attempt = Attempt(
            ordinal=self._submissions + 1,
            chunk_id=chunk.id,
            span_id=chunk.span_id,
            speaker=chunk.speaker,
            voice=speaker.voice,
            chars=len(chunk.text),
        )
        # Durable before the synth callable can dispatch an HTTP request.
        self.journal.started(attempt)
        self._submissions = attempt.ordinal
        self._started_chunks.add(attempt.chunk_id)
        self._current = attempt

    def _after_dispatch(self, chunk: Chunk, wav: bytes | None,
                        error: Exception | None) -> None:
        attempt = self._current
        if attempt is None or attempt.chunk_id != chunk.id:
            raise AuditionUncertain("synth result does not match the recorded attempt")
        if error is not None:
            self.journal.failed(attempt, f"{type(error).__name__}: {error}")
            self._current = None
            raise AuditionStopped(
                f"attempt {attempt.ordinal} failed; audition stopped") from error
        if wav is None:
            self.journal.failed(attempt, "synth returned no audio")
            self._current = None
            raise AuditionStopped(
                f"attempt {attempt.ordinal} failed; audition stopped")
        try:
            if not isinstance(wav, (bytes, bytearray)) or not wav:
                raise AuditionError("synth returned no audio bytes")
            _validate_wav(bytes(wav))
        except Exception as exc:
            self.journal.failed(attempt, f"{type(exc).__name__}: {exc}")
            self._current = None
            raise AuditionStopped(
                f"attempt {attempt.ordinal} failed; audition stopped") from exc
        self.journal.completed(attempt, bytes(wav))
        self._current = None

    async def render(self, pid: str, *, synth: Synth | None = None) -> dict[str, Any]:
        self._ensure_ready()
        return await self.service.render(
            pid,
            synth=synth,
            on_chunk_attempt=self._before_dispatch,
            on_chunk_result=self._after_dispatch,
        )
