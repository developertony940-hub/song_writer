"""Smoke tests that don't require any runtime to be online.

Covers: settings load, project store CRUD, model router acquire/release,
schema validation. Uses tmp_path fixture to isolate the project root.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _isolate_projects_root(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # The Settings dataclass is frozen; override the module-level singleton so
    # get_settings() returns a copy whose projects_root points at tmp_path.
    from app.config.settings import get_settings, reload_settings
    import dataclasses
    s = get_settings()
    new = dataclasses.replace(s, projects_root=tmp_path)
    from app.config import settings as settings_module
    monkeypatch.setattr(settings_module, "_cached", new)
    from app.services import project_store as ps
    ps._store = None


def test_settings_load():
    from app.config import get_settings
    s = get_settings()
    assert s.name == "LocalAIContent"
    assert s.lmstudio.base_url.startswith("http")
    assert s.vram.gpu0_capacity_mb == 24576


def test_project_store_crud(tmp_path: Path):
    from app.models.schemas import ProjectCreate
    from app.services.project_store import get_store
    store = get_store()
    meta = store.create(ProjectCreate(name="Test", brief="b", brand_voice="v", target_audience="", notes=""))
    assert meta.name == "Test"
    assert (store.root / meta.id / "copy").is_dir()
    listed = store.list_projects()
    assert any(p.id == meta.id for p in listed)
    got = store.get(meta.id)
    assert got.brief == "b"
    updated = store.update(meta.id, notes="updated")
    assert updated.notes == "updated"
    store.delete(meta.id)
    with pytest.raises(KeyError):
        store.get(meta.id)


def test_asset_save_and_list(tmp_path: Path):
    from app.models.schemas import AssetKind, ProjectCreate
    from app.services.project_store import get_store
    store = get_store()
    proj = store.create(ProjectCreate(name="A", brief="", brand_voice="", target_audience="", notes=""))
    ref = store.save_asset(proj.id, AssetKind.copy, "hero.md", "# Hello", meta={"model": "qwen"})
    assert ref.kind == AssetKind.copy
    assert (store.root / proj.id / "copy" / "hero.md").read_text(encoding="utf-8") == "# Hello"
    assets = store.list_assets(proj.id, AssetKind.copy)
    assert len(assets) == 1 and assets[0].id == ref.id
    path = store.asset_path(proj.id, ref.id)
    assert path.exists()


def test_model_router_acquire_release():
    from app.services.model_router import get_router, reset_router
    reset_router()
    r = get_router()
    prof = asyncio.run(r.acquire("qwen3-coder-30b-a3b-instruct", "job1"))
    assert prof.slot.value == 0
    status = r.status()
    assert status["slots"]["0"]["current_model"] == "qwen3-coder-30b-a3b-instruct"
    asyncio.run(r.release("qwen3-coder-30b-a3b-instruct", "job1"))
    # lock cleared, model remains (release without unload keeps it resident)
    status_after = r.status()
    assert status_after["slots"]["0"]["locked_by"] is None
    assert status_after["slots"]["0"]["current_model"] == "qwen3-coder-30b-a3b-instruct"


def test_model_router_sequential_phasing():
    from app.services.model_router import get_router, reset_router
    reset_router()
    r = get_router()
    asyncio.run(r.acquire("qwen3-coder-30b-a3b-instruct", "j1"))
    asyncio.run(r.release("qwen3-coder-30b-a3b-instruct", "j1"))
    # Acquiring another GPU0 model after release may request unloading the prior one.
    asyncio.run(r.acquire("gemma-3-27b-it", "j2"))
    assert r.status()["slots"]["0"]["current_model"] == "gemma-3-27b-it"
    # Cross-GPU acquisition is independent.
    asyncio.run(r.acquire("flux_dev_fp8", "j3"))
    assert r.status()["slots"]["1"]["current_model"] == "flux_dev_fp8"


def test_model_router_unknown_model():
    from app.services.model_router import get_router, reset_router
    reset_router()
    r = get_router()
    # Unknown models are accepted with a 0-byte footprint on a guessed slot.
    prof = asyncio.run(r.acquire("some-random-llm-7b", "j1"))
    assert prof.category == "unknown"
    asyncio.run(r.release("some-random-llm-7b", "j1"))