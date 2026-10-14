"""Phase 0 compatibility tests for the presence-gated manifest fields.

These tests exercise manifest serialization in isolation. They never touch the
production ``projects/`` root: the ``store`` and ``client`` fixtures redirect
``projects_root`` to ``tmp_path`` and seed copies of checked-in fixtures.

Scope is scaffolding + compatibility only: no service requests, no generation,
no downloads/installs, no spending.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path

import pydantic
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifests"
_ASSET_DIRS = ("copy", "images", "video", "audio", "music", "final", "preview", "prompts", "reviews")


def _reset_singletons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings as settings_module
    from app.config.settings import get_settings
    from app.services import orchestrator as orch_module
    from app.services import project_store as ps
    from app.services.model_router import reset_router

    monkeypatch.setattr(
        settings_module, "_cached",
        dataclasses.replace(get_settings(), projects_root=tmp_path),
    )
    ps._store = None
    orch_module._orch = None
    reset_router()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset_singletons(tmp_path, monkeypatch)
    from app.services.project_store import get_store
    instance = get_store()
    yield instance
    import app.services.project_store as ps
    ps._store = None


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _reset_singletons(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as result:
        yield result


def seed_raw(store, raw: dict):
    pid = raw["id"]
    pdir = store.root / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "manifest.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    for name in _ASSET_DIRS:
        (pdir / name).mkdir(exist_ok=True)
    return pid, pdir


def seed(store, fixture_name: str):
    return seed_raw(store, json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8")))


def read_raw(pdir: Path) -> dict:
    return json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))


def _load_prev_schema():
    import sys
    path = FIXTURES.parent / "prev_schema.py"
    spec = importlib.util.spec_from_file_location("prev_schema_fixture", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module  # register so pydantic can resolve the module
    spec.loader.exec_module(module)
    module.PrevProjectMeta.model_rebuild()
    return module


def test_legacy_read_distinguishes_absent_from_stored(store):
    pid, pdir = seed(store, "legacy_minimal.json")
    meta = store.get(pid)
    assert meta.product is None
    assert meta.manifest_schema_version is None
    assert "product" not in meta.model_fields_set
    assert "manifest_schema_version" not in meta.model_fields_set
    raw = read_raw(pdir)
    assert "product" not in raw
    assert "manifest_schema_version" not in raw


def test_absent_unchanged_defaults_stay_absent(store):
    pid, pdir = seed(store, "legacy_minimal.json")
    store.set_mode(pid, "manual")  # equals the default: not an actual mutation
    raw = read_raw(pdir)
    assert "mode" not in raw
    assert "composition" not in raw
    assert "stages" not in raw
    assert "product" not in raw and "manifest_schema_version" not in raw


def test_actual_mutation_persists(store):
    pid, pdir = seed(store, "legacy_minimal.json")
    store.set_mode(pid, "auto")
    assert read_raw(pdir)["mode"] == "auto"
    store.save_composition(pid, {
        "total_duration_s": 30.0, "video_clips": [], "audio_clips": [], "subtitles": [],
    })
    composition = read_raw(pdir)["composition"]
    assert composition["total_duration_s"] == 30.0


def test_in_place_mutation_of_absent_container_persists(store):
    pid, pdir = seed(store, "legacy_minimal.json")
    assert "stages" not in read_raw(pdir)
    meta = store.get(pid)
    meta.stages["copy"] = "succeeded"  # in-place: model_fields_set is not a dirty tracker
    store._write_manifest(store._dir(pid), meta)
    assert read_raw(pdir)["stages"]["copy"] == "succeeded"


def test_unknown_keys_preserved_at_root_and_nested(store):
    pid, pdir = seed(store, "legacy_with_extras.json")
    store.set_mode(pid, "auto")
    raw = read_raw(pdir)
    assert raw["legacy_extra_root"] == {"nested": [1, 2, 3]}
    assert raw["legacy_flag"] is True
    assert raw["stages"]["custom_stage"] == "pending"
    assert raw["artifacts"]["custom_artifact"] == "keep-me"


def test_authoritative_structure_replaced_wholesale(store):
    pid, pdir = seed(store, "legacy_nested.json")
    assert read_raw(pdir)["composition"]["junk_key"] == "stale"
    store.save_composition(pid, {
        "total_duration_s": 30.0, "video_clips": [], "audio_clips": [], "subtitles": [],
    })
    assert "junk_key" not in read_raw(pdir)["composition"]


def test_explicit_null_product_and_version_are_rejected(store):
    pid, pdir = seed(store, "legacy_minimal.json")
    raw = read_raw(pdir)
    raw["product"] = None
    (pdir / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        store.get(pid)
    raw.pop("product")
    raw["manifest_schema_version"] = None
    (pdir / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        store.get(pid)


def test_new_project_omits_presence_gated_and_default_fields(store):
    from app.models.schemas import ProjectCreate
    meta = store.create(ProjectCreate(name="New", brief="b"))
    raw = read_raw(store.root / meta.id)
    assert "product" not in raw and "manifest_schema_version" not in raw
    assert "composition" not in raw and "stages" not in raw
    for required in ("id", "name", "brief", "created_at", "updated_at"):
        assert required in raw


def test_set_product_migrates_and_records_event(store):
    from app.models.schemas import Product, ProjectCreate
    meta = store.create(ProjectCreate(name="Mig", brief="b"))
    updated = store.set_product(meta.id, Product.audiobook)
    assert updated.product is Product.audiobook
    raw = read_raw(store.root / meta.id)
    assert raw["product"] == "audiobook"
    assert raw["manifest_schema_version"] == 1
    events = (store.root / meta.id / "reviews" / "manifest_events.jsonl").read_text(encoding="utf-8")
    assert "PRODUCT_MIGRATED" in events
    assert store.get(meta.id).product is Product.audiobook


def test_set_product_rejected_with_product_specific_artifact(store):
    from app.models.schemas import AssetKind, Product, ProjectCreate
    meta = store.create(ProjectCreate(name="Reject", brief="b"))
    store.save_asset(meta.id, AssetKind.audio, "chap.wav", b"x", meta={"product": "audiobook"})
    with pytest.raises(ValueError):
        store.set_product(meta.id, Product.podcast)


def test_new_format_manifest_parses_under_previous_schema(store):
    from app.models.schemas import Product, ProjectCreate
    meta = store.create(ProjectCreate(name="Compat", brief="b"))
    store.set_product(meta.id, Product.audiobook)
    raw = read_raw(store.root / meta.id)
    assert raw["product"] == "audiobook" and raw["manifest_schema_version"] == 1
    prev = _load_prev_schema()
    parsed = prev.PrevProjectMeta(**raw)  # unknown keys must be ignored, not fatal
    assert parsed.id == meta.id


def test_load_save_reload_roundtrip_is_stable(store):
    pid, pdir = seed(store, "legacy_with_extras.json")
    store.set_mode(pid, "auto")
    first = read_raw(pdir)
    store._write_manifest(store._dir(pid), store.get(pid))
    assert read_raw(pdir) == first


def test_api_responses_do_not_leak_presence_gated_fields(client):
    created = client.post("/api/projects", json={"name": "api", "brief": "b"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert "product" not in body
    assert "manifest_schema_version" not in body
    pid = body["id"]
    got = client.get(f"/api/projects/{pid}").json()
    assert "product" not in got and "manifest_schema_version" not in got
    listed = client.get("/api/projects").json()
    assert listed
    for row in listed:
        assert "product" not in row and "manifest_schema_version" not in row


# --- authoritative nested structures: preservation vs documented replacement ---


_PLAN = {
    "platform": "tiktok", "target_duration_s": 10.0,
    "hook": "Stop scrolling", "cta": "Try it today",
    "scenes": [
        {"id": "hook", "start_s": 0, "end_s": 5, "voiceover": "Stop scrolling now.",
         "caption": "Stop scrolling now.", "visual_prompt": "Vertical cinematic reveal",
         "motion_prompt": "Slow push in"},
        {"id": "cta", "start_s": 5, "end_s": 10, "voiceover": "Try it today.",
         "caption": "Try it today.", "visual_prompt": "Vertical confident call to action",
         "motion_prompt": "Gentle settle"},
    ],
}


def _authoritative_raw(pid: str) -> dict:
    return {
        "id": pid, "name": "Auth", "brief": "b", "brand_voice": "professional",
        "target_audience": "general", "notes": "",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "mode": "manual",
        "video_plan": {**_PLAN, "junk_plan": "keep"},
        "composition": {"total_duration_s": 30.0, "video_clips": [], "audio_clips": [],
                        "subtitles": [], "junk_key": "stale"},
        "preview_approval": {"preview_asset_id": "p", "junk_prev": "keep"},
    }


def test_replacement_policy_is_explicit(store):
    from app.services.project_store import _AUTHORITATIVE_REPLACE_KEYS, _NESTED_MERGE_KEYS
    assert set(_NESTED_MERGE_KEYS) == {"stages", "artifacts"}
    assert set(_AUTHORITATIVE_REPLACE_KEYS) == {"video_plan", "composition", "preview_approval"}


def test_unrelated_write_preserves_authoritative_nested_unknowns(store):
    pid, pdir = seed_raw(store, _authoritative_raw("dddddddddddd"))
    store.set_mode(pid, "auto")  # unrelated to any of the three structures
    raw = read_raw(pdir)
    assert raw["video_plan"]["junk_plan"] == "keep"
    assert raw["composition"]["junk_key"] == "stale"
    assert raw["preview_approval"]["junk_prev"] == "keep"


def test_owning_operations_replace_only_their_own_structure(store):
    from app.models.schemas import VideoPlan, video_plan_fingerprint
    pid, pdir = seed_raw(store, _authoritative_raw("dddddddddddd"))

    # composition: replaced by save_composition; the other two stay untouched
    store.save_composition(pid, {"total_duration_s": 30.0, "video_clips": [],
                                 "audio_clips": [], "subtitles": []})
    raw = read_raw(pdir)
    assert "junk_key" not in raw["composition"]
    assert raw["video_plan"]["junk_plan"] == "keep"
    assert raw["preview_approval"]["junk_prev"] == "keep"

    # preview_approval: replaced by set_preview_approval; video_plan untouched
    store.set_preview_approval(pid, {"preview_asset_id": "new",
                                     "approved_at": "2026-01-03T00:00:00Z"})
    raw = read_raw(pdir)
    assert "junk_prev" not in raw["preview_approval"]
    assert raw["video_plan"]["junk_plan"] == "keep"

    # video_plan: replaced by save_video_plan, which also clears preview_approval
    fingerprint = video_plan_fingerprint(VideoPlan(**_PLAN))
    store.save_video_plan(pid, dict(_PLAN), fingerprint)
    raw = read_raw(pdir)
    assert "junk_plan" not in raw["video_plan"]
    assert raw["video_plan"]["fingerprint"] == fingerprint
    assert raw["preview_approval"] is None


def test_api_mode_stage_composition_responses_do_not_leak(client):
    from app.models.schemas import AssetKind
    from app.services.project_store import get_store

    pid = client.post("/api/projects", json={"name": "api2", "brief": "b"}).json()["id"]

    def clean(payload):
        assert isinstance(payload, dict)
        assert "product" not in payload
        assert "manifest_schema_version" not in payload

    clean(client.patch(f"/api/projects/{pid}/mode", json={"mode": "auto"}).json())
    clean(client.put(f"/api/projects/{pid}/composition", json={"composition": {
        "total_duration_s": 30.0, "video_clips": [], "audio_clips": [], "subtitles": []}}).json())

    store = get_store()
    ref = store.save_asset(pid, AssetKind.copy, "plan.md", "copy")
    store.set_artifact(pid, "copy_plan", ref.id)
    store.update_stage(pid, "copy", "succeeded")
    stage = client.patch(f"/api/projects/{pid}/stage", json={"stage": "copy", "status": "approved"})
    assert stage.status_code == 200, stage.text
    clean(stage.json())
