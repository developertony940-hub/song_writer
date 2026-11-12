"""Prompt-to-draft safety: sanitisation, exact operator text, stanza breaks.

The agent pipeline sanitises the brief; the draft service must not be the
weaker path. These tests use a fake assistant and a temporary projects root.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.agents.records import read_run_records
from app.config import settings as settings_module
from app.models.music import MusicDraftFromPromptRequest
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import project_store as ps
from app.services.brief_safety import sanitize_brief
from app.services.model_router import reset_router
from app.services.music_draft_service import (
    MusicDraftGenerationError, MusicDraftService,
)
from app.services.music_service import MusicService

APPROVED_VERSE3 = (
    "Maybe those old friends are still out there somewhere,\n"
    "Maybe the same old band still plays the songs we shared.\n"
    "And maybe we've got a few more miles on these old boots,\n"
    "But, baby, there's still a two-step left in me and you.\n"
    "\n"
    "We don't have to be young again,\n"
    "We don't have to turn back time,\n"
    "Just let me hold you like I did back then,\n"
    "And let that old fiddle play one more time."
)


class FakeRouter:
    async def acquire(self, model, job_id, *, allow_unload=True):
        return None

    async def release(self, model, job_id, unload=False):
        return None


class FakeComfy:
    async def status(self):
        return {"online": False, "base_url": "fake", "gpus": []}


def valid_response() -> str:
    return json.dumps({
        "title": "Sanitize",
        "musical_direction": "Southern gothic two-step with fiddle and banjo.",
        "lyrics": ("[Verse 1]\nFake line one,\nFake line two.\n"
                   "[Chorus]\nFake chorus one,\nFake chorus two."),
        "duration_s": 60,
        "bpm": 96,
        "time_signature": "4",
        "key_scale": "E minor",
        "arrangement": {"Verse 1": "Sparse guitar.", "Chorus": "Full band."},
    })


class FakeLMS:
    default_model = "test-assistant"

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer if answer is not None else valid_response()

    async def chat_text(self, prompt, **kwargs):
        return self.answer


class SanitizeTests(unittest.TestCase):
    def _run(self, prompt: str, answer: str | None = None):
        folder = tempfile.TemporaryDirectory()
        root = Path(folder.name)
        base = settings_module.get_settings()
        cached = settings_module._cached
        settings_module._cached = dataclasses.replace(
            base, projects_root=root,
            music=dataclasses.replace(base.music,
                                      live_generation_authorized=False))
        ps._store = None
        music_module._service = None
        reset_router()
        try:
            service = MusicService(comfy=FakeComfy(), router=FakeRouter())
            pid = service.store.create(
                ProjectCreate(name="sanitize", brief="")).id
            drafts = MusicDraftService(
                service=service, lms=FakeLMS(answer), router=FakeRouter(),
                store=service.store)
            try:
                result = asyncio.run(drafts.generate(pid, prompt))
                error = None
            except Exception as exc:  # noqa: BLE001 - asserted by the caller
                result, error = None, exc
            state = service._load_state(pid)
            rows = read_run_records(service._state_dir(pid))
            return result, error, state, rows, folder
        finally:
            settings_module._cached = cached
            ps._store = None
            music_module._service = None
            reset_router()

    def test_control_bytes_do_not_reach_saved_lyrics(self):
        prompt = (
            "Chord progression: Em -> G -> D -> B7\n"
            "[Verse 1]\nOperator\x00line one,\nOperator line two,\n"
            "Operator line three,\nOperator line four.\n"
        )
        result, error, state, _, folder = self._run(prompt)
        try:
            self.assertIsNone(error)
            self.assertNotIn("\x00", result["settings"]["lyrics"])
            self.assertIn("Operator line one,", result["settings"]["lyrics"])
            self.assertNotIn("\x00", state.draft.settings.lyrics)
        finally:
            folder.cleanup()

    def test_exact_operator_text_and_stanza_break_survive(self):
        prompt = "Chord progression: Em -> G -> D -> B7\n[Verse 1]\n" + APPROVED_VERSE3 + "\n"
        result, error, state, _, folder = self._run(prompt)
        try:
            self.assertIsNone(error)
            lyrics = result["settings"]["lyrics"]
            self.assertIn(APPROVED_VERSE3, lyrics)
            self.assertIn("\n\nWe don't have to be young again,", lyrics)
            self.assertIn("And let that old fiddle play one more time.", lyrics)
            self.assertEqual(state.draft.settings.lyrics.rstrip("\n"),
                             lyrics.rstrip("\n"))
        finally:
            folder.cleanup()

    def test_sanitizer_preserves_punctuation_and_blank_lines(self):
        text = "Line one \u2014 \u201cquoted\u201d\n\nLine two\x00\n```text\nLine three\n```"
        cleaned = sanitize_brief(text)
        self.assertIn("Line one \u2014 \u201cquoted\u201d", cleaned)
        self.assertIn("\n\n", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("```", cleaned)
        self.assertIn("Line three", cleaned)

    def test_overlarge_prompt_is_rejected_before_any_call(self):
        with self.assertRaises(ValidationError):
            MusicDraftFromPromptRequest(project_id="a" * 12, prompt="x" * 200001)

    def test_malformed_response_fails_visibly_and_records_a_ledger_row(self):
        prompt = "Chord progression: Em -> G -> D -> B7\n[Verse 1]\nOperator line one,\nOperator line two,\n"
        result, error, state, rows, folder = self._run(prompt, answer="{not valid json")
        try:
            self.assertIsNone(result)
            self.assertIsInstance(error, MusicDraftGenerationError)
            self.assertTrue(rows)
            self.assertEqual(rows[-1]["outcome"], "failed")
            self.assertIsNone(state.draft)
        finally:
            folder.cleanup()


if __name__ == "__main__":
    unittest.main()
