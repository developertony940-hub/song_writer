"""Phase 4 representative/offline acceptance regression tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.models.schemas import AssetKind


def _fresh_acceptance(tmp_path: Path, monkeypatch):
    """Make singleton-backed services use an isolated deterministic project root."""
    from app.config import reload_settings
    from app.config import settings as settings_module
    from app.services import project_store, orchestrator as orchestrator_module
    from app.services.acceptance import AcceptanceService

    original_settings, original_store = settings_module._cached, project_store._store
    original_orchestrator = orchestrator_module._orch
    monkeypatch.setenv("LOCALAI_PROJECTS_ROOT", str(tmp_path / "projects"))
    # Real-state helpers exercise signing; offline runs remain keyless in the
    # service itself, but this test fixture supplies a stable private test key.
    monkeypatch.setenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET", "phase4-test-signing-secret")
    try:
        reload_settings()
        project_store._store = None
        orchestrator_module._orch = None
        return AcceptanceService()
    finally:
        settings_module._cached = original_settings
        project_store._store = original_store
        orchestrator_module._orch = original_orchestrator


def _nonfixture_proxy_state(acceptance, project, state):
    """Build a signed proxy checkpoint for unit tests unrelated to fixtures."""
    from app.services.acceptance import _plan

    asyncio.run(acceptance._run_offline(project.id, _plan()))
    for asset in acceptance.store.list_assets(project.id):
        acceptance.store.update_asset_meta(project.id, asset.id,
                                           acceptance_fixture=False, source="test_nonfixture")
    proxy = acceptance.store.list_assets(project.id, kind=AssetKind.preview)[-1]
    return acceptance._transition_with_snapshot(
        state, "awaiting_proxy_review", project.id, "proxy_ready", include_composition=True,
        proxy=acceptance._proxy_binding(project.id, proxy),
    )


def test_offline_acceptance_produces_qcd_master_and_both_report_formats(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    report = asyncio.run(acceptance.run("offline"))

    assert report["status"] == "passed_technical"
    assert report["preflight"]["ready"] is True
    assert report["master_audit"]["passed"] is True
    assert report["master_audit"]["qc"]["passed"] is True
    assert report["master_audit"]["stage_evidence"]["ok"] is True
    assert report["master_audit"]["captions"]["matches_plan"] is True
    for path in report["reports"].values():
        assert Path(path).is_file()
    persisted = json.loads(Path(report["reports"]["json"]).read_text(encoding="utf-8"))
    assert persisted["project_id"] == report["project_id"]
    assert "Technical master audit" in Path(report["reports"]["markdown"]).read_text(encoding="utf-8")


def test_master_audit_rejects_tampered_current_plan_evidence(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    report = asyncio.run(acceptance.run("offline"))
    master = acceptance.store.get_asset(report["project_id"], report["master_audit"]["master"]["asset_id"])
    acceptance.store.update_asset_meta(report["project_id"], master.id, plan_fingerprint="obsolete-plan")
    from app.services.acceptance import _plan
    audit = acceptance._audit_master(report["project_id"], acceptance.store.get_asset(report["project_id"], master.id), _plan())
    assert audit["passed"] is False
    assert any("fingerprint" in error for error in audit["errors"])


def test_offline_run_cannot_be_resumed_or_certified_as_real(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    offline = asyncio.run(acceptance.run("offline"))
    resumed = asyncio.run(acceptance.run("real", project_id=offline["project_id"], operator_approved=True))
    assert resumed["status"] == "failed"
    assert any("identity/mode" in item["message"] for item in resumed["failures"])
    assert resumed["reports"]


def test_invalid_project_id_returns_structured_failure(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    report = asyncio.run(acceptance.run("real", project_id="../../outside"))
    assert report["status"] == "failed"
    assert report["project_id"] == "../../outside"
    assert report["reports"] == {}
    assert any("invalid project ID" in item["message"] for item in report["failures"])


def test_proxy_binding_rejects_replaced_reviewed_source_bytes(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="binding", brief="test"))
    state = acceptance._create_state(project.id, "real")
    state = _nonfixture_proxy_state(acceptance, project, state)
    video = acceptance.store.list_assets(project.id, kind=AssetKind.video)[0]
    acceptance.store.asset_path(project.id, video.id).write_bytes(b"replaced")
    with pytest.raises(Exception, match="source media changed"):
        acceptance._assert_proxy_binding(project.id, state)


def test_master_audit_rejects_replaced_master_bytes(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    report = asyncio.run(acceptance.run("offline"))
    master = acceptance.store.get_asset(report["project_id"], report["master_audit"]["master"]["asset_id"])
    acceptance.store.asset_path(report["project_id"], master.id).write_bytes(b"not media")
    from app.services.acceptance import _plan
    audit = acceptance._audit_master(report["project_id"], acceptance.store.get_asset(report["project_id"], master.id), _plan())
    assert not audit["passed"]
    assert any("current master bytes" in error or "independent" in error for error in audit["errors"])


def test_exact_proxy_bytes_and_rejection_remediation_are_recorded(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="proxy", brief="test"))
    state = acceptance._create_state(project.id, "real")
    state = _nonfixture_proxy_state(acceptance, project, state)
    proxy = acceptance.store.list_assets(project.id, kind=AssetKind.preview)[-1]
    proxy_path = acceptance.store.asset_path(project.id, proxy.id)
    original = proxy_path.read_bytes()
    proxy_path.write_bytes(b"replaced proxy")
    with pytest.raises(Exception, match="proxy bytes changed"):
        acceptance._assert_proxy_binding(project.id, state)
    proxy_path.write_bytes(original)
    report: dict = {}
    assert asyncio.run(acceptance._resume_real(project.id, state, report, operator_approved=False, operator_rejected=True)) is None
    persisted = acceptance._load_state(project.id, "real")
    assert persisted["state"] == "awaiting_remediation"
    assert report["status"] == "pending_operator_remediation"


def test_real_first_run_waits_for_candidates_and_preflight_failure_creates_no_project(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)

    async def not_ready(_mode):
        return {"ready": False, "checks": {"fake": {"ok": False, "errors": ["unavailable"]}}}
    monkeypatch.setattr(acceptance, "_preflight", not_ready)
    blocked = asyncio.run(acceptance.run("real"))
    assert blocked["status"] == "failed"
    assert blocked["project_id"] is None
    assert acceptance.store.list_projects() == []

    async def ready(_mode):
        return {"ready": True, "checks": {"fake": {"ok": True}}}
    async def copy(_req):
        return None
    async def candidates(_req):
        return []
    monkeypatch.setattr(acceptance, "_preflight", ready)
    monkeypatch.setattr(acceptance.orch, "generate_copy", copy)
    monkeypatch.setattr(acceptance.orch, "generate_plan_candidates", candidates)
    pending = asyncio.run(acceptance.run("real"))
    assert pending["status"] == "pending_operator_candidate_review"
    assert acceptance.store.list_assets(pending["project_id"], AssetKind.preview) == []
    assert acceptance.store.list_candidates(pending["project_id"]) == []


def test_audit_requires_provenance_for_all_bound_source_assets(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    report = asyncio.run(acceptance.run("offline"))
    source = acceptance.store.list_assets(report["project_id"], AssetKind.audio)[0]
    acceptance.store.update_asset_meta(report["project_id"], source.id, provenance=None)
    master = acceptance.store.get_asset(report["project_id"], report["master_audit"]["master"]["asset_id"])
    from app.services.acceptance import _plan
    audit = acceptance._audit_master(report["project_id"], master, _plan())
    assert not audit["passed"]
    assert any("incomplete provenance" in error for error in audit["errors"])


def test_offline_state_mode_forgery_has_no_real_origin_signature(tmp_path: Path, monkeypatch):
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    offline = asyncio.run(acceptance.run("offline"))
    state_path = acceptance._state_path(offline["project_id"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    proxy = acceptance.store.list_assets(offline["project_id"], AssetKind.preview)[-1]
    state.update(mode="real", original_mode="real", state="awaiting_proxy_review",
                 proxy=acceptance._proxy_binding(offline["project_id"], proxy))
    state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed = asyncio.run(acceptance.run("real", project_id=offline["project_id"], operator_approved=True))
    assert resumed["status"] == "failed"
    assert any("origin signature" in item["message"] for item in resumed["failures"])


def test_real_state_rejects_missing_or_changed_signing_secret(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    from app.services.acceptance import AcceptanceError
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="signed", brief="test"))
    acceptance._create_state(project.id, "real")
    assert "phase4-test-signing-secret" not in acceptance._state_path(project.id).read_text(encoding="utf-8")
    monkeypatch.delenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET")
    from app.config import reload_settings
    reload_settings()
    with pytest.raises(AcceptanceError, match="requires LOCALAI_ACCEPTANCE_SIGNING_SECRET"):
        acceptance._load_state(project.id, "real")
    monkeypatch.setenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET", "a-different-test-signing-secret")
    with pytest.raises(AcceptanceError, match="signature is invalid"):
        acceptance._load_state(project.id, "real")
    from app.config import settings as settings_module
    settings_module._cached = None


def test_signed_operator_approved_state_requires_complete_exact_approval(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    from app.services.acceptance import AcceptanceError
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="approval", brief="test"))
    state = acceptance._create_state(project.id, "real")
    state = _nonfixture_proxy_state(acceptance, project, state)
    proxy = acceptance.store.list_assets(project.id, kind=AssetKind.preview)[-1]
    state = acceptance._transition(state, "operator_approved")
    with pytest.raises(AcceptanceError, match="complete signed approval record"):
        asyncio.run(acceptance._resume_real(project.id, state, {}, operator_approved=False, operator_rejected=False))

    approval = {"at": "2026-01-01T00:00:00+00:00", "project_id": project.id, "run_id": state["run_id"],
                "proxy_asset_id": proxy.id, "proxy_sha256": state["proxy"]["sha256"],
                "source_media_digest": "wrong", "source_media": state["proxy"]["source_media"]}
    state = acceptance._transition(state, "operator_approved", operator_approval=approval)
    with pytest.raises(AcceptanceError, match="does not bind the exact"):
        asyncio.run(acceptance._resume_real(project.id, state, {}, operator_approved=False, operator_rejected=False))


def test_signed_snapshot_rejects_fixture_marker_removal_from_assets_registry(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="fixture snapshot", brief="test"))
    state = acceptance._create_state(project.id, "real")
    from app.services.acceptance import _plan
    asyncio.run(acceptance._run_offline(project.id, _plan()))
    proxy = acceptance.store.list_assets(project.id, kind=AssetKind.preview)[-1]
    snapshot = {"version": acceptance._EVIDENCE_SNAPSHOT_VERSION, "label": "proxy_ready",
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "evidence": acceptance._project_evidence(project.id, include_composition=True)}
    state = acceptance._transition(state, "awaiting_proxy_review", evidence_snapshots=[snapshot],
                                   proxy=acceptance._proxy_binding(project.id, proxy))
    before = state["integrity"]["value"]

    assets_path = acceptance.store._dir(project.id) / "assets.json"
    rows = json.loads(assets_path.read_text(encoding="utf-8"))
    for row in rows:
        row["meta"].pop("acceptance_fixture", None)
        row["meta"].pop("source", None)
    assets_path.write_text(json.dumps(rows), encoding="utf-8")

    assert acceptance._load_state(project.id, "real")["integrity"]["value"] == before
    from app.services.acceptance import AcceptanceError
    with pytest.raises(AcceptanceError, match="signed evidence snapshot.*no longer matches"):
        acceptance._verify_evidence_snapshot(project.id, state, "proxy_ready")


def test_signed_snapshot_rejects_candidate_and_provenance_mutation(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    from app.services.acceptance import AcceptanceError
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="snapshot mutations", brief="test"))
    state = _nonfixture_proxy_state(acceptance, project, acceptance._create_state(project.id, "real"))
    candidate = acceptance.store.list_candidates(project.id)[0]
    acceptance.store.update_candidate(project.id, candidate["id"], review_reason="tampered review evidence")
    assert acceptance._load_state(project.id, "real")
    with pytest.raises(AcceptanceError, match="signed evidence snapshot.*no longer matches"):
        acceptance._verify_evidence_snapshot(project.id, state, "proxy_ready")

    acceptance.store.update_candidate(project.id, candidate["id"], review_reason=candidate.get("review_reason"))
    asset = acceptance.store.list_assets(project.id, AssetKind.video)[0]
    acceptance.store.update_asset_meta(project.id, asset.id, provenance={"tampered": True})
    with pytest.raises(AcceptanceError, match="signed evidence snapshot.*no longer matches"):
        acceptance._verify_evidence_snapshot(project.id, state, "proxy_ready")


def test_real_certification_rejects_synthetic_fixture_origin(tmp_path: Path, monkeypatch):
    from app.models.schemas import ProjectCreate
    from app.services.acceptance import AcceptanceError, _plan
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    project = acceptance.store.create(ProjectCreate(name="fixture defense", brief="test"))
    acceptance._create_state(project.id, "real")
    asyncio.run(acceptance._run_offline(project.id, _plan()))
    with pytest.raises(AcceptanceError, match="rejects deterministic acceptance-fixture"):
        acceptance._assert_no_synthetic_fixture_origin(project.id)


def test_real_run_without_signing_secret_fails_before_project_creation(tmp_path: Path, monkeypatch):
    """No key means no preflight/generation/project side effects for real mode."""
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    monkeypatch.delenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET")
    from app.config import settings as settings_module
    settings_module._cached = None
    report = asyncio.run(acceptance.run("real"))
    assert report["status"] == "failed"
    assert report["project_id"] is None
    assert acceptance.store.list_projects() == []
    assert any("LOCALAI_ACCEPTANCE_SIGNING_SECRET" in item["message"] for item in report["failures"])


def test_offline_acceptance_needs_no_signing_secret(tmp_path: Path, monkeypatch):
    """The signing boundary is deliberately real-mode-only."""
    acceptance = _fresh_acceptance(tmp_path, monkeypatch)
    monkeypatch.delenv("LOCALAI_ACCEPTANCE_SIGNING_SECRET")
    from app.config import settings as settings_module
    settings_module._cached = None
    report = asyncio.run(acceptance.run("offline"))
    assert report["status"] == "passed_technical"


def test_real_acceptance_reloads_persisted_master_snapshot_before_certification(tmp_path: Path, monkeypatch):
    """A completed real run certifies the freshly persisted master checkpoint.

    The worker-side master transition is deliberately made detached from the
    caller's in-memory state object. This models the stale-state boundary that
    existed before ``run`` reloaded ``acceptance-run.json`` after rendering.
    """
    from copy import deepcopy
    from app.models.schemas import CandidateRecord, GeneratePlanCandidatesRequest, PlanCandidateKind, ReviewCandidateRequest
    from app.services.provenance import asset_provenance

    acceptance = _fresh_acceptance(tmp_path, monkeypatch)

    async def ready(mode):
        assert mode == "real"
        return {"ready": True, "checks": {"test": {"ok": True}}}

    async def fake_copy(_request):
        # The real plan artifact supplies canonical copy-stage evidence.
        return None

    async def fake_candidates(request: GeneratePlanCandidatesRequest):
        """Mock provider output while preserving real candidate registry shape."""
        plan, fingerprint = acceptance.orch.production.get_plan(request.project_id)
        generated = []
        for scene in plan.scenes:
            reference = None
            if request.kind == PlanCandidateKind.image:
                # Image bytes are review evidence but not composition media.
                asset = acceptance.store.save_asset(
                    request.project_id, AssetKind.image, f"mock_{scene.id}.png", b"mock image bytes",
                    meta={"source": "test_mock_provider", "provenance": asset_provenance(
                        prompt_id="image.generate", rendered_prompt=scene.visual_prompt)},
                )
            else:
                selected = acceptance.orch.production.selected(
                    request.project_id, fingerprint, scene.id, PlanCandidateKind.image)
                assert selected is not None
                reference = selected.asset_id
                path = acceptance.store.asset_dir(request.project_id, AssetKind.video) / f"mock_{scene.id}.mp4"
                rc, error = await acceptance.ffmpeg._run([
                    "-f", "lavfi", "-i", f"color=c=0x264653:s=320x568:r=30:d={scene.end_s - scene.start_s}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(path),
                ])
                assert rc == 0, error
                asset = acceptance.store.save_asset(
                    request.project_id, AssetKind.video, path.name, None,
                    meta={"source": "test_mock_provider", "provenance": asset_provenance(
                        prompt_id="video.generate", rendered_prompt=scene.motion_prompt)},
                )
            record = CandidateRecord(
                id=f"{request.kind.value}-{scene.id}", project_id=request.project_id,
                plan_fingerprint=fingerprint, scene_id=scene.id, kind=request.kind,
                asset_id=asset.id, seed=1, reference_asset_id=reference,
            )
            acceptance.store.update_asset_meta(
                request.project_id, asset.id, candidate_id=record.id, plan_fingerprint=fingerprint,
                scene_id=scene.id, candidate_kind=request.kind.value, seed=record.seed,
                reference_asset_id=reference,
            )
            acceptance.store.add_candidate(request.project_id, record.model_dump(mode="json"))
            generated.append(record)
        return generated

    async def fake_audio(project_id: str):
        """Provide valid local media, replacing only the external TTS boundary."""
        plan, fingerprint = acceptance.orch.production.get_plan(project_id)
        created = []
        for index, scene in enumerate(plan.scenes):
            path = acceptance.store.asset_dir(project_id, AssetKind.audio) / f"mock_{scene.id}.wav"
            rc, error = await acceptance.ffmpeg._run([
                "-f", "lavfi", "-i", f"sine=frequency={440 + index * 100}:sample_rate=48000:duration={scene.end_s - scene.start_s}",
                "-af", "volume=0.12", "-ac", "2", "-ar", "48000", "-y", str(path),
            ])
            assert rc == 0, error
            asset = acceptance.store.save_asset(
                project_id, AssetKind.audio, path.name, None,
                meta={"source": "VideoPlan.voiceover", "plan_fingerprint": fingerprint, "scene_id": scene.id,
                      "timing": {"start_s": scene.start_s, "end_s": scene.end_s},
                      "provenance": asset_provenance(prompt_id="audio.synthesize", rendered_prompt=scene.voiceover)},
            )
            created.append(asset)
        acceptance.store.update_stage(project_id, "audio", "succeeded")
        return created

    monkeypatch.setattr(acceptance, "_preflight", ready)
    monkeypatch.setattr(acceptance.orch, "generate_copy", fake_copy)
    monkeypatch.setattr(acceptance.orch, "generate_plan_candidates", fake_candidates)
    monkeypatch.setattr(acceptance.orch, "generate_plan_audio", fake_audio)

    first = asyncio.run(acceptance.run("real"))
    assert first["status"] == "pending_operator_candidate_review"
    project_id = first["project_id"]
    assert acceptance._load_state(project_id, "real")["state"] == "awaiting_image_review"
    for candidate in acceptance.store.list_candidates(project_id):
        acceptance.orch.review_candidate(project_id, candidate["id"], ReviewCandidateRequest(
            status="accepted", select=True, reason="operator selected image candidate"))

    second = asyncio.run(acceptance.run("real", project_id=project_id))
    assert second["status"] == "pending_operator_candidate_review"
    assert acceptance._load_state(project_id, "real")["state"] == "awaiting_video_review"
    for candidate in acceptance.store.list_candidates(project_id):
        if candidate["kind"] == PlanCandidateKind.video.value:
            acceptance.orch.review_candidate(project_id, candidate["id"], ReviewCandidateRequest(
                status="accepted", select=True, reason="operator selected motion candidate"))

    proxy_pending = asyncio.run(acceptance.run("real", project_id=project_id))
    assert proxy_pending["status"] == "pending_operator_proxy_review"
    proxy_state = acceptance._load_state(project_id, "real")
    assert proxy_state["state"] == "awaiting_proxy_review"
    assert acceptance._snapshot(proxy_state, "proxy_ready")["evidence"]["linkage"]["composition"] is not None

    # Detach only the persisted master checkpoint from the caller's state. The
    # old code would validate ``master_rendered`` on the stale approved state.
    transition_with_snapshot = acceptance._transition_with_snapshot

    def detached_master_transition(state, next_state, checkpoint_project_id, label, *, include_composition=False, **changes):
        if label == "master_rendered":
            return transition_with_snapshot(deepcopy(state), next_state, checkpoint_project_id, label,
                                            include_composition=include_composition, **changes)
        return transition_with_snapshot(state, next_state, checkpoint_project_id, label,
                                        include_composition=include_composition, **changes)

    load_states, verified_labels = [], []
    load_state, verify_snapshot = acceptance._load_state, acceptance._verify_evidence_snapshot

    def tracked_load(project, mode):
        loaded = load_state(project, mode)
        load_states.append(loaded["state"])
        return loaded

    def tracked_snapshot(project, state, label, **kwargs):
        verified_labels.append(label)
        return verify_snapshot(project, state, label, **kwargs)

    monkeypatch.setattr(acceptance, "_transition_with_snapshot", detached_master_transition)
    monkeypatch.setattr(acceptance, "_load_state", tracked_load)
    monkeypatch.setattr(acceptance, "_verify_evidence_snapshot", tracked_snapshot)
    completed = asyncio.run(acceptance.run("real", project_id=project_id, operator_approved=True))

    assert completed["status"] == "passed_technical_operator_attested"
    assert completed["failures"] == []
    assert load_states == ["awaiting_proxy_review", "master_rendered"]
    assert verified_labels.count("master_rendered") >= 2
    state = acceptance._load_state(project_id, "real")
    assert state["state"] == "completed"
    assert acceptance._snapshot(state, "master_rendered")["evidence"]["linkage"]["composition"] is not None
    assert completed["master_audit"]["passed"] is True
