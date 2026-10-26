"""Offline tests for bounded section-level repaint.

A fake provider supplies deterministic PCM; no service is contacted, no model
is loaded, and no live generation occurs.
"""

from __future__ import annotations

import dataclasses
import hashlib

import httpx
from array import array
from pathlib import Path

import pytest

from app.config import get_settings
from app.config import settings as settings_module
from app.models.music import MusicSettings
from app.models.schemas import AssetKind, ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.music_service import (
    MusicConflict,
    MusicNotAuthorized,
    MusicService,
)
from app.services.repaint_provider import RepaintOutput

SR = 48000
CH = 2
SECONDS = 10.0
FRAMES = int(SECONDS * SR)


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
           repaint: bool = True, max_revisions: int = 30) -> None:
    base = get_settings()
    monkeypatch.setattr(
        settings_module,
        "_cached",
        dataclasses.replace(
            base,
            projects_root=tmp_path,
            music=dataclasses.replace(
                base.music,
                repaint_enabled=repaint,
                repaint_crossfade_ms=10,
                repaint_min_free_vram_mib=1024,
                max_revisions=max_revisions,
            ),
        ),
    )
    ps._store = None
    music_module._service = None


def _service() -> MusicService:
    return MusicService(comfy=object(), router=object())


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


def _take(service: MusicService, pid: str, *, suffix: str = "", approve: bool = True) -> tuple[str, array, str]:
    settings = _settings()
    service.save_draft(pid, settings, "source")
    values = _source_values()
    data = music_module._wav_f32_bytes(values, SR, CH)
    digest = hashlib.sha256(data).hexdigest()
    asset = service.store.save_asset(
        pid, AssetKind.music, f"source{suffix}.wav", data,
        meta={"music": {
            "revision": 1,
            "effective_settings": settings.model_dump(mode="json"),
            "audio_sha256": digest,
        }},
    )
    service._append_journal(pid, {
        "kind": "attempt_started", "attempt_id": f"source-attempt{suffix}",
        "at": "2026-10-24T00:00:00+00:00", "revision": 1,
        "revision_name": "source",
        "effective_settings": settings.model_dump(mode="json"),
        "submission_cap": 4,
    })
    service._append_journal(pid, {
        "kind": "attempt_completed", "attempt_id": f"source-attempt{suffix}",
        "at": "2026-10-24T00:01:00+00:00", "asset_id": asset.id,
        "sha256": digest, "prompt_id": f"source-prompt{suffix}",
    })
    if approve:
        service.approve(pid, "approved source", 4)
    return asset.id, values, digest


class FakeRepaint:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode

    def generate(self, request):
        source, sr, ch = music_module._decode_pcm(request.source_path)
        output = array("f", source)
        start = int(round(request.start_s * sr))
        end = int(round(request.end_s * sr))
        for index in range(start * ch, end * ch):
            output[index] = source[index] * 0.5
        if self.mode == "short":
            output = output[:-2]
        if self.mode == "rate":
            return RepaintOutput(samples=output, sample_rate=44100, channels=ch)
        return RepaintOutput(samples=output, sample_rate=sr, channels=ch)


def _proposal(service: MusicService, pid: str, asset_id: str) -> str:
    result = service.create_repaint_proposal(
        pid, source_asset_id=asset_id, start_s=2.0, end_s=4.0,
        prompt="make the fiddle gentler; retain guitar, tempo, and mood",
    )
    return result["proposal"]["proposal_id"]


def _gate(service: MusicService, pid: str, measured: int = 1024) -> None:
    service.propose_repaint_capacity_gate(
        pid, measured_free_vram_mib=measured, gpu_index=1,
        competing_processes=[], note="measured before first load")
    service.approve_repaint_capacity_gate(
        pid, confirm=True, note="operator approved the measured capacity")


def _submitted(service: MusicService, pid: str) -> tuple[str, dict]:
    asset_id, source_values, source_sha = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "authorize candidate")
    _gate(service, pid)
    result = service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    return proposal_id, {"source_values": source_values, "source_sha": source_sha,
                         "asset_id": asset_id, "result": result}


def test_proposal_requires_completed_take_and_valid_interval(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    asset_id, _, _ = _take(service, pid)
    before = service._journal_state(pid)["submissions"]

    with pytest.raises(MusicConflict):
        service.create_repaint_proposal(
            pid, source_asset_id="missing", start_s=0.0, end_s=1.0, prompt="x")
    with pytest.raises(MusicConflict):
        service.create_repaint_proposal(
            pid, source_asset_id=asset_id, start_s=1.0, end_s=1.0, prompt="x")
    with pytest.raises(MusicConflict):
        service.create_repaint_proposal(
            pid, source_asset_id=asset_id, start_s=0.0, end_s=99.0, prompt="x")
    with pytest.raises(MusicConflict):
        service.create_repaint_proposal(
            pid, source_asset_id=asset_id, start_s=0.0, end_s=1.0, prompt="   ")

    result = service.create_repaint_proposal(
        pid, source_asset_id=asset_id, start_s=2.0, end_s=4.0, prompt="gentler fiddle")
    proposal = result["proposal"]
    assert proposal["status"] == "proposed"
    assert proposal["start_sample"] == 96000
    assert proposal["end_sample"] == 192000
    assert proposal["context_start_s"] == 0.0
    assert proposal["context_end_s"] == 7.0
    assert proposal["source_sha256"] == hashlib.sha256(
        service.store.asset_path(pid, asset_id).read_bytes()).hexdigest()
    assert service._journal_state(pid)["submissions"] == before


def test_submission_requires_authorization_and_both_gates(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, repaint=True)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    asset_id, _, _ = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    before = service._journal_state(pid)["submissions"]

    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    service.authorize_repaint(pid, proposal_id, "authorize")
    monkeypatch.setattr(
        settings_module, "_cached",
        dataclasses.replace(get_settings(), music=dataclasses.replace(
            get_settings().music, repaint_enabled=False)))
    with pytest.raises(MusicNotAuthorized):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    assert service._journal_state(pid)["submissions"] == before

    monkeypatch.setattr(
        settings_module, "_cached",
        dataclasses.replace(get_settings(), music=dataclasses.replace(
            get_settings().music, repaint_enabled=True)))
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())

    service.propose_repaint_capacity_gate(
        pid, measured_free_vram_mib=2048, gpu_index=1,
        competing_processes=["none"], note="measured")
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    assert service._journal_state(pid)["submissions"] == before

    service.approve_repaint_capacity_gate(
        pid, confirm=True, note="approved after measurement")
    result = service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    assert result["status"] == "candidate"
    assert service._journal_state(pid)["submissions"] == before + 1


def test_candidate_preserves_pcm_outside_transition_and_records_provenance(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    proposal_id, data = _submitted(service, pid)
    proposal = service.repaint_proposal(pid, proposal_id)
    candidate = proposal["candidate"]

    source_path = service.store.asset_path(pid, data["asset_id"])
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == data["source_sha"]

    master_path = service.store.asset_path(pid, candidate["master_asset_id"])
    master, rate, channels = music_module._read_wav_f32(master_path)
    source, _, _ = music_module._read_wav_f32(source_path)
    assert rate == SR and channels == CH
    assert len(master) == len(source)
    fade = candidate["assembly"]["crossfade_samples"]
    fade_start = max(0, proposal["start_sample"] - fade)
    fade_end = min(FRAMES, proposal["end_sample"] + fade)
    assert master[:fade_start * CH] == source[:fade_start * CH]
    assert master[fade_end * CH:] == source[fade_end * CH:]
    assert master[proposal["start_sample"] * CH:proposal["end_sample"] * CH] != \
        source[proposal["start_sample"] * CH:proposal["end_sample"] * CH]
    assert candidate["verification"]["pcm_equal_outside_transition"] is True
    assert candidate["duration_samples"] == FRAMES
    assert candidate["source_sha256"] == data["source_sha"]
    assert proposal["prompt"].startswith("make the fiddle gentler")
    assert proposal["settings"]["bpm"] == 80
    assert proposal["provider"] == "ace-step-1.5-repaint"
    assert proposal["model"] == "acestep-v15-turbo"
    assert "explicit" in proposal["workflow"]
    meta = service.store.get_asset(pid, candidate["master_asset_id"]).meta
    assert meta["source_sha256"] == data["source_sha"]
    assert meta["start_sample"] == proposal["start_sample"]


def test_bad_output_is_rejected_and_still_consumes_one_submission(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    asset_id, _, _ = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "authorize")
    _gate(service, pid)
    before = service._journal_state(pid)["submissions"]

    for mode in ("short", "rate"):
        if mode == "rate":
            service2 = service
            proposal2 = _proposal(service2, pid, asset_id)
            service2.authorize_repaint(pid, proposal2, "authorize again")
            proposal_id = proposal2
        with pytest.raises(MusicConflict):
            service.submit_repaint(pid, proposal_id, provider=FakeRepaint(mode))
        state = service._load_state(pid)
        failed = next(p for p in state.repaint_proposals
                      if p.proposal_id == proposal_id)
        assert failed.status == "failed"
        assert failed.candidate is None
    assert service._journal_state(pid)["submissions"] == before + 2
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())


def test_apply_creates_new_version_without_touching_selection_or_approval(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    proposal_id, data = _submitted(service, pid)
    service.prefer_take(pid, data["asset_id"], "source remains preferred")
    before_state = service._load_state(pid)

    applied = service.apply_repaint(pid, proposal_id)
    state = service._load_state(pid)
    assert applied["draft_version"] == 2
    assert [r.version for r in state.revisions] == [1, 2]
    assert state.revisions[0].name == "source"
    assert state.revisions[1].source == "repaint"
    assert state.revisions[1].parent_version == 1
    assert state.draft.version == 2
    assert state.draft.settings_sha256 == before_state.draft.settings_sha256
    assert state.approval.draft_version == 1
    assert state.approval.settings_sha256 == before_state.approval.settings_sha256
    assert state.preferred_take.asset_id == data["asset_id"]
    assert state.audio_approval is None
    assert service.revisions(pid)["current_version"] == 2
    assert service.store.get_asset(
        pid, state.repaint_proposals[-1].candidate.master_asset_id
    ).meta["applied_revision"] == 2


def test_reject_leaves_source_untouched_and_blocks_apply(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    proposal_id, data = _submitted(service, pid)
    service.reject_repaint(pid, proposal_id, "not the right direction")
    source_path = service.store.asset_path(pid, data["asset_id"])
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == data["source_sha"]
    with pytest.raises(MusicConflict):
        service.apply_repaint(pid, proposal_id)
    assert service._load_state(pid).draft.version == 1


def test_capacity_gate_rejects_low_measurement_and_requires_confirmation(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    with pytest.raises(MusicConflict):
        service.propose_repaint_capacity_gate(
            pid, measured_free_vram_mib=0, gpu_index=1,
            competing_processes=[], note="bad")
    service.propose_repaint_capacity_gate(
        pid, measured_free_vram_mib=512, gpu_index=1,
        competing_processes=[], note="measured low")
    with pytest.raises(MusicConflict):
        service.approve_repaint_capacity_gate(pid, confirm=False, note="nope")
    service.approve_repaint_capacity_gate(pid, confirm=True, note="approved")
    gate = service.repaint_capacity_gate(pid)
    assert gate["approved"] is True
    assert gate["measured_free_vram_mib"] == 512

    asset_id, _, _ = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "authorize")
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())


def test_apply_respects_revision_retention_limit(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch, max_revisions=1)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    proposal_id, _ = _submitted(service, pid)
    with pytest.raises(MusicConflict):
        service.apply_repaint(pid, proposal_id)
    state = service._load_state(pid)
    assert [r.version for r in state.revisions] == [1]


def test_authorization_invalidated_when_bound_input_changes(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    asset_id, _, _ = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "authorize")

    revised = service.revise_repaint(
        pid, proposal_id, start_s=2.0, end_s=3.5, prompt="different guidance")
    assert revised["status"] == "proposed"
    assert revised["authorized_fingerprint"] == ""
    assert revised["authorization_note"] == ""
    before = service._journal_state(pid)["submissions"]
    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    assert service._journal_state(pid)["submissions"] == before

    service.authorize_repaint(pid, proposal_id, "authorize again")
    state = service._load_state(pid)
    proposal = next(p for p in state.repaint_proposals
                    if p.proposal_id == proposal_id)
    proposal.prompt = "tampered after authorization"
    service._save_state(pid, state)
    with pytest.raises(MusicConflict, match="authorization is invalid"):
        service.submit_repaint(pid, proposal_id, provider=FakeRepaint())


class TimeoutRepaint:
    def generate(self, request):
        raise httpx.TimeoutException("isolated timeout")


def test_provider_timeout_creates_durable_hold_until_confirmed_stop(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="repaint", brief="x")).id
    asset_id, _, _ = _take(service, pid)
    proposal_id = _proposal(service, pid, asset_id)
    service.authorize_repaint(pid, proposal_id, "authorize")
    _gate(service, pid)
    before = service._journal_state(pid)["submissions"]

    with pytest.raises(MusicConflict):
        service.submit_repaint(pid, proposal_id, provider=TimeoutRepaint())
    assert service._journal_state(pid)["submissions"] == before + 1
    kinds = [row.get("kind") for row in service._journal_state(pid)["rows"]]
    assert "attempt_uncertain" in kinds

    reloaded = _service()
    state = reloaded._load_state(pid)
    assert state.recovery_required is True
    held = next(p for p in state.repaint_proposals if p.proposal_id == proposal_id)
    assert held.status == "uncertain"

    with pytest.raises(MusicConflict, match="recovery is required"):
        reloaded.submit_repaint(pid, proposal_id, provider=FakeRepaint())
    assert reloaded._journal_state(pid)["submissions"] == before + 1

    with pytest.raises(MusicConflict, match="confirmed provider stop"):
        reloaded.recover_repaint(pid, proposal_id, False)
    recovered = reloaded.recover_repaint(pid, proposal_id, True)
    assert recovered["recovery_required"] is False
    assert reloaded._load_state(pid).recovery_required is False

    new_proposal = _proposal(reloaded, pid, asset_id)
    reloaded.authorize_repaint(pid, new_proposal, "authorize after recovery")
    result = reloaded.submit_repaint(pid, new_proposal, provider=FakeRepaint())
    assert result["status"] == "candidate"
    assert reloaded._journal_state(pid)["submissions"] == before + 2

def test_browser_repaint_workflow_controls():
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    for element_id in [
        "music-repaint-info", "music-repaint-source",
        "music-repaint-audio", "music-repaint-source-meta",
        "music-repaint-suggestions", "music-repaint-start", "music-repaint-end",
        "music-repaint-play-selection", "music-repaint-stop", "music-repaint-playback",
        "music-repaint-section", "music-repaint-shared-start",
        "music-repaint-chips", "music-repaint-review",
        "music-repaint-confirm", "music-repaint-confirmed",
        "music-repaint-prompt", "music-repaint-note", "music-repaint-save",
        "music-repaint-authorize", "music-repaint-revise", "music-repaint-submit",
        "music-repaint-recover", "music-repaint-status", "music-repaint-list",
        "music-repaint-compare", "music-repaint-source-context",
        "music-repaint-candidate-audio", "music-repaint-apply", "music-repaint-reject",
    ]:
        assert f'id="{element_id}"' in html
    assert "app.js?v=46" in html
    for token in [
        "async function refreshMusicRepaint", "function compareMusicRepaint",
        "function repaintProposalBody", "function musicRepaintTimeSuggestions",
        "function updateMusicRepaintConfirmed", "refreshMusicRepaint()",
        "not detected audio boundaries",
        "other sounds inside the interval may change",
        "Only PCM outside the declared edit/transition region is preserved",
        "/repaint/proposals", "/authorize", "/revise", "/submit",
        "/recover", "/apply", "/reject", "/compare", "/repaint/capacity-gate",
        "confirm_stopped: true", "authorization was invalidated",
        "function playMusicRepaintSelection", "function stopMusicRepaintSelection",
        "function validateMusicRepaintSelection", "function updateMusicRepaintPlayback",
        "musicRepaintSelectionStopAt", "refreshMusicRepaintBoundaries",
        "selectMusicRepaintOccurrence", "listening-confirmed",
        "/section-boundaries", "shared_start_from", "musicRepaintBoundaries",
    ]:
        assert token in js

def test_section_boundaries_persist_per_occurrence_and_source(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="boundaries", brief="x")).id
    asset1, _, sha1 = _take(service, pid)
    asset2, _, sha2 = _take(service, pid, suffix="-b", approve=False)
    before = service._load_state(pid)

    first = service.save_section_boundary(
        pid, asset_id=asset1, section="chorus", occurrence=1,
        arrangement_index=0, start_s=1.0, end_s=3.0,
        suggested_start_s=0.6, suggested_end_s=3.0)
    second = service.save_section_boundary(
        pid, asset_id=asset1, section="chorus", occurrence=2,
        arrangement_index=0, start_s=5.0, end_s=7.0,
        suggested_start_s=4.2, suggested_end_s=7.0)
    other = service.save_section_boundary(
        pid, asset_id=asset2, section="chorus", occurrence=1,
        arrangement_index=0, start_s=2.0, end_s=4.0)
    assert first["boundary"]["source_sha256"] == sha1
    assert other["boundary"]["source_sha256"] == sha2

    listing = service.section_boundaries(pid, asset1)
    assert len(listing["boundaries"]) == 2
    assert [item["occurrence"] for item in listing["boundaries"]] == [1, 2]
    assert all(item["section"] == "chorus" for item in listing["boundaries"])
    assert service.section_boundaries(pid, asset2)["boundaries"][0]["start_s"] == 2.0
    assert len(service.section_boundaries(pid)["boundaries"]) == 3

    reloaded = _service()
    assert len(reloaded.section_boundaries(pid, asset1)["boundaries"]) == 2
    state = reloaded._load_state(pid)
    assert state.draft.settings_sha256 == before.draft.settings_sha256
    assert len(state.revisions) == len(before.revisions)
    assert state.draft.settings.arrangement == before.draft.settings.arrangement
    assert len(state.repaint_proposals) == len(before.repaint_proposals)


def test_section_boundary_validation_uses_actual_source_duration(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="boundaries", brief="x")).id
    asset, _, _ = _take(service, pid)

    with pytest.raises(MusicConflict):
        service.save_section_boundary(
            pid, asset_id=asset, section="outro", occurrence=1,
            arrangement_index=9, start_s=0.0, end_s=11.0)
    with pytest.raises(MusicConflict):
        service.save_section_boundary(
            pid, asset_id=asset, section="outro", occurrence=1,
            arrangement_index=9, start_s=-1.0, end_s=2.0)
    with pytest.raises(MusicConflict):
        service.save_section_boundary(
            pid, asset_id=asset, section="outro", occurrence=1,
            arrangement_index=9, start_s=3.0, end_s=3.0)

    saved = service.save_section_boundary(
        pid, asset_id=asset, section="outro", occurrence=1,
        arrangement_index=9, start_s=0.0, end_s=10.0)
    assert saved["boundary"]["source_duration_s"] == 10.0
    assert saved["boundary"]["start_sample"] == 0
    assert saved["boundary"]["end_sample"] == 480000


def test_section_boundary_flags_gap_and_overlap_without_shifting(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="boundaries", brief="x")).id
    asset, _, _ = _take(service, pid)

    service.save_section_boundary(
        pid, asset_id=asset, section="intro", occurrence=1,
        arrangement_index=0, start_s=0.0, end_s=3.0)
    verse = service.save_section_boundary(
        pid, asset_id=asset, section="verse", occurrence=1,
        arrangement_index=1, start_s=4.0, end_s=6.0,
        shared_start_from="intro #1")
    assert verse["boundary"]["shared_start_from"] == "intro #1"
    flags = verse["review_flags"]
    assert flags[0]["code"] == "gap" and flags[0]["seconds"] == 1.0
    assert "intro #1" in flags[0]["message"] and "verse #1" in flags[0]["message"]

    records = {item["section"]: item for item in
               service.section_boundaries(pid, asset)["boundaries"]}
    assert records["intro"]["start_s"] == 0.0
    assert records["intro"]["end_s"] == 3.0
    assert records["verse"]["start_s"] == 4.0

    overlap = service.save_section_boundary(
        pid, asset_id=asset, section="verse", occurrence=1,
        arrangement_index=1, start_s=2.5, end_s=6.0,
        shared_start_from="intro #1")
    flags = overlap["review_flags"]
    assert flags[0]["code"] == "overlap" and flags[0]["seconds"] == 0.5
    records = {item["section"]: item for item in
               service.section_boundaries(pid, asset)["boundaries"]}
    assert records["intro"]["start_s"] == 0.0
    assert records["intro"]["end_s"] == 3.0
    assert records["verse"]["start_s"] == 2.5
    assert len(records) == 2

def test_section_boundary_stale_source_identifier_is_rejected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    service = _service()
    pid = service.store.create(ProjectCreate(name="boundaries", brief="x")).id
    _take(service, pid)
    with pytest.raises(MusicConflict):
        service.save_section_boundary(
            pid, asset_id="missing", section="intro", occurrence=1,
            arrangement_index=0, start_s=0.0, end_s=1.0)
