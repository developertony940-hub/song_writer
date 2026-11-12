"""Tests for the ledger, the operator-lyric recovery and the structure critic.

Everything here runs offline with a scripted assistant: no provider is
contacted, no GPU is touched, and the tests never touch the live projects.
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
from app.services import project_store as ps
from app.services.agent_probe import probe_model_capability
from app.services.lmstudio_client import ChatResult, ChatUsage
from app.services.model_router import reset_router
from app.services.music_draft_service import MusicDraftService, build_draft_settings
from app.services.music_input_review import review_music_inputs
from app.services.music_service import MusicService
from app.services.operator_lyrics import (
    extract_operator_lyrics, merge_operator_lyrics, operator_lyrics_survived,
)

from app.agents.records import (
    append_run_record, read_run_records, summarise_run_records,
)

KNOWN = ("intro", "verse", "pre-chorus", "chorus", "post-chorus", "bridge",
         "outro", "inst", "instrumental", "hook", "breakdown", "interlude")


class FakeRouter:
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
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict]] = []

    async def status(self) -> dict:
        return {"online": True, "base_url": "fake", "gpus": []}

    async def submit_music(self, workflow: str, patches: dict) -> str:
        self.submissions.append((workflow, patches))
        raise AssertionError("the prompt path must never submit a generation")


class ScriptedLMS:
    """Replays scripted answers and reports the usage it was told to report."""

    default_model = "test-assistant"

    def __init__(self, answers: list[str], *, usage: tuple[int, int] = (120, 340),
                 elapsed_ms: int = 42) -> None:
        self.answers = list(answers)
        self.usage = usage
        self.elapsed_ms = elapsed_ms
        self.calls: list[dict] = []

    async def chat_result(self, prompt: str, **kwargs) -> ChatResult:
        self.calls.append({"prompt": prompt, **kwargs})
        text = self.answers.pop(0) if self.answers else ""
        prompt_tokens, completion_tokens = self.usage
        return ChatResult(
            text=text,
            usage=ChatUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            total_tokens=prompt_tokens + completion_tokens),
            model=str(kwargs.get("model") or self.default_model),
            elapsed_ms=self.elapsed_ms,
            finish_reason="stop",
        )

    async def chat_text(self, prompt: str, **kwargs) -> str:
        return (await self.chat_result(prompt, **kwargs)).text


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_run_record_is_appended_and_readable(self):
        append_run_record(self.state_dir, {
            "project_id": "abc", "model": "m", "tokens_in": 100, "tokens_out": 50,
            "outcome": "saved", "defects": ["empty_section"],
        })
        append_run_record(self.state_dir, {
            "project_id": "abc", "model": "m", "tokens_in": 10, "tokens_out": 5,
            "outcome": "failed", "defects": [],
        })
        rows = read_run_records(self.state_dir)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "agent_run")
        self.assertEqual(rows[0]["recorded_at"][:4], "2026")
        summary = summarise_run_records(rows)
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["tokens_in"], 110)
        self.assertEqual(summary["total_tokens"], 165)
        self.assertEqual(summary["outcomes"], {"saved": 1, "failed": 1})
        self.assertEqual(summary["defects"], {"empty_section": 1})

    def test_the_ledger_never_touches_the_attempt_journal(self):
        append_run_record(self.state_dir, {"project_id": "abc", "outcome": "saved"})
        self.assertFalse((self.state_dir / "attempts.jsonl").exists())
        self.assertTrue((self.state_dir / "agent_runs.jsonl").exists())

    def test_a_malformed_ledger_line_is_reported_not_repaired(self):
        append_run_record(self.state_dir, {"project_id": "abc", "outcome": "saved"})
        with (self.state_dir / "agent_runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        rows = read_run_records(self.state_dir)
        self.assertEqual(rows[-1]["kind"], "ledger_error")


class OperatorLyricTests(unittest.TestCase):
    def test_lyrics_are_recovered_from_a_structured_brief(self):
        source = """1. Key, Tempo, and Groove
Key: E Minor (The quintessential key for blues and southern rock).

Bassline: A heavy, analog Moog synthesizer bassline that pulses in tight eighth notes.

[Verse 1] (0:30 - 0:58)
Musical Vibe: The beat drops in instantly.

Twelve-fifteen on the monitor screen,
Blue-white glow on a tired routine.

[Pre-Chorus]
Footsteps echo on the floor above,
Moving faster than a mortal love.
"""
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertEqual(result.confidence, "clean")
        self.assertEqual(result.tags, ["Verse 1", "Pre-Chorus"])
        self.assertIn("Twelve-fifteen on the monitor screen,", result.lines)
        self.assertIn("Moving faster than a mortal love.", result.lines)
        self.assertNotIn("A heavy, analog Moog synthesizer bassline that pulses in tight eighth notes.",
                         result.lines)

    def test_a_section_heading_may_carry_an_annotation(self):
        """'[Verse 1 - Johnny's Party]' is the verse; the annotation is not sung."""
        source = ("[VERSE 1 - JOHNNY'S HALLOWEEN PARTY]\n"
                  "Johnny's got his boots and his old straw hat\n"
                  "A mason jar shine and a black barn cat\n"
                  "[CHORUS - Full band, big singalong vocals]\n"
                  "Pour another round, let that moonshine roll\n")
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertEqual(result.tags, ["Verse 1", "Chorus"])
        self.assertIn("Johnny's got his boots and his old straw hat", result.lines)
        self.assertNotIn("JOHNNY'S HALLOWEEN PARTY", result.lyrics)

    def test_lyric_lines_need_no_punctuation(self):
        """A real lyric sheet rarely punctuates its lines."""
        source = ("[Verse 1]\n"
                  "The screen door swings where the cypress trees sway\n"
                  "Jack-o'-lanterns light the old dirt way\n")
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertEqual(len(result.lines), 2)

    def test_playing_instructions_are_not_lyrics(self):
        source = ("[Verse 1]\n"
                  "A line of the song,\n"
                  "Fiddle rises. Drums move from light shuffle to stronger snare.\n"
                  "Return to lighter shuffle. Piano adds honky-tonk accents.\n"
                  "First 8 bars: electric guitar solo with a country-rock feel.\n"
                  "Final G chord. Let ring.\n"
                  "- A bullet item is not a sung line.\n"
                  "End of song\n")
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertEqual(result.lines, ["A line of the song,"])

    def test_a_chord_prefix_does_not_cost_the_operator_the_words(self):
        source = ("[Chorus]\n"
                  "[G] Pour another round, let that moonshine roll\n"
                  "[C] Raise your glass up high, let the good times flow\n")
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertIn("Pour another round, let that moonshine roll", result.lines)
        self.assertNotIn("[G]", result.lyrics)

    def test_only_one_copy_of_a_repeated_song_is_claimed(self):
        """A paste with two drafts must not become a two-times-too-long song."""
        song = ("[Verse 1]\nFirst version line one,\nFirst version line two,\n"
                "[Chorus]\nFirst chorus line one,\nFirst chorus line two,\n"
                "[Bridge]\nFirst bridge line one,\nFirst bridge line two,\n")
        source = song + "Some notes about the arrangement.\n" + song
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertEqual(result.lines.count("First version line one,"), 1)
        self.assertTrue(any("copies of sectioned material" in note
                            for note in result.notes))

    def test_an_instrumental_section_carries_no_sung_words(self):
        source = ("[Verse 1]\nA line of the song,\n"
                  "[INSTRUMENTAL - 20-25 SECONDS]\n"
                  "Fiddle solo into banjo and electric guitar.\n")
        result = extract_operator_lyrics(source)
        self.assertTrue(result.found)
        self.assertNotIn("Fiddle solo into banjo and electric guitar.", result.lines)

    def test_a_model_that_numbers_its_sections_does_not_double_the_song(self):
        """'Chorus 1' and 'Chorus' are the same section, not two sections."""
        source = ("[Verse 1]\nOperator verse one,\n"
                  "[Chorus]\nOperator chorus one,\n"
                  "[Verse 2]\nOperator verse two,\n"
                  "[Chorus]\nOperator chorus one,\n")
        extracted = extract_operator_lyrics(source)
        model = ("[Verse 1]\nModel verse one,\n"
                 "[Chorus 1]\nModel chorus one,\n"
                 "[Verse 2]\nModel verse two,\n"
                 "[Chorus 2]\nModel chorus two,\n")
        merged, replaced, added = merge_operator_lyrics(extracted, model)
        self.assertEqual(merged.count("Operator chorus one,"), 2)
        self.assertNotIn("Model chorus one,", merged)
        self.assertNotIn("Model chorus two,", merged)
        self.assertEqual(len(replaced), 4)
        self.assertEqual(added, [])

    def test_the_operator_repeats_keep_their_place_in_the_song(self):
        """A chorus that comes back after a verse must come back after it."""
        source = ("[Chorus]\nHook line one,\n"
                  "[Verse 2]\nVerse two line one,\n"
                  "[Chorus]\nHook line one,\n")
        extracted = extract_operator_lyrics(source)
        model = ("[Chorus]\nModel hook,\n[Verse 2]\nModel verse,\n[Chorus]\nModel hook,\n")
        merged, _, _ = merge_operator_lyrics(extracted, model)
        tags = [block.splitlines()[0] for block in merged.split("\n\n") if block.strip()]
        self.assertEqual(tags, ["[Chorus]", "[Verse 2]", "[Chorus]"])
        self.assertNotIn("Model hook", merged)

    def test_a_model_section_the_operator_never_wrote_is_kept(self):
        source = "[Verse 1]\nOperator line one,\n"
        extracted = extract_operator_lyrics(source)
        model = "[Verse 1]\nModel rewrote this,\n[Bridge]\nModel bridge line.\n"
        merged, replaced, added = merge_operator_lyrics(extracted, model)
        self.assertIn("Operator line one,", merged)
        self.assertNotIn("Model rewrote this,", merged)
        self.assertIn("Model bridge line.", merged)
        self.assertEqual(replaced, ["Verse 1"])
        self.assertEqual(added, [])

    def test_an_annotated_arrangement_key_still_finds_its_section(self):
        """'[CHORUS - 8 BARS]' is the chorus, so its directive is kept."""
        from app.services.music_draft_service import normalise_arrangement

        kept, notes = normalise_arrangement(
            {"[CHORUS - 8 BARS]": "Full band, gang vocals.",
             "[VERSE 1 - 16 BARS]": "Shuffle groove."},
            ["Chorus", "Verse 1"], known_sections=("chorus", "verse"))
        self.assertEqual(set(kept), {"Chorus", "Verse 1"})
        self.assertEqual(kept["Chorus"], "Full band, gang vocals.")
        self.assertEqual(notes, [])

    def test_a_long_arrangement_is_shortened_before_a_section_is_dropped(self):
        """A trimmed directive still describes the section; a missing one does not."""
        from app.services.music_draft_service import ensure_arrangement_covers_tags

        tags = ["Intro", "Verse 1", "Chorus"]
        arrangement = {
            "Intro": "Sparse acoustic guitar and distant fiddle. " * 14,
            "Verse 1": "Full shuffle groove, banjo between the vocal lines. " * 14,
            "Chorus": "Full band, gang vocals, crashes, electric power chords. " * 14,
        }
        kept, notes = ensure_arrangement_covers_tags(
            arrangement, tags, ("intro", "verse", "chorus"))
        self.assertEqual(set(kept), set(tags))
        total = sum(len(key) + len(value) for key, value in kept.items())
        self.assertLessEqual(total, 2000)
        self.assertTrue(any("shortened" in note for note in notes))

    def test_a_run_together_brief_is_refused_rather_than_guessed(self):
        source = ("[Verse 1] The moon is full, the graveyard gates are wide,The skeletons "
                  "are crawling up outside.Old man Jenkins threw his shroud away,")
        result = extract_operator_lyrics(source)
        self.assertFalse(result.found)
        self.assertEqual(result.confidence, "none")
        self.assertTrue(result.notes)

    def test_a_brief_without_lyrics_reports_no_lyrics(self):
        source = ("Song Research: an electro-swing novelty track.\n\n"
                  "Chord progressions: Gm -> Cm -> D7 -> Gm.\n\n"
                  "Mixing chain: high-pass at 90 Hz, 1176 then LA-2A, limiter at -1.0 dBFS.\n")
        result = extract_operator_lyrics(source)
        self.assertFalse(result.found)

    def test_operator_sections_replace_the_model_sections(self):
        source = ("[Verse 1]\nOperator line one,\nOperator line two.\n"
                  "[Chorus]\nOperator chorus one,\nOperator chorus two.\n")
        extracted = extract_operator_lyrics(source)
        model = ("[Verse 1]\nModel rewrote this,\n[Bridge]\nModel bridge line.\n")
        merged, replaced, added = merge_operator_lyrics(extracted, model)
        self.assertIn("Operator line one,", merged)
        self.assertNotIn("Model rewrote this,", merged)
        self.assertIn("Model bridge line.", merged)
        # "Verse 1" existed in both, so it was replaced; "Chorus" existed only in
        # the operator's source, so it was added back.
        self.assertEqual(replaced, ["Verse 1"])
        self.assertEqual(added, ["Chorus"])
        self.assertEqual(operator_lyrics_survived(extracted, merged), [])

    def test_a_model_cannot_drop_the_operator_words(self):
        source = "[Verse 1]\nKeep this exact line,\nAnd this one too.\n"
        extracted = extract_operator_lyrics(source)
        hostile = "[Verse 1]\nTotally different words,\n"
        merged, _, _ = merge_operator_lyrics(extracted, hostile)
        self.assertEqual(operator_lyrics_survived(extracted, merged), [])

    def test_the_draft_builder_injects_operator_lyrics_over_the_model(self):
        source = "[Verse 1]\nOperator wins this line,\nOperator wins this too.\n"
        candidate, filled, problems = build_draft_settings({
            "musical_direction": "Warm southern gothic with fiddle.",
            "lyrics": "[Verse 1]\nModel replaced it,\n[Chorus]\nModel chorus here.\n",
            "arrangement": {"Verse 1": "Sparse guitar.", "Chorus": "Full band."},
        }, source=source, known_sections=KNOWN)
        self.assertEqual(problems, [])
        self.assertIn("Operator wins this line,", candidate["lyrics"])
        self.assertNotIn("Model replaced it,", candidate["lyrics"])
        self.assertTrue(any("operator lyrics were recovered" in note for note in filled))


class StructureCriticTests(unittest.TestCase):
    def review(self, lyrics: str, arrangement: dict) -> list[dict]:
        return review_music_inputs(
            lyrics=lyrics, musical_direction="Southern gothic with fiddle.",
            duration_s=120, arrangement=arrangement, known_sections=KNOWN, bpm=120)

    def test_a_shared_name_with_occurrence_wording_is_flagged(self):
        warnings = self.review(
            "[Chorus]\nLine one here,\nLine two here.\n[Chorus]\nLine one here,\nLine two here.\n",
            {"Chorus": "Final chorus at maximum energy."})
        self.assertIn("shared_occurrence_wording", {w["code"] for w in warnings})

    def test_an_instrumental_claim_with_words_is_flagged(self):
        warnings = self.review(
            "[Bridge]\nWe ride through the night,\nThe fiddle sings.\n",
            {"Bridge": "Fast turnaround. Instrumental solo for the fiddle."})
        self.assertIn("instrumental_lyric_conflict", {w["code"] for w in warnings})

    def test_a_true_instrumental_section_is_not_flagged(self):
        warnings = self.review(
            "[inst]\n[Chorus]\nLine one here,\nLine two here.\n",
            {"inst": "Instrumental-only passage; no vocals.",
             "Chorus": "Full band lift."})
        codes = {w["code"] for w in warnings}
        self.assertNotIn("instrumental_lyric_conflict", codes)

    def test_tags_without_words_are_flagged(self):
        warnings = self.review("[Intro]\n[Verse 1]\n[Chorus]\n", {})
        self.assertIn("no_sung_lines", {w["code"] for w in warnings})

    def test_existing_detectors_still_fire(self):
        warnings = self.review("[mystery]\nOne line here,\n", {"verse": "quiet"})
        codes = {w["code"] for w in warnings}
        self.assertIn("unknown_section", codes)
        self.assertIn("arrangement_orphan", codes)


class CapabilityProbeTests(unittest.TestCase):
    def test_a_model_that_returns_nothing_is_not_capable(self):
        class Empty:
            async def chat_result(self, prompt: str, **kwargs):
                return ChatResult(text="", usage=ChatUsage(), model="m", elapsed_ms=1,
                                  finish_reason="length")

        verdict = asyncio.run(probe_model_capability(Empty(), "m"))
        self.assertFalse(verdict.capable)
        self.assertEqual(verdict.reason, "empty_completion")

    def test_a_model_that_answers_json_is_capable(self):
        class Good:
            async def chat_result(self, prompt: str, **kwargs):
                return ChatResult(text='{"lyrics": "[Verse]\\nA line", "musical_direction": "x"}',
                                  usage=ChatUsage(1, 2, 3), model="m", elapsed_ms=1,
                                  finish_reason="stop")

        verdict = asyncio.run(probe_model_capability(Good(), "m"))
        self.assertTrue(verdict.capable)
        self.assertEqual(verdict.reason, "ok")

    def test_a_model_that_answers_prose_is_not_capable(self):
        class Prose:
            async def chat_result(self, prompt: str, **kwargs):
                return ChatResult(text="Sure! Here is your song.", usage=ChatUsage(),
                                  model="m", elapsed_ms=1, finish_reason="stop")

        verdict = asyncio.run(probe_model_capability(Prose(), "m"))
        self.assertFalse(verdict.capable)
        self.assertEqual(verdict.reason, "not_json")


class DraftLedgerTests(unittest.TestCase):
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
        self.pid = self.service.store.create(ProjectCreate(name="ledger", brief="")).id

    def tearDown(self) -> None:
        settings_module._cached = self._cached
        ps._store = None
        music_module._service = None
        reset_router()
        self._tmp.cleanup()

    def _answer(self) -> str:
        return json.dumps({
            "title": "Ledger Song",
            "musical_direction": "Warm southern gothic with fiddle and banjo.",
            "lyrics": "[Verse 1]\nA line of the song,\nAnother line here.\n",
            "duration_s": 60, "bpm": 120, "time_signature": "4", "key_scale": "E minor",
            "arrangement": {"Verse 1": "Sparse guitar and banjo."},
        })

    def test_a_saved_draft_writes_a_ledger_row_with_tokens(self):
        lms = ScriptedLMS([self._answer()])
        drafts = MusicDraftService(service=self.service, lms=lms, router=FakeRouter(),
                                   store=self.service.store)
        result = asyncio.run(drafts.generate(self.pid, "A southern gothic song"))
        self.assertEqual(result["usage"]["tokens_in"], 120)
        self.assertEqual(result["usage"]["tokens_out"], 340)
        self.assertEqual(result["usage"]["total_tokens"], 460)
        rows = read_run_records(self.service._state_dir(self.pid))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "saved")
        self.assertEqual(rows[0]["tokens_in"], 120)
        self.assertEqual(rows[0]["draft_version"], 1)

    def test_a_failed_draft_writes_a_ledger_row_with_the_defects(self):
        bad = json.dumps({"title": "x", "musical_direction": "warm", "lyrics": ""})
        lms = ScriptedLMS([bad, bad])
        drafts = MusicDraftService(service=self.service, lms=lms, router=FakeRouter(),
                                   store=self.service.store)
        with self.assertRaises(Exception):
            asyncio.run(drafts.generate(self.pid, "A southern gothic song"))
        rows = read_run_records(self.service._state_dir(self.pid))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "failed")
        self.assertTrue(rows[0]["defects"])

    def test_the_attempt_journal_is_not_created_by_a_draft(self):
        lms = ScriptedLMS([self._answer()])
        drafts = MusicDraftService(service=self.service, lms=lms, router=FakeRouter(),
                                   store=self.service.store)
        asyncio.run(drafts.generate(self.pid, "A southern gothic song"))
        state = MusicState.model_validate_json(
            (self.service._state_dir(self.pid) / "state.json").read_text(encoding="utf-8"))
        self.assertIsNone(state.approval)
        self.assertIsNone(state.lifetime_cap)
        self.assertFalse((self.service._state_dir(self.pid) / "attempts.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
