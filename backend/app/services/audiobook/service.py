"""Audiobook slice service.

Required operator sequence:
    import -> annotate -> plan -> authorized render -> assemble -> listen ->
    record review -> approve -> verify/export
Live (non-fake) rendering is held server-side until explicitly authorized, and
approval can never precede a listen-back of the current WAV/MP3 pair.

Bounded scope: one locked chapter, verbatim speaker spans, deterministic chunks,
WAV + MP3 delivery, byte-bound approval, and a cancellation/recovery gate that
holds shared ModelRouter capacity. No LLM adaptation, music, M4B, or general
workflow engine is involved.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.models.audiobook import (
    AnnotationSet,
    Approval,
    AudiobookState,
    ChunkStatus,
    ImportChapterRequest,
    Master,
    ManuscriptRef,
    RecoveryState,
    Review,
    Speaker,
    SpeakerSpan,
    Exclusion,
)
from app.config import get_settings
from app.services.audiobook import audio, coverage
from app.services.model_router import CapacityHeld, HoldPersistenceError, get_router
from app.services.project_store import get_store
from app.services.tts_client import get_tts

MODEL = "kokoro-82m"
Synth = Callable[[str, str, float], Awaitable[bytes]]


class AudiobookError(ValueError):
    """A caller-visible audiobook validation error."""


class RecoveryBlocked(AudiobookError):
    """Rendering is blocked pending operator recovery of uncertain capacity."""


class GenerationNotAuthorized(AudiobookError):
    """A real (non-fake) render was requested while generation is unauthorized."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_signature(chunk) -> tuple:
    return (chunk.id, chunk.span_id, chunk.speaker, chunk.text, chunk.start_char, chunk.end_char)


class AudiobookService:
    def __init__(self) -> None:
        self.store = get_store()
        self.tts = get_tts()
        self.router = get_router()

    # --- paths ------------------------------------------------------
    def _base(self, pid: str) -> Path:
        pdir = self.store._dir(pid)
        base = pdir / "audiobook"
        (base / "chunks").mkdir(parents=True, exist_ok=True)
        return base

    def _state_path(self, pid: str) -> Path:
        return self._base(pid) / "state.json"

    def _manuscript_path(self, pid: str) -> Path:
        return self._base(pid) / "chapter.txt"

    def text(self, pid: str) -> str:
        path = self._manuscript_path(pid)
        if not path.is_file():
            raise AudiobookError("no chapter has been imported")
        return path.read_text(encoding="utf-8")

    # --- state ------------------------------------------------------
    def load(self, pid: str) -> AudiobookState:
        path = self._state_path(pid)
        if not path.exists():
            return AudiobookState()
        try:
            return AudiobookState(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise AudiobookError("audiobook state file is unreadable or invalid") from exc

    def save(self, pid: str, state: AudiobookState) -> None:
        self._state_path(pid).write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")

    def _event(self, pid: str, kind: str, detail: dict) -> None:
        line = json.dumps({"kind": kind, "at": _now(), **detail}, ensure_ascii=False)
        with (self._base(pid) / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # --- import -----------------------------------------------------
    def import_chapter(self, pid: str, req: ImportChapterRequest) -> AudiobookState:
        state = self.load(pid)
        if state.manuscript is not None and state.manuscript.locked:
            raise AudiobookError("the chapter is locked; re-import is not permitted")
        normalized = req.text.replace("\r\n", "\n").replace("\r", "\n")
        self._manuscript_path(pid).write_text(normalized, encoding="utf-8")
        state.manuscript = ManuscriptRef(
            filename=req.filename, sha256=audio.sha256_bytes(normalized.encode("utf-8")),
            char_count=len(normalized), newline_policy="lf", locked=True,
        )
        state.annotations = AnnotationSet()
        state.chunks = []
        state.master = None
        state.review = None
        state.approval = None
        self.save(pid, state)
        return state

    # --- speakers ---------------------------------------------------
    def set_speakers(self, pid: str, speakers: list[Speaker]) -> AudiobookState:
        state = self.load(pid)
        names = [speaker.name for speaker in speakers]
        if len(names) != len(set(names)):
            raise AudiobookError("speaker names must be unique")
        state.speakers = {speaker.name: speaker for speaker in speakers}
        state.approval = None
        state.review = None
        self.save(pid, state)
        return state

    # --- annotations (manual speaker-span editor surface) -----------
    def set_annotations(self, pid: str, spans: list[SpeakerSpan], exclusions: list[Exclusion]) -> AudiobookState:
        state = self.load(pid)
        if state.manuscript is None:
            raise AudiobookError("import a chapter before annotating")
        length = state.manuscript.char_count
        candidate = AnnotationSet(version=state.annotations.version + 1, spans=spans, exclusions=exclusions)
        errors = coverage.coverage_errors(length, candidate)
        if errors:
            raise AudiobookError("; ".join(errors))
        unknown = sorted({span.speaker for span in spans} - set(state.speakers))
        if unknown:
            raise AudiobookError("annotations reference unknown speakers: " + ", ".join(unknown))
        state.annotations = candidate
        state.review = None
        state.approval = None
        self.save(pid, state)
        return state

    # --- chunk planning ---------------------------------------------
    def plan_chunks(self, pid: str) -> AudiobookState:
        state = self.load(pid)
        if state.manuscript is None:
            raise AudiobookError("import a chapter first")
        text = self.text(pid)
        errors = coverage.coverage_errors(len(text), state.annotations)
        if errors:
            raise AudiobookError("; ".join(errors))
        unresolved = coverage.unresolved_span_ids(state.annotations)
        if unresolved:
            raise AudiobookError("unresolved attributions must be reviewed first: " + ", ".join(unresolved))
        planned = coverage.plan_chunks(text, state.annotations)
        previous = {chunk.id: chunk for chunk in state.chunks}
        merged = []
        for chunk in planned:
            old = previous.get(chunk.id)
            if old is not None and old.status == ChunkStatus.rendered and old.sha256 and old.audio_file:
                path = self._base(pid) / old.audio_file
                if path.is_file() and audio.sha256_file(path) == old.sha256:
                    merged.append(old)  # unchanged chunk: keep its rendered audio
                    continue
            merged.append(chunk)
        state.chunks = merged
        state.review = None
        state.approval = None
        self.save(pid, state)
        return state

    def _stale_reason(self, state: AudiobookState, text: str) -> str | None:
        """Return a message when persisted chunks no longer match the annotations.

        Guards both render and assembly: a chunk set planned against earlier
        annotations (or edited on disk) must never be treated as current.
        """
        if not state.chunks:
            return None
        expected = coverage.plan_chunks(text, state.annotations)
        if [_chunk_signature(chunk) for chunk in expected] != [_chunk_signature(chunk) for chunk in state.chunks]:
            return "chunks are stale relative to the current annotations; re-plan chunks"
        return None

    # --- rendering --------------------------------------------------
    async def _default_synth(self, text: str, voice: str, speed: float) -> bytes:
        return await self.tts.synthesize(text, voice=voice, speed=speed)

    async def render(self, pid: str, *, synth: Synth | None = None,
                     should_cancel: Callable[[], bool] | None = None,
                     confirm_stopped: bool = True, force: bool = False) -> dict:
        state = self.load(pid)
        if state.recovery.blocked:
            raise RecoveryBlocked("render blocked pending operator recovery: "
                                  + (state.recovery.reason or "uncertain occupied capacity"))
        if synth is None and not get_settings().audiobook.live_generation_authorized:
            # Server-side live-generation hold: real TTS is refused. Injected
            # fake synthesis used by isolated tests is unaffected.
            raise GenerationNotAuthorized(
                "live TTS generation is not authorized; no real render request is permitted")
        if state.manuscript is None:
            raise AudiobookError("import a chapter first")
        text = self.text(pid)
        errors = coverage.coverage_errors(len(text), state.annotations)
        if errors:
            raise AudiobookError("; ".join(errors))
        unresolved = coverage.unresolved_span_ids(state.annotations)
        if unresolved:
            raise AudiobookError("unresolved attributions: " + ", ".join(unresolved))
        if not state.chunks:
            raise AudiobookError("plan chunks before rendering")
        stale = self._stale_reason(state, text)
        if stale:
            raise AudiobookError(stale)

        synth = synth or self._default_synth
        should_cancel = should_cancel or (lambda: False)
        base = self._base(pid)
        job_id = uuid.uuid4().hex[:10]
        try:
            await self.router.acquire(MODEL, job_id)
        except CapacityHeld as exc:
            raise RecoveryBlocked(str(exc)) from exc
        rendered = 0
        cancelled = False
        confirmed = True
        try:
            for chunk in state.chunks:
                if should_cancel():
                    cancelled = True
                    confirmed = confirm_stopped
                    break
                path = base / "chunks" / f"{chunk.id}.wav"
                if (not force and chunk.status == ChunkStatus.rendered and chunk.sha256
                        and path.is_file() and audio.sha256_file(path) == chunk.sha256):
                    continue  # persisted resume
                speaker = state.speakers.get(chunk.speaker)
                if speaker is None:
                    raise AudiobookError(f"chunk {chunk.id} references unknown speaker {chunk.speaker}")
                wav = await synth(chunk.text, speaker.voice, speaker.speed)
                path.write_bytes(wav)
                chunk.audio_file = f"chunks/{chunk.id}.wav"
                chunk.sha256 = audio.sha256_bytes(wav)
                chunk.status = ChunkStatus.rendered
                chunk.rendered_at = _now()
                rendered += 1
                self.save(pid, state)  # persist resume point
        finally:
            await self.router.release(MODEL, job_id, unload=False)

        if rendered:
            # New audio invalidates any prior listen-back / approval.
            state.review = None
            state.approval = None
        if cancelled and not confirmed:
            reason = ("cancelled render was not confirmed stopped; the shared GPU capacity is held "
                      "until documented stop/drain evidence is recorded")
            hold_persisted = True
            try:
                self.router.hold(MODEL, job_id, reason=reason, holder=pid)
            except HoldPersistenceError as exc:
                # Fail safe: the in-memory hold still blocks this process, but the
                # operator must know that restart/cross-process protection is degraded.
                hold_persisted = False
                reason = f"{reason}; WARNING: shared hold not persisted ({exc})"
            state.recovery = RecoveryState(blocked=True, reason=reason, since=_now())
            self._event(pid, "RECOVERY_BLOCKED", {"reason": reason, "hold_persisted": hold_persisted})
        self.save(pid, state)
        return {"cancelled": cancelled, "rendered": rendered,
                "blocked": state.recovery.blocked, "state": state.model_dump(mode="json")}

    # --- assembly + approval ----------------------------------------
    def assemble(self, pid: str, *, pause_ms: int = coverage.DEFAULT_PAUSE_MS) -> AudiobookState:
        state = self.load(pid)
        if state.manuscript is None:
            raise AudiobookError("import a chapter first")
        if not state.chunks or any(chunk.status != ChunkStatus.rendered for chunk in state.chunks):
            raise AudiobookError("all chunks must be rendered before assembly")
        text = self.text(pid)
        errors = coverage.coverage_errors(len(text), state.annotations)
        if errors:
            raise AudiobookError("; ".join(errors))
        stale = self._stale_reason(state, text)
        if stale:
            raise AudiobookError(stale)
        base = self._base(pid)
        paths = []
        for chunk in state.chunks:
            if not chunk.audio_file or not (base / chunk.audio_file).is_file():
                raise AudiobookError(f"chunk {chunk.id} audio is missing")
            path = base / chunk.audio_file
            if not chunk.sha256 or audio.sha256_file(path) != chunk.sha256:
                raise AudiobookError(f"chunk {chunk.id} audio changed since it was rendered; re-render before assembly")
            paths.append(path)
        wav_path, mp3_path = base / "master.wav", base / "master.mp3"
        audio.write_master_wav(paths, pause_ms, wav_path)
        audio.encode_mp3(wav_path, mp3_path)
        state.master = Master(
            wav_file="master.wav", wav_sha256=audio.sha256_file(wav_path),
            mp3_file="master.mp3", mp3_sha256=audio.sha256_file(mp3_path),
            chunk_versions=[{"id": chunk.id, "sha256": chunk.sha256} for chunk in state.chunks],
            annotation_version=state.annotations.version, pause_ms=pause_ms,
            loudness_target=None, created_at=_now(),
        )
        state.review = None
        state.approval = None
        self.save(pid, state)
        return state

    # --- listen-back review -----------------------------------------
    def review(self, pid: str, *, listened_wav: bool, listened_mp3: bool,
               note: str | None = None) -> AudiobookState:
        """Record an operator listen-back of the current assembled master.

        Binds the attestation to the exact WAV/MP3 bytes present now, so a later
        byte change invalidates the review and approval cannot precede listening.
        """
        state = self.load(pid)
        if state.master is None:
            raise AudiobookError("assemble a master before reviewing")
        base = self._base(pid)
        wav_path, mp3_path = base / state.master.wav_file, base / state.master.mp3_file
        if not wav_path.is_file() or not mp3_path.is_file():
            raise AudiobookError("master files are missing")
        if not (listened_wav and listened_mp3):
            raise AudiobookError("both the WAV and MP3 master must be listened to before review")
        state.review = Review(
            wav_sha256=audio.sha256_file(wav_path), mp3_sha256=audio.sha256_file(mp3_path),
            listened_wav=True, listened_mp3=True, note=note, reviewed_at=_now(),
        )
        state.approval = None
        self.save(pid, state)
        return state

    def approve(self, pid: str) -> dict:
        state = self.load(pid)
        if state.master is None:
            raise AudiobookError("assemble a master before approving")
        base = self._base(pid)
        wav_path, mp3_path = base / state.master.wav_file, base / state.master.mp3_file
        if not wav_path.is_file() or not mp3_path.is_file():
            raise AudiobookError("master files are missing")
        # Approval may never precede listening: a review of the exact current
        # WAV/MP3 pair is mandatory.
        if state.review is None:
            raise AudiobookError("assemble, then listen to the master before approving")
        if not (state.review.listened_wav and state.review.listened_mp3):
            raise AudiobookError("both the WAV and MP3 must be listened to before approving")
        wav_sha, mp3_sha = audio.sha256_file(wav_path), audio.sha256_file(mp3_path)
        if state.review.wav_sha256 != wav_sha or state.review.mp3_sha256 != mp3_sha:
            raise AudiobookError("master bytes changed since review; listen and review again before approving")
        state.approval = Approval(
            wav_sha256=wav_sha, mp3_sha256=mp3_sha,
            chunk_versions=state.master.chunk_versions,
            annotation_version=state.master.annotation_version, approved_at=_now(),
        )
        self.save(pid, state)
        return {"approved": True, "wav_sha256": wav_sha, "mp3_sha256": mp3_sha}

    def verify_approval(self, pid: str) -> dict:
        state = self.load(pid)
        if state.approval is None:
            return {"approved": False, "reasons": ["no approval recorded"]}
        if state.master is None:
            return {"approved": False, "reasons": ["no master recorded"]}
        base = self._base(pid)
        reasons: list[str] = []
        wav_path, mp3_path = base / state.master.wav_file, base / state.master.mp3_file
        if not wav_path.is_file():
            reasons.append("wav master file is missing")
        elif audio.sha256_file(wav_path) != state.approval.wav_sha256:
            reasons.append("wav bytes changed since approval")
        if not mp3_path.is_file():
            reasons.append("mp3 master file is missing")
        elif audio.sha256_file(mp3_path) != state.approval.mp3_sha256:
            reasons.append("mp3 bytes changed since approval")
        if state.approval.annotation_version != state.annotations.version:
            reasons.append("annotation version changed since approval")
        if [{"id": c.id, "sha256": c.sha256} for c in state.chunks] != state.approval.chunk_versions:
            reasons.append("chunk set changed since approval")
        return {"approved": not reasons, "reasons": reasons}

    # --- recovery ---------------------------------------------------
    def clear_recovery(self, pid: str, *, worker_stopped: bool, queue_drained: bool,
                       evidence: str, note: str | None = None) -> AudiobookState:
        """Clear a recovery hold only with documented stop/drain evidence.

        A bare acknowledgement is insufficient: the operator must assert the
        worker is stopped and its queue is drained and attach a durable evidence
        reference. The shared, persisted ModelRouter hold is released here so
        competing acquisition across projects resumes.
        """
        if not (worker_stopped and queue_drained):
            raise AudiobookError("recovery cannot be cleared: confirm the worker is stopped and its queue is drained")
        if not evidence or len(evidence.strip()) < 10:
            raise AudiobookError("recovery cannot be cleared: documented stop/drain evidence is required")
        state = self.load(pid)
        router_hold = self.router.held(MODEL)
        owns_hold = bool(router_hold and router_hold.get("holder") == pid)
        if not state.recovery.blocked and not owns_hold:
            raise AudiobookError("no active recovery hold for this project")
        prior = state.recovery.reason
        cleared = False
        if owns_hold or router_hold is None:
            try:
                cleared = self.router.clear_hold(MODEL, holder=pid)
            except HoldPersistenceError as exc:
                # Keep the recovery blocked rather than claim a release that did not persist.
                raise AudiobookError(f"could not release the shared capacity hold: {exc}") from exc
        self._event(pid, "RECOVERY_CLEARED", {
            "worker_stopped": worker_stopped, "queue_drained": queue_drained,
            "evidence": evidence, "note": note, "prior_reason": prior,
            "shared_hold_released": cleared,
        })
        state.recovery = RecoveryState(blocked=False, reason=None, since=None)
        self.save(pid, state)
        return state

    # --- status -----------------------------------------------------
    def chapter(self, pid: str) -> dict:
        """Return the locked manuscript text for the editor display."""
        state = self.load(pid)
        if state.manuscript is None:
            return {"imported": False, "text": "", "manuscript": None}
        return {"imported": True, "text": self.text(pid),
                "manuscript": state.manuscript.model_dump(mode="json")}

    def status(self, pid: str) -> dict:
        state = self.load(pid)
        text_len = state.manuscript.char_count if state.manuscript else 0
        hold = self.router.held(MODEL)
        return {
            "state": state.model_dump(mode="json"),
            "coverage_errors": coverage.coverage_errors(text_len, state.annotations) if state.manuscript else [],
            "unresolved_spans": coverage.unresolved_span_ids(state.annotations),
            "verification": self.verify_approval(pid),
            "live_generation_authorized": get_settings().audiobook.live_generation_authorized,
            "capacity_hold": hold if (hold and hold.get("holder") == pid) else None,
        }


_service: AudiobookService | None = None


def get_audiobook() -> AudiobookService:
    global _service
    if _service is None:
        _service = AudiobookService()
    return _service
