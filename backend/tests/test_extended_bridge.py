"""Offline extended-Bridge plan, assembly, and package tests.

Uses the authoritative 300s source/proposal read-only and writes only to
isolated temporary storage. No provider, model, service, or protected project
is contacted or mutated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import zipfile
from array import array
from pathlib import Path

import pytest

from app.config import get_settings
from app.config import settings as settings_module
from app.services.music_service import (
    MusicNotAuthorized, MusicService, _decode_pcm, _sha256_bytes, _wav_f32_bytes,
)
from app.services.project_store import ProjectStore
from app.services.replacement_export import (
    ExtendedBridgeBlocked,
    ExtendedBridgeExportService,
    assemble_extended_bridge,
)
from app.services.repaint_provider import RepaintOutput

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "projects/ac668b7c7faa/music/ace-step-519bd4ccf1.mp3"
PROPOSAL = REPO / "docs/extended-bridge-audio-v2-proposal.json"
SR = 48000
CH = 2


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _actual_export_service() -> ExtendedBridgeExportService:
    base = get_settings()
    actual = dataclasses.replace(
        base, projects_root=REPO / "projects", profiles_root=REPO / "profiles")
    previous = settings_module._cached
    settings_module._cached = actual
    try:
        store = ProjectStore()
        music = MusicService(store=store, comfy=object(), router=object())
    finally:
        settings_module._cached = previous
    return ExtendedBridgeExportService(music=music)


@pytest.fixture(scope="module")
def extended_plan(tmp_path_factory):
    out = tmp_path_factory.mktemp("extended-bridge-offline")
    service = _actual_export_service()
    result = service.plan("ac668b7c7faa", output_root=out)
    return service, Path(result["plan_dir"]), result


def test_timeline_active_map_is_reconciled_and_history_is_retained():
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    active = {
        item["section"]: [item["start_s"], item["end_s"]]
        for item in proposal["audio_timelines"]["original"]["markers"]
        if item["section"] in {"Bridge", "Final Chorus", "Outro"}
    }
    assert active == {
        "Bridge": [185.0, 224.0],
        "Final Chorus": [224.0, 285.0],
        "Outro": [285.0, 300.0],
    }
    correction_1 = next(
        item for item in proposal["marker_history"]["corrections"]
        if item["correction_id"] == "correction-1")
    assert correction_1["status"] == "superseded_by_correction-2"
    assert correction_1["result"] == {
        "Final Chorus": [224.0, 284.0], "Outro": [284.0, 300.0]}
    assert proposal["verification"]["status"] == "verified_offline"
    assert proposal["verification"]["failed_checks"] == []
    verification = json.loads(
        (REPO / "docs/extended-bridge-audio-v2-verification.json").read_text(
            encoding="utf-8"))
    assert len(verification["checks"]) == 70
    assert verification["failed_checks"] == []


def test_extended_plan_reconstructs_exact_conditioning_without_mutation(extended_plan):
    _service, plan_dir, result = extended_plan
    project = REPO / "projects/ac668b7c7faa"
    state_before = (project / "music/.state/state.json").read_bytes()
    assets_before = (project / "assets.json").read_bytes()
    source_before = _sha(SOURCE)
    plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    request = json.loads((plan_dir / "native.request.json").read_text(encoding="utf-8"))
    body = (plan_dir / "native.body").read_bytes()
    clip = (plan_dir / "native.conditioning.wav").read_bytes()
    assert plan["status"] == "planned_not_dispatched"
    assert plan["provider_contacted"] is False
    assert plan["allowance_consumed"] is False
    assert plan["project_mutated"] is False
    assert request["provider_kind"] == "native_ace_step_repaint"
    assert request["capture_status"] == "dry_run_not_dispatched"
    assert request["accounting"]["allowance_consumed"] is False
    assert request["request_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert request["conditioning"]["sha256"] == hashlib.sha256(clip).hexdigest()
    assert request["conditioning"]["sha256"] == (
        "3ffbb2703aaf5423c98966bcfde7792a2c492b8da46da3af9430a32a24aca3ce")
    assert request["conditioning"]["duration_s"] == 54.0
    assert request["conditioning"]["sample_rate"] == SR
    assert request["conditioning"]["channels"] == CH
    assert request["conditioning"]["source_mapping"] == {
        "clip_start_s": 182.0,
        "clip_end_s": 236.0,
        "mask_start_s": 185.0,
        "mask_end_s": 233.0,
        "clip_offset_s": 182.0,
        "conditioning_is_full_source": False,
    }
    assert request["fields"]["repainting_start"] == "3.0"
    assert request["fields"]["repainting_end"] == "51.0"
    assert request["fields"]["lyrics"] == "\n".join(plan["lyrics"])
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
    assert hashlib.sha256(body).hexdigest() == result["request_body_sha256"]
    assert hashlib.sha256(clip).hexdigest() == result["conditioning_sha256"]
    assert (plan_dir / "native.environment.json").is_file()
    assert plan_dir.is_relative_to(Path(result["plan_dir"]).parents[0])
    assert not plan_dir.is_relative_to(project)
    assert _sha(SOURCE) == source_before
    assert (project / "music/.state/state.json").read_bytes() == state_before
    assert (project / "assets.json").read_bytes() == assets_before


def test_extended_plan_rejects_protected_output_root(tmp_path):
    service = _actual_export_service()
    with pytest.raises(ExtendedBridgeBlocked, match="outside the protected project"):
        service.plan(
            "ac668b7c7faa",
            output_root=REPO / "projects/ac668b7c7faa/music/extended-forbidden",
        )


def test_extended_dry_run_has_no_dispatch_path(extended_plan, tmp_path, monkeypatch):
    service, plan_dir, _result = extended_plan
    monkeypatch.setattr(
        service.music, "begin_extended_bridge_live",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MusicNotAuthorized("fixture gate blocks before dispatch")))
    with pytest.raises(MusicNotAuthorized):
        service.execute_live(
            plan_dir, authorization={"live_authorized": True},
            client=object())
    assert not (tmp_path / "network").exists()


def test_extended_package_assembles_contract_and_verifies_hashes(extended_plan):
    service, plan_dir, _result = extended_plan
    clip, sample_rate, channels = _decode_pcm(plan_dir / "native.conditioning.wav")
    assert sample_rate == SR and channels == CH
    provider = array("f", clip)
    mask_start = 3 * sample_rate * channels
    mask_end = 51 * sample_rate * channels
    for index in range(mask_start, mask_end):
        provider[index] = provider[index] * 0.5
    result = service.package_candidate(
        plan_dir,
        RepaintOutput(samples=provider, sample_rate=sample_rate, channels=channels),
        synthetic=True,
        label="synthetic contract fixture (documented mask test, not model output)",
    )
    assert result["synthetic_fixture"] is True
    assert result["actual_provider_output_claimed"] is False
    assert result["live_execution_records"] is False
    assert result["apply_allowed"] is False
    assert result["project_mutated"] is False
    package = Path(result["package_path"])
    assert package.is_file()
    assert _sha(package) == result["package_sha256"]
    with zipfile.ZipFile(io.BytesIO(package.read_bytes())) as archive:
        names = set(archive.namelist())
        for name in [
            "manifest.json", "hashes.sha256", "plan.json",
            "native.request.json", "native.body", "native.conditioning.wav",
            "native.environment.json", "provider_output.raw",
            "provider_output.meta.json", "review_54s.wav",
            "candidate_master_309s.wav", "candidate_master_309s.mp3",
            "mapping.json", "assembly-recipe.txt",
        ]:
            assert name in names
        for line in archive.read("hashes.sha256").decode("utf-8").splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["synthetic_fixture"] is True
        assert manifest["actual_provider_output_claimed"] is False
        assert manifest["apply_allowed"] is False
        assert "submission_cap" in manifest["excluded"]
        assert "approval" in manifest["excluded"]
        assert hashlib.sha256(archive.read("provider_output.raw")).hexdigest() == (
            "58c42924375b9ff40efe2592cf619e690f01e38d89fa95b8a94071730335462d")
        assert hashlib.sha256(archive.read("review_54s.wav")).hexdigest() == (
            "0830bdd5e30f66c303fc646d96fcf9511857a948f81ae99a065b8bb28ad70f59")
        assert hashlib.sha256(archive.read("candidate_master_309s.wav")).hexdigest() == (
            "a7c7801e6b09fd2086ea4a6e937365a9d75637e1b30cadd5311209b3afbbaa2d")
        mp3_meta = manifest["mp3_derivative"]
        assert mp3_meta["lossy"] is True
        assert mp3_meta["bitrate"] == "320k"
        assert mp3_meta["pcm_equality_claims_apply"] is False
        assert hashlib.sha256(archive.read("candidate_master_309s.mp3")).hexdigest() == (
            mp3_meta["sha256"])
        mp3_path = plan_dir / "candidate_master_309s.mp3"
        if not mp3_path.exists():
            mp3_path.write_bytes(archive.read("candidate_master_309s.mp3"))
        mp3_samples, mp3_rate, mp3_channels = _decode_pcm(mp3_path)
        assert mp3_rate == SR and mp3_channels == CH
        assert len(mp3_samples) // mp3_channels >= 308 * SR
        master_path = plan_dir / "candidate_master_309s.wav"
        review_path = plan_dir / "review_54s.wav"
        if not master_path.exists():
            master_path.write_bytes(archive.read("candidate_master_309s.wav"))
        if not review_path.exists():
            review_path.write_bytes(archive.read("review_54s.wav"))
        master, rate, ch = _decode_pcm(master_path)
        review, _, _ = _decode_pcm(review_path)
        assert rate == SR and ch == CH
        assert len(master) // ch == 309 * SR
        assert len(review) // ch == 54 * SR
        original, _, _ = _decode_pcm(REPO / "projects/ac668b7c7faa/music/ace-step-519bd4ccf1.mp3")
        assert master[:185 * SR * CH] == original[:185 * SR * CH]
        assert master[233 * SR * CH:] == original[224 * SR * CH:]
        assert master[233 * SR * CH:294 * SR * CH] == original[224 * SR * CH:285 * SR * CH]
        assert master[294 * SR * CH:] == original[285 * SR * CH:]
        assert review[:3 * SR * CH] == original[182 * SR * CH:185 * SR * CH]
        assert review[51 * SR * CH:] == original[224 * SR * CH:227 * SR * CH]
    with pytest.raises(ExtendedBridgeBlocked, match="already exists"):
        service.package_candidate(
            plan_dir,
            RepaintOutput(samples=provider, sample_rate=sample_rate, channels=channels),
            synthetic=True,
        )


def test_extended_package_rejects_invalid_provider_output(extended_plan):
    service, plan_dir, _result = extended_plan
    clip, sample_rate, channels = _decode_pcm(plan_dir / "native.conditioning.wav")
    with pytest.raises(ExtendedBridgeBlocked, match="not exactly 54s"):
        service.package_candidate(
            plan_dir,
            RepaintOutput(samples=array("f", clip[:-channels]),
                          sample_rate=sample_rate, channels=channels),
            synthetic=True,
        )
    with pytest.raises(ExtendedBridgeBlocked, match="rate/channels"):
        service.package_candidate(
            plan_dir,
            RepaintOutput(samples=array("f", clip),
                          sample_rate=44100, channels=channels),
            synthetic=True,
        )
    nonfinite = array("f", clip)
    nonfinite[0] = float("nan")
    with pytest.raises(ExtendedBridgeBlocked, match="non-finite"):
        service.package_candidate(
            plan_dir,
            RepaintOutput(samples=nonfinite, sample_rate=sample_rate,
                          channels=channels),
            synthetic=True,
        )


def test_extended_package_rejects_changed_conditioning(extended_plan):
    service, plan_dir, _result = extended_plan
    clip_path = plan_dir / "native.conditioning.wav"
    before = clip_path.read_bytes()
    clip, sample_rate, channels = _decode_pcm(clip_path)
    try:
        clip_path.write_bytes(before + b"changed")
        with pytest.raises(ExtendedBridgeBlocked, match="conditioning bytes changed"):
            service.package_candidate(
                plan_dir,
                RepaintOutput(samples=array("f", clip),
                              sample_rate=sample_rate, channels=channels),
                synthetic=True,
            )
    finally:
        clip_path.write_bytes(before)


def test_extended_assembly_rejects_wrong_frame_count():
    original = array("f", [0.0]) * (300 * SR * CH)
    provider = array("f", [0.0]) * (53 * SR * CH)
    with pytest.raises(Exception, match="54s"):
        assemble_extended_bridge(original, provider)
