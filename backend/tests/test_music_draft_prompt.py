"""Prompt-to-draft checks: coercion, completion, repair, and no authority.

Written in the stdlib unittest style so they run offline with
``python -m unittest`` and are still collected by pytest.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from app.config import settings as settings_module
from app.models.music import MusicState
from app.models.schemas import ProjectCreate
from app.services import music_service as music_module
from app.services import orchestrator as orch_module
from app.services import project_store as ps
from app.services.model_router import reset_router
from app.services.music_draft_service import (
    MusicDraftGenerationError, MusicDraftService, bpm_from_source,
    build_draft_settings, build_user_prompt, coerce_bpm, coerce_duration,
    coerce_key, coerce_time_signature, duration_from_source,
    ensure_arrangement_covers_tags, estimate_duration, extract_json_object,
    key_from_source, normalise_arrangement, normalise_lyrics,
)
from app.services.music_service import MusicService


class FakeRouter:
    """A cooperative lease stub: records acquisition without a real GPU."""

    def __init__(self) -> None:
        self.acquire_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []

    async def acquire(self, model: str, job_id: str, *, allow_unload: bool = True) -> None:
        self.acquire_calls.append((model, job_id))

    async def release(self, model: str, job_id: str, unload: bool = False) -> None:
        self.release_calls.append((model, job_id))

    def status(self) -> dict:
        return {"slots": {}, "holds": {}}


class FakeComfy:
    """A generator stub that refuses to submit anything."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict]] = []

    async def status(self) -> dict:
        return {"online": True, "base_url": "fake", "gpus": []}

    async def submit_music(self, workflow: str, patches: dict) -> str:
        self.submissions.append((workflow, patches))
        raise AssertionError("the prompt path must never submit a generation")


class FakeLMS:
    """A local assistant that replays canned answers."""

    default_model = "test-assistant"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_text(self, prompt: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.responses:
            raise AssertionError("the assistant was called more times than expected")
        return self.responses.pop(0)


def good_response(**overrides) -> str:
    payload = {
        "title": "Test Song",
        "musical_direction": "Warm electronic pop with a bright female lead vocal.",
        "lyrics": ("[Verse 1]\nA quiet room, a borrowed light,\nA melody that learns to write.\n"
                   "[Chorus]\nStay with me through the fade,\nEvery word that we made."),
        "duration_s": 45,
        "bpm": 110,
        "time_signature": "4",
        "key_scale": "C major",
        "language": "en",
        "arrangement": {"Verse 1": "Sparse piano and soft pads.",
                        "Chorus": "Full band lift with a wide chorus."},
        "mix_chain": ("Large diaphragm condenser, high-pass at 90 Hz, 1176 then LA-2A, "
                      "slapback at 90 ms, dark hall reverb on the bridge, bus compression "
                      "at 2 dB, limiter at -1.0 dBFS."),
        "references": ["Brian Setzer", "Some Song"],
    }
    payload.update(overrides)
    return json.dumps(payload)


class HelperTests(unittest.TestCase):
    def test_extract_json_tolerates_fences_prose_and_braces_in_strings(self):
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json_object('Here you go:\n{"a": {"b": 2}}\nthanks'),
                         {"a": {"b": 2}})
        self.assertTrue(
            extract_json_object('{"lyrics": "He said, \\"hello\\" {now}"}')["lyrics"].endswith("{now}"))
        with self.assertRaises(MusicDraftGenerationError):
            extract_json_object("no json here")

    def test_extract_json_tolerates_raw_newlines_inside_strings(self):
        raw = '{"lyrics": "[Verse 1]\nLine one,\nLine two.", "bpm": 125}'
        data = extract_json_object(raw)
        self.assertIn("Line one,", data["lyrics"])
        self.assertEqual(data["bpm"], 125)

    def test_extract_json_unwraps_a_single_nested_draft_object(self):
        raw = '{"draft": {"lyrics": "[Verse]\\nA line", "musical_direction": "Warm pop."}}'
        data = extract_json_object(raw)
        self.assertIn("lyrics", data)
        self.assertEqual(data["musical_direction"], "Warm pop.")

    def test_lyrics_are_split_into_tag_per_line_and_stage_directions_move_out(self):
        lyrics, notes, stage = normalise_lyrics(
            "[Verse 1] First line here\n[Bridge]\nSecond line\n(Crowd roar SFX)\n")
        self.assertTrue(lyrics.startswith("[Verse 1]\nFirst line here\n[Bridge]\nSecond line"))
        self.assertEqual(stage["Bridge"], ["(Crowd roar SFX)"])
        self.assertTrue(any("stage direction" in note for note in notes))

    def test_section_tags_with_timestamps_are_normalised(self):
        lyrics, notes, _ = normalise_lyrics("[Verse 1 (0:30 - 0:58)]\nA line\n")
        self.assertTrue(lyrics.startswith("[Verse 1]"))
        self.assertTrue(any("normalised" in note for note in notes))

    def test_arrangement_keeps_only_tags_and_uses_their_exact_spelling(self):
        arrangement, notes = normalise_arrangement(
            {"verse 1": "quiet piano", "Bridge": "drums out", "Ghost": "orphan"},
            tags=["Verse 1", "Bridge"])
        self.assertEqual(set(arrangement), {"Verse 1", "Bridge"})
        self.assertEqual(arrangement["Verse 1"], "quiet piano")
        self.assertTrue(any("Ghost" in note for note in notes))

    def test_long_directives_and_too_many_sections_are_capped(self):
        arrangement, notes = normalise_arrangement(
            {f"Verse {index}": "x" * 400 for index in range(1, 25)},
            tags=[f"Verse {index}" for index in range(1, 25)])
        self.assertEqual(len(arrangement), 20)
        self.assertTrue(all(len(value) <= 300 for value in arrangement.values()))
        self.assertTrue(any("300 characters" in note for note in notes))

    def test_missing_directives_are_filled_for_every_tag(self):
        arrangement, notes = ensure_arrangement_covers_tags(
            {"Verse 1": "quiet"}, ["Verse 1", "Chorus", "Bridge"],
            ("intro", "verse", "chorus", "bridge", "outro", "inst"))
        self.assertEqual(set(arrangement), {"Verse 1", "Chorus", "Bridge"})
        self.assertIn("wide pads", arrangement["Chorus"])
        self.assertEqual(len(notes), 2)

    def test_scalars_are_coerced(self):
        self.assertEqual(coerce_key("D minor"), "D minor")
        self.assertEqual(coerce_key("d minor"), "D minor")
        self.assertEqual(coerce_key("Dm"), "D minor")
        self.assertEqual(coerce_key("Bb major"), "Bb major")
        self.assertIsNone(coerce_key("H minor"))
        self.assertEqual(coerce_time_signature("4/4"), "4")
        self.assertEqual(coerce_time_signature("3/4"), "3")
        self.assertEqual(coerce_time_signature("6/8"), "6")
        self.assertIsNone(coerce_time_signature("5/4"))
        self.assertEqual(coerce_duration("4:45"), 285.0)
        self.assertIsNone(coerce_duration(700))
        self.assertEqual(coerce_bpm("125 BPM"), 125)

    def test_source_values_are_read_when_the_assistant_omits_them(self):
        source = "Tempo 118 BPM. Key: D Minor. Total duration 5:30."
        self.assertEqual(bpm_from_source(source), 118)
        self.assertEqual(key_from_source(source), "D minor")
        self.assertEqual(duration_from_source(source), 330.0)

    def test_duration_is_estimated_from_lyric_lines(self):
        lyrics = "\n".join(["[Verse 1]"] + [f"Line {index}" for index in range(20)])
        self.assertEqual(estimate_duration(lyrics), 70.0)

    def test_build_draft_settings_reports_problems_and_fills_gaps(self):
        candidate, filled, problems = build_draft_settings(
            {"lyrics": "[Verse]\nOne line\nTwo lines\nThree lines\nFour lines\n",
             "musical_direction": "Warm pop."},
            source="Tempo 118 BPM. Key: D Minor.", known_sections=("verse",))
        self.assertEqual(problems, [])
        self.assertEqual(candidate["bpm"], 118)
        self.assertEqual(candidate["key_scale"], "D minor")
        self.assertEqual(candidate["time_signature"], "4")
        self.assertGreater(candidate["duration_s"], 10)
        self.assertNotIn("Chorus", candidate["arrangement"])
        self.assertTrue(any("duration_s" in note for note in filled))

        _, _, problems = build_draft_settings({"lyrics": "", "musical_direction": ""})
        self.assertTrue(problems)

    def test_bare_section_tags_are_rejected_as_incomplete(self):
        _, _, problems = build_draft_settings({
            "musical_direction": "Warm pop with a clear vocal.",
            "lyrics": "[Intro]\n[Verse 1]\n[Chorus 1]\n[Outro]\n",
        })
        self.assertTrue(any("no singable lines" in problem for problem in problems))

    def test_all_instrumental_structure_is_not_rejected(self):
        _, _, problems = build_draft_settings({
            "musical_direction": "Dark ambient instrumental with no lead vocal.",
            "lyrics": "[inst]\n[Outro]\n",
            "arrangement": {"inst": "Instrumental-only drone; no vocals.",
                            "Outro": "No vocals; fade out."},
        }, known_sections=("inst", "outro"))
        self.assertEqual(problems, [])

    def test_a_short_duration_is_raised_to_fit_the_lyrics(self):
        lines = "\n".join(f"Line number {index} of the verse," for index in range(1, 21))
        candidate, filled, problems = build_draft_settings({
            "musical_direction": "Driving country rock with fiddle.",
            "lyrics": f"[Verse 1]\n{lines}\n",
            "duration_s": 40,
        }, known_sections=("verse",))
        self.assertEqual(problems, [])
        self.assertGreaterEqual(candidate["duration_s"], 60)
        self.assertTrue(any("raised" in note for note in filled))

    def test_an_operator_stated_duration_is_never_raised(self):
        lines = "\n".join(f"Line number {index} of the verse," for index in range(1, 21))
        candidate, filled, _ = build_draft_settings({
            "musical_direction": "Driving country rock with fiddle.",
            "lyrics": f"[Verse 1]\n{lines}\n",
            "duration_s": 40,
        }, source="Total duration 0:40.", known_sections=("verse",))
        self.assertEqual(candidate["duration_s"], 40.0)
        self.assertFalse(any("raised" in note for note in filled))

    def test_shared_section_names_with_single_occurrence_wording_are_flagged(self):
        _, filled, problems = build_draft_settings({
            "musical_direction": "Driving country rock with fiddle.",
            "lyrics": ("[Chorus]\nLine one of the chorus here,\nLine two of the chorus here.\n"
                       "[Verse]\nLine one of the verse here,\nLine two of the verse here.\n"
                       "[Chorus]\nLine one of the chorus here,\nLine two of the chorus here.\n"),
            "arrangement": {"Chorus": "Final chorus at maximum energy.",
                            "Verse": "Restrained train beat."},
        }, known_sections=("verse", "chorus"))
        self.assertEqual(problems, [])
        self.assertTrue(any("applies to all" in note for note in filled))

    def test_user_prompt_asks_for_all_three_parts_and_forbids_named_artists(self):
        prompt = build_user_prompt("SONG SOURCE")
        self.assertIn("research", prompt.lower())
        self.assertIn("mixing and vocal processing chain", prompt.lower())
        self.assertIn("never name an artist", prompt)
        self.assertIn("verbatim", prompt)
        # The source travels as separate untrusted data, so it must not appear
        # in the instruction block that carries the rules.
        self.assertNotIn("SONG SOURCE", prompt)
        self.assertIn("untrusted reference data", prompt)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        base = settings_module.get_settings()
        self._cached = settings_module._cached
        settings_module._cached = dataclasses.replace(
            base, projects_root=self.root,
            music=dataclasses.replace(base.music, live_generation_authorized=False))
        ps._store = None
        orch_module._orch = None
        music_module._service = None
        reset_router()
        self.service = MusicService(comfy=FakeComfy(), router=FakeRouter())
        self.pid = self.service.store.create(
            ProjectCreate(name="prompt-draft-test", brief="")).id

    def tearDown(self) -> None:
        settings_module._cached = self._cached
        ps._store = None
        orch_module._orch = None
        music_module._service = None
        reset_router()
        self._tmp.cleanup()

    def _drafts(self, lms: FakeLMS) -> MusicDraftService:
        return MusicDraftService(service=self.service, lms=lms, router=FakeRouter(),
                                 store=self.service.store)

    def _state(self) -> MusicState:
        path = self.root / self.pid / "music/.state/state.json"
        if not path.is_file():
            return MusicState()
        return MusicState.model_validate_json(path.read_text(encoding="utf-8"))

    def test_prompt_creates_an_unapproved_draft_with_evidence(self):
        lms = FakeLMS([good_response()])
        result = asyncio.run(self._drafts(lms).generate(
            self.pid, "A warm electronic pop song"))

        state = self._state()
        self.assertIsNotNone(state.draft)
        self.assertEqual(state.draft.version, 1)
        self.assertIsNone(state.approval)
        self.assertIsNone(state.lifetime_cap)
        self.assertIsNone(state.audio_approval)
        self.assertEqual(state.revisions[0].source, "prompt_draft")
        self.assertFalse((self.root / self.pid / "music/.state/attempts.jsonl").exists())
        self.assertIsNone(result["approval"])
        self.assertIsNone(result["lifetime_cap"])

        provenance = self.root / self.pid / "music/.state/draft_provenance/v1.json"
        self.assertTrue(provenance.is_file())
        record = json.loads(provenance.read_text(encoding="utf-8"))
        self.assertEqual(record["lyrics_sha256"], state.draft.lyrics_sha256)
        self.assertEqual(record["settings_sha256"], state.draft.settings_sha256)
        self.assertEqual(record["model"], "test-assistant")

        brief = self.root / self.pid / "copy" / result["artifact"]["filename"]
        self.assertTrue(brief.is_file())
        self.assertTrue(result["artifact"]["filename"].endswith("_production_brief.md"))
        text = brief.read_text(encoding="utf-8")
        self.assertIn("Brian Setzer", text)
        self.assertIn("limiter at -1.0 dBFS", text)
        self.assertIn("cannot execute insert plugins", text)

        meta = self.service.store.get(self.pid)
        self.assertIn("Prompt draft", meta.notes)
        self.assertEqual(state.draft.settings.bpm, 110)

    def test_a_rejected_answer_is_repaired_once(self):
        lms = FakeLMS([json.dumps({"title": "x", "musical_direction": "warm", "lyrics": ""}),
                       good_response()])
        result = asyncio.run(self._drafts(lms).generate(
            self.pid, "A warm electronic pop song"))
        self.assertEqual(len(lms.calls), 2)
        self.assertIn("rejected", lms.calls[1]["prompt"])
        self.assertEqual(result["draft"]["version"], 1)

    def test_two_bad_answers_are_refused_and_nothing_is_saved(self):
        bad = json.dumps({"title": "x", "musical_direction": "warm", "lyrics": ""})
        lms = FakeLMS([bad, bad])
        with self.assertRaises(MusicDraftGenerationError):
            asyncio.run(self._drafts(lms).generate(self.pid, "A warm electronic pop song"))
        state = self._state()
        self.assertIsNone(state.draft)
        self.assertIsNone(state.approval)
        self.assertFalse((self.root / self.pid / "music/.state/draft_provenance").exists())

    def test_each_prompt_draft_keeps_its_own_production_brief(self):
        # Different settings force a second revision, so each run must own a file.
        lms = FakeLMS([good_response(), good_response(bpm=112)])
        drafts = self._drafts(lms)
        first = asyncio.run(drafts.generate(self.pid, "A warm electronic pop song"))
        second = asyncio.run(drafts.generate(self.pid, "A warm electronic pop song"))
        self.assertNotEqual(first["artifact"]["relative_path"],
                            second["artifact"]["relative_path"])
        self.assertIn("_v1_", first["artifact"]["filename"])
        self.assertIn("_v2_", second["artifact"]["filename"])
        for path in (first["artifact"]["relative_path"],
                     second["artifact"]["relative_path"]):
            self.assertTrue((self.root / self.pid / path).is_file())

    def test_generation_never_runs_from_the_prompt_path(self):
        fake = FakeComfy()
        self.service.comfy = fake
        lms = FakeLMS([good_response()])
        asyncio.run(self._drafts(lms).generate(self.pid, "A warm electronic pop song"))
        self.assertEqual(fake.submissions, [])
        self.assertFalse((self.root / self.pid / "music/.state/attempts.jsonl").exists())

    def test_a_language_name_is_mapped_to_the_code_the_engine_accepts(self):
        """'english' is not a code ACE-Step knows; 'en' is."""
        from app.services.music_draft_service import coerce_language

        self.assertEqual(coerce_language("english"), "en")
        self.assertEqual(coerce_language("English"), "en")
        self.assertEqual(coerce_language("en"), "en")
        self.assertEqual(coerce_language("french"), "fr")
        self.assertIsNone(coerce_language("klingon"))
        self.assertIsNone(coerce_language(None))

    def test_operator_lyrics_are_kept_verbatim(self):
        original = "Twelve-fifteen on the monitor screen,\nBlue-white glow on a tired routine."
        lms = FakeLMS([good_response(lyrics=f"[Verse 1]\n{original}")])
        asyncio.run(self._drafts(lms).generate(self.pid, "Song about a night shift"))
        state = self._state()
        self.assertIn(original, state.draft.settings.lyrics)


if __name__ == "__main__":
    unittest.main()
