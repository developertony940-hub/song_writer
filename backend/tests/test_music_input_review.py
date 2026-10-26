"""Dependency-free checks; these do not replace the full backend regression suite."""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from app.services.music_input_review import parse_section_tags, review_music_inputs, section_family


KNOWN = ("intro", "verse", "chorus", "bridge", "outro", "inst", "instrumental", "interlude")


def review(**updates):
    args = dict(lyrics="[verse]\nOne line\nTwo lines\n", musical_direction="Warm country song",
                duration_s=12, arrangement={}, known_sections=KNOWN, seconds_per_line=6)
    args.update(updates)
    return review_music_inputs(**args)


class InputReviewTests(unittest.TestCase):
    def test_distinct_labels_are_known_without_canonicalizing_identity(self):
        lyrics = "[Verse 1]\nOne\n[Verse 2]\nTwo\n[Chorus 2]\nThree\n[Final Chorus]\nFour\n"
        names = [name for name, _, _ in parse_section_tags(lyrics)]
        self.assertEqual(names, ["Verse 1", "Verse 2", "Chorus 2", "Final Chorus"])
        self.assertEqual(review(lyrics=lyrics, duration_s=24,
                                arrangement={name: "Distinct " + name for name in names}), [])

    def test_crlf_spans_preserve_original_offsets(self):
        lyrics = "[Verse 1]\r\nOne\r\n\r\n[Chorus 2]\r\nTwo\r\n"
        tags = parse_section_tags(lyrics)
        self.assertEqual([name for name, _, _ in tags], ["Verse 1", "Chorus 2"])
        for name, start, end in tags:
            self.assertEqual(lyrics[start:end], f"[{name}]\r")

    def test_empty_instrumental_and_explicit_intro_outro_are_not_missing_lyrics(self):
        lyrics = "[Intro]\n[verse]\nOne\nTwo\n[Instrumental]\n[Outro]\n"
        self.assertEqual(review(lyrics=lyrics, arrangement={
            "Intro": "Instrumental only; fiddle", "Outro": "No vocals; steel guitar",
        }), [])

    def test_empty_verse_and_unqualified_outro_still_warn(self):
        warnings = review(lyrics="[verse]\n[outro]\n")
        self.assertEqual([w["section"] for w in warnings if w["code"] == "empty_section"],
                         ["verse", "outro"])

    def test_instrument_name_alone_does_not_exempt_empty_section(self):
        self.assertIn("empty_section", {w["code"] for w in review(
            lyrics="[outro]\n", arrangement={"outro": "steel guitar"})})

    def test_unknown_and_orphan_remain_visible(self):
        codes = {w["code"] for w in review(lyrics="[mystery]\nOne\nTwo\n",
                                         arrangement={"verse": "quiet"})}
        self.assertEqual(codes, {"unknown_section", "arrangement_orphan"})

    def test_exact_arrangement_keys_do_not_merge_distinct_sections(self):
        warnings = review(lyrics="[Chorus 2]\nOne\nTwo\n", arrangement={"chorus": "soft"})
        self.assertEqual([w["section"] for w in warnings if w["code"] == "arrangement_orphan"],
                         ["chorus"])

    def test_bare_header_warns_without_rewriting_input(self):
        lyrics = "Verse 1\nOne\nTwo\n"
        self.assertIn("unbracketed_section", {w["code"] for w in review(lyrics=lyrics)})
        self.assertEqual(lyrics, "Verse 1\nOne\nTwo\n")

    def test_stale_duration_is_detected(self):
        self.assertIn("duration_conflict", {w["code"] for w in review(
            musical_direction="Country. Duration 180 seconds, 80 BPM.", duration_s=300)})

    def test_matching_minutes_and_bpm_are_not_duration_conflicts(self):
        warnings = review(musical_direction="Country. Duration: 5 minutes, 180 BPM.", duration_s=300)
        self.assertNotIn("duration_conflict", {w["code"] for w in warnings})

    def test_density_warning_remains_for_complete_sheet(self):
        lyrics = "[Verse 1]\n" + "A sung line\n" * 64
        warnings = review(lyrics=lyrics, duration_s=300)
        self.assertEqual([w["code"] for w in warnings], ["lyric_density_high"])
        self.assertIn("64 lyric lines", warnings[0]["message"])

    def test_inputs_and_stanza_breaks_are_not_mutated(self):
        args = dict(lyrics="[Bridge]\nFirst\nSecond\n\nThird\nFourth\n",
                    arrangement={"Bridge": "restrained"}, musical_direction="Country song",
                    duration_s=24, known_sections=KNOWN, seconds_per_line=6)
        original = copy.deepcopy(args)
        self.assertEqual(review_music_inputs(**args), [])
        self.assertEqual(args, original)

    def test_custom_configured_sections_remain_supported(self):
        self.assertEqual(section_family("coda", (*KNOWN, "coda")), "coda")
        self.assertIsNone(section_family("not a chorus", KNOWN))
        self.assertIsNone(section_family("verse 0", KNOWN))

    def test_legacy_unknown_empty_orphan_codes_are_preserved(self):
        lyrics = "[wobbulator]\n[verse]\nOne\n[chorus]\n\n[outro]\nTwo\n"
        self.assertEqual({w["code"] for w in review(lyrics=lyrics, arrangement={"coda": "soft"})},
                         {"unknown_section", "empty_section", "arrangement_orphan"})


class ExtractedServiceContractTests(unittest.TestCase):
    """Execute actual AST-extracted pure service paths without importing FastAPI.

    This is deliberately labeled limited evidence: no dependency-backed app,
    filesystem state, HTTP routes, authorizations or provider execution tested.
    """
    def setUp(self):
        source = Path(__file__).parents[1] / "app/services/music_service.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MusicService")
        names = {"_normalize_lyrics", "_effective_tags"}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {"_preflight", "_patches"}]
        selected = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    *functions, *methods], type_ignores=[])
        cfg = SimpleNamespace(known_sections=KNOWN, preflight_seconds_per_line=6)
        self.ns = dict(review_music_inputs=review_music_inputs,
                       get_settings=lambda: SimpleNamespace(music=cfg))
        exec(compile(ast.fix_missing_locations(selected), str(source), "exec"), self.ns)

    def test_service_preflight_uses_shared_review(self):
        settings = SimpleNamespace(lyrics="[Verse 1]\nOne\nTwo\n", musical_direction="Country",
                                   duration_s=12, arrangement={"Verse 1": "gentle"})
        self.assertEqual(self.ns["_preflight"](None, settings), [])

    def test_existing_workflow_transport_keeps_stanzas_and_distinct_guidance(self):
        lyrics = "[Bridge]\r\nOne\r\nTwo\r\n\r\nThree\r\nFour\r\n"
        settings = SimpleNamespace(
            lyrics=lyrics, musical_direction="Country", arrangement={"Verse 1": "quiet", "Verse 2": "build"},
            seed=7, bpm=80, duration_s=300, time_signature="4", language="en", key_scale="C major",
            generate_audio_codes=True, temperature=None, top_p=None, cfg_scale=None)
        patches = self.ns["_patches"](None, "fixture", "attempt", settings)
        self.assertEqual(patches["3"]["lyrics"], lyrics.replace("\r\n", "\n"))
        self.assertIn("\n\nThree", patches["3"]["lyrics"])
        self.assertEqual(patches["3"]["tags"], "Country; Verse 1: quiet; Verse 2: build")
        self.assertEqual(patches["5"]["seconds"], 300)


if __name__ == "__main__":
    unittest.main()
