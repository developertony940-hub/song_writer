"""Verification test: an approval cannot raise an existing lifetime cap (W1).

This exercises the unchanged ``MusicService`` against a temporary projects
root. It is a regression guard for an invariant that already exists in the
source; it is not a code change.
"""
from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import pytest

from app.config import settings as settings_module
from app.models.music import MusicSettings
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.model_router import reset_router
from app.services.music_service import MusicConflict, MusicService


class FakeRouter:
    async def acquire(self, model, job_id, *, allow_unload=True):
        return None

    async def release(self, model, job_id, unload=False):
        return None


class FakeComfy:
    async def status(self):
        return {"online": False, "base_url": "fake", "gpus": []}

    async def submit_music(self, workflow, patches):
        raise AssertionError("no generation may happen in this test")


def test_approval_cannot_raise_an_existing_lifetime_cap():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        base = settings_module.get_settings()
        cached = settings_module._cached
        settings_module._cached = dataclasses.replace(
            base, projects_root=root,
            music=dataclasses.replace(base.music, live_generation_authorized=False))
        ps._store = None
        music_module._service = None
        reset_router()
        try:
            service = MusicService(comfy=FakeComfy(), router=FakeRouter())
            pid = service.store.create(ProjectCreate(name="cap-invariant", brief="")).id
            service.save_draft(pid, MusicSettings(
                musical_direction="Southern gothic with fiddle and banjo.",
                lyrics="[Verse 1]\nA line of the song,\nAnother line here.\n",
                duration_s=60, bpm=120, time_signature="4", key_scale="E minor",
                arrangement={"Verse 1": "Sparse guitar and banjo."},
            ), revision_name="draft 1", source="draft")

            service.set_submission_cap(pid, 2, "initial budget")
            with pytest.raises(MusicConflict):
                service.approve(pid, "raise it", 3)
            approved = service.approve(pid, "keep it", 2)
            assert approved["lifetime_cap"] == 2
            assert approved["approval"]["submission_cap"] == 2
        finally:
            settings_module._cached = cached
            ps._store = None
            music_module._service = None
            reset_router()
