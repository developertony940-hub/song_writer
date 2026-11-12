"""Tests for the project's submission budget.

Offline only: no provider is contacted, no GPU is touched and no live
project is modified.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from app.config import settings as settings_module
from app.models.music import MusicSettings
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.model_router import reset_router
from app.services.music_service import MusicConflict, MusicService


class FakeRouter:
    async def acquire(self, model, job_id, *, allow_unload=True): return None
    async def release(self, model, job_id, unload=False): return None
    def status(self): return {"slots": {}, "holds": {}}


class FakeComfy:
    async def status(self): return {"online": True, "base_url": "fake", "gpus": []}

    async def submit_music(self, workflow, patches):
        raise AssertionError("the cap tests must never submit a generation")


def _settings() -> MusicSettings:
    return MusicSettings.model_validate({
        "musical_direction": "Southern gothic with fiddle and banjo.",
        "lyrics": "[Verse]\nA line of the song,\nAnother line here.\n",
        "duration_s": 60, "bpm": 120, "time_signature": "4", "key_scale": "E minor",
    })


class SubmissionCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        base = settings_module.get_settings()
        self._cached = settings_module._cached
        settings_module._cached = dataclasses.replace(
            base, projects_root=root,
            music=dataclasses.replace(base.music, live_generation_authorized=False))
        ps._store = None
        music_module._service = None
        reset_router()
        self.service = MusicService(comfy=FakeComfy(), router=FakeRouter())
        self.pid = self.service.store.create(ProjectCreate(name="caps", brief="")).id
        self.service.save_draft(self.pid, _settings())

    def tearDown(self) -> None:
        settings_module._cached = self._cached
        ps._store = None
        music_module._service = None
        reset_router()
        self._tmp.cleanup()

    def test_approving_a_draft_cannot_raise_the_project_budget(self):
        self.service.approve(self.pid, "first approval", 2)
        with self.assertRaises(MusicConflict):
            self.service.approve(self.pid, "cannot raise", 3)
        state = self.service._load_state(self.pid)
        self.assertEqual(state.lifetime_cap, 2)

    def test_the_explicit_action_raises_the_budget(self):
        self.service.approve(self.pid, "first approval", 2)
        result = self.service.set_submission_cap(
            self.pid, 10, "operator wants more iterations")
        self.assertEqual(result["previous_cap"], 2)
        self.assertEqual(result["lifetime_cap"], 10)
        self.assertEqual(result["cap_remaining"], 10)
        state = self.service._load_state(self.pid)
        self.assertEqual(state.lifetime_cap, 10)

    def test_the_explicit_action_cannot_exceed_the_configured_ceiling(self):
        ceiling = settings_module.get_settings().music.max_submission_cap
        with self.assertRaises(MusicConflict):
            self.service.set_submission_cap(self.pid, ceiling + 1, "too many")
        state = self.service._load_state(self.pid)
        self.assertIsNone(state.lifetime_cap)

    def test_the_explicit_action_requires_a_note(self):
        with self.assertRaises(MusicConflict):
            self.service.set_submission_cap(self.pid, 10, "   ")

    def test_the_budget_cannot_be_set_below_what_was_already_spent(self):
        self.service.approve(self.pid, "first approval", 2)
        journal_path = self.service._journal_path(self.pid)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as handle:
            for attempt_id in ("a1", "a2"):
                handle.write(json.dumps({"kind": "attempt_started",
                                         "attempt_id": attempt_id}) + "\n")
                handle.write(json.dumps({"kind": "attempt_completed",
                                         "attempt_id": attempt_id}) + "\n")
        self.assertEqual(self.service._journal_state(self.pid)["submissions"], 2)
        with self.assertRaises(MusicConflict):
            self.service.set_submission_cap(self.pid, 1, "too low")
        state = self.service._load_state(self.pid)
        self.assertEqual(state.lifetime_cap, 2)

    def test_the_budget_may_be_set_to_exactly_what_was_spent(self):
        self.service.approve(self.pid, "first approval", 2)
        journal_path = self.service._journal_path(self.pid)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "attempt_started",
                                     "attempt_id": "a1"}) + "\n")
            handle.write(json.dumps({"kind": "attempt_completed",
                                     "attempt_id": "a1"}) + "\n")
        result = self.service.set_submission_cap(self.pid, 1, "stop here")
        self.assertEqual(result["cap_remaining"], 0)

    def test_approving_with_a_smaller_cap_does_not_shrink_the_budget(self):
        self.service.approve(self.pid, "first approval", 2)
        self.service.set_submission_cap(self.pid, 10, "more iterations")
        self.service.approve(self.pid, "re-approved for the next iteration", 10)
        state = self.service._load_state(self.pid)
        self.assertEqual(state.lifetime_cap, 10)
        self.assertEqual(state.approval.submission_cap, 10)
        self.assertEqual(self.service._effective_cap(state), 10)

    def test_a_lower_approval_still_bounds_the_current_draft(self):
        self.service.approve(self.pid, "first approval", 2)
        self.service.set_submission_cap(self.pid, 10, "more iterations")
        self.service.approve(self.pid, "one render for this draft", 1)
        state = self.service._load_state(self.pid)
        self.assertEqual(state.lifetime_cap, 10)
        self.assertEqual(self.service._effective_cap(state), 1)


if __name__ == "__main__":
    unittest.main()
