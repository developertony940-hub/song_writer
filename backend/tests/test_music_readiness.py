"""Stdlib-only tests of actual pure readiness/duration code; no live acceptance."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from app.services.music_input_review import estimate_lyric_duration, review_music_inputs
from app.services.music_readiness import (
    build_music_readiness, device_mapping, observe_endpoint,
    read_persisted_snapshot, summarize_queue,
)


class DurationTests(unittest.TestCase):
    def estimate(self, **updates):
        args = dict(lyrics="[Verse 1]\nMy unforgettable melody\n", arrangement={}, bpm=80)
        args.update(updates)
        return estimate_lyric_duration(**args)

    def test_counts_and_explicit_tempo_range_are_transparent(self):
        value = self.estimate()
        self.assertEqual(value["words"], 3)
        self.assertGreater(value["syllables_estimate"], value["words"])
        self.assertEqual(value["tempo_bpm_range"], [80, 80])
        self.assertFalse(value["legacy_seconds_per_line_used"])
        self.assertLess(value["total_seconds_range"][0], value["total_seconds_range"][1])

    def test_slow_tempo_and_syllable_density_expand_estimate(self):
        self.assertGreater(self.estimate(bpm=60)["total_seconds_range"][1],
                           self.estimate(bpm=120)["total_seconds_range"][1])
        self.assertGreater(self.estimate(lyrics="Extraordinary unforgettable")['syllables_estimate'],
                           self.estimate(lyrics="My song")['syllables_estimate'])

    def test_explicit_instrumental_allowance_does_not_assume_intro_is_instrumental(self):
        value = self.estimate(lyrics="[Intro]\n[Verse 1]\nMy song\n[Instrumental]\n[Outro]\n",
                              arrangement={"Outro": "no vocals; guitar"})
        self.assertEqual(value["instrumental_sections"], 2)
        self.assertEqual(value["instrumental_seconds_range"], [12, 24])

    def test_unknown_tempo_has_range_and_direction_bpm_is_not_duration(self):
        self.assertEqual(self.estimate(bpm=None)["tempo_bpm_range"], [60, 140])
        value = self.estimate(bpm=None, musical_direction="Warm country, 82 BPM")
        self.assertEqual(value["tempo_source"], "direction")
        self.assertEqual(value["tempo_bpm_range"], [82, 82])
        for invalid in (0, -1, float("nan"), float("inf")):
            self.assertEqual(self.estimate(bpm=invalid)["tempo_source"], "unspecified_range")

    def test_legacy_parameter_cannot_reintroduce_line_count_estimate(self):
        args = dict(lyrics="[Verse 1]\nMy song\n", musical_direction="Warm country", duration_s=30,
                    arrangement={}, known_sections=("verse",))
        self.assertEqual(review_music_inputs(**args, seconds_per_line=1),
                         review_music_inputs(**args, seconds_per_line=1000))

    def test_estimate_never_mutates_inputs_or_omits_words_in_instrumental_tag(self):
        args = dict(lyrics="[Instrumental]\nActual words\n", arrangement={"Instrumental": "guitar"}, bpm=80)
        original = copy.deepcopy(args)
        value = estimate_lyric_duration(**args)
        self.assertEqual(args, original)
        self.assertEqual(value["words"], 2)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.args = dict(
            project_id="0123456789ab", state={
                "draft": {"version": 1, "lyrics_sha256": "recorded-lyrics", "settings_sha256": "recorded-settings",
                          "settings": {"lyrics": "[Verse 1]\nMy lovely song\n", "musical_direction": "Warm country",
                                       "duration_s": 30, "bpm": 80}},
                "approval": {"submission_cap": 2}, "lifetime_cap": 2,
            }, journal={"submissions": 1, "rows": [], "orphans": [], "open_uncertain": []},
            hashes_match=True,
            policy={"model": "ace-step-1.5", "slot": 1, "configured_device_index": 1,
                    "capacity_verified": True, "capacity_override_authorized": True,
                    "live_generation_authorized": True},
            router={"slots": {"1": {"current_model": None, "locked_by": None}}, "holds": {}},
            endpoint="http://example.invalid:8188",
            system_stats={"devices": [{"type": "cuda", "index": 1, "vram_free": 10**12}]},
            queue={"queue_running": [], "queue_pending": []}, known_sections=("verse",),
        )

    def codes(self, value):
        return {b["code"] for b in value["blockers"]}

    def test_free_memory_and_config_flags_do_not_verify_mapping_ownership_or_exclusivity(self):
        result = build_music_readiness(**self.args)
        self.assertFalse(result["ready"])
        self.assertFalse(result["can_generate"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["runtime"]["device_mapping"]["status"], "unknown")
        self.assertEqual(result["runtime"]["ownership"]["status"], "unknown")
        self.assertEqual(result["runtime"]["exclusive_capacity"]["status"], "unknown")
        self.assertFalse(result["actions"]["generation"]["enabled"])
        self.assertEqual(result["actions"]["generation"]["path"], "/api/music/0123456789ab/generate")
        self.assertFalse(result["actions"]["dispatch_added"])

    def test_actual_endpoint_device_mismatch_fails_closed(self):
        self.args["system_stats"]["devices"][0]["index"] = 0
        result = build_music_readiness(**self.args)
        self.assertIn("device_mapping_mismatch", self.codes(result))
        self.assertEqual(result["status"], "blocked")

    def test_configured_device_differs_from_policy_even_if_endpoint_matches_config(self):
        result = device_mapping({"devices": [{"type": "cuda", "index": 0}]}, configured_index=0, policy_slot=1)
        self.assertEqual(result["status"], "mismatch")

    def test_cpu_endpoint_does_not_satisfy_cuda_policy(self):
        self.assertEqual(device_mapping({"devices": [{"type": "cpu", "index": 1}]},
                                       configured_index=1, policy_slot=1)["status"], "mismatch")

    def test_missing_stats_and_malformed_queue_stay_unknown_not_idle(self):
        self.args.update(system_stats=None, queue={})
        result = build_music_readiness(**self.args)
        self.assertIn("runtime_unavailable", self.codes(result))
        self.assertIn("queue_unknown", self.codes(result))
        self.assertIsNone(result["runtime"]["queue"]["running"])
        self.assertFalse(summarize_queue({"queue_running": [], "queue_pending": "bad"})["known"])

    def test_busy_queue_counts_without_leaking_prompt_data(self):
        self.args["queue"]["queue_running"] = [[1, "prompt", {"secret": "lyric"}]]
        result = build_music_readiness(**self.args)
        self.assertIn("queue_busy", self.codes(result))
        self.assertEqual(result["runtime"]["queue"], {"known": True, "running": 1, "pending": 0})

    def test_approvals_caps_recovery_and_lease_blockers_are_independent(self):
        self.args["hashes_match"] = False
        self.args["policy"]["live_generation_authorized"] = False
        self.args["journal"].update(submissions=2, orphans=["attempt"])
        self.args["router"]["slots"]["1"] = {"locked_by": "other", "current_model": "flux"}
        self.args["router"]["holds"] = {"1": {"reason": "uncertain"}}
        self.args["state"]["current_attempt"] = {"status": "running"}
        result = build_music_readiness(**self.args)
        self.assertTrue({"approval_required", "live_disabled", "cap_exhausted", "recovery_required",
                         "slot_busy", "model_switch_required", "capacity_held", "attempt_running"} <= self.codes(result))
        self.assertEqual(result["submission"]["remaining"], 0)

    def test_unreadable_holds_block_and_cap_never_increases(self):
        self.args["router"]["holds_unreadable"] = True
        self.args["state"]["approval"]["submission_cap"] = 4
        result = build_music_readiness(**self.args)
        self.assertIn("capacity_held", self.codes(result))
        self.assertEqual(result["submission"]["effective_cap"], 2)

    def test_invalid_recorded_cap_is_not_discarded_to_increase_capacity(self):
        self.args["state"]["lifetime_cap"] = 0
        result = build_music_readiness(**self.args)
        self.assertIn("cap_invalid", self.codes(result))
        self.assertEqual(result["submission"]["effective_cap"], 0)
        self.assertEqual(result["submission"]["remaining"], 0)

    def test_existing_capture_references_are_not_invented_or_verified(self):
        self.args["journal"]["rows"] = [{"kind": "attempt_started", "attempt_id": "a",
                                          "api_request_path": "music/request.json", "environment_sha256": "abc",
                                          "effective_settings": {"secret": "no echo"}}]
        original = copy.deepcopy(self.args)
        result = build_music_readiness(**self.args)
        self.assertEqual(self.args, original)
        self.assertEqual(result["captures"], [{"attempt_id": "a", "kind": "attempt_started", "artifacts": {
            "api_request_path": "music/request.json", "environment_sha256": "abc"}}])
        self.assertEqual(result["approvals"]["record"], {"submission_cap": 2})

    def test_no_draft_or_cap_does_not_make_up_approval(self):
        self.args.update(state={}, hashes_match=False)
        result = build_music_readiness(**self.args)
        self.assertTrue({"draft_missing", "approval_required", "cap_missing"} <= self.codes(result))
        self.assertIsNone(result["input_review"]["duration"])


class FilesystemReadTests(unittest.TestCase):
    def test_new_project_read_creates_no_music_or_state_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "0123456789ab"
            project.mkdir()
            (project / "manifest.json").write_text("{}")
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            self.assertEqual(read_persisted_snapshot(root, project.name), ({}, []))
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob("*")))
            with self.assertRaises(KeyError):
                read_persisted_snapshot(root, "../escape")

    def test_corrupt_existing_state_does_not_become_empty_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "0123456789ab"
            directory = project / "music" / ".state"
            directory.mkdir(parents=True)
            (project / "manifest.json").write_text("{}")
            (directory / "state.json").write_text("not json")
            with self.assertRaises(json.JSONDecodeError):
                read_persisted_snapshot(root, project.name)


class EndpointObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_is_only_get_system_stats_and_queue(self):
        calls = []

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"devices": []}

        class Client:
            async def get(self, path):
                calls.append(path)
                return Response()

        stats, queue, errors = await observe_endpoint(Client())
        self.assertEqual(calls, ["/system_stats", "/queue"])
        self.assertEqual(errors, [])
        self.assertEqual(stats, {"devices": []})

    async def test_observation_failure_does_not_retry_or_mutate(self):
        calls = []

        class Client:
            async def get(self, path):
                calls.append(path)
                raise TimeoutError("private connection details")

        stats, queue, errors = await observe_endpoint(Client())
        self.assertIsNone(stats)
        self.assertIsNone(queue)
        self.assertEqual(len(errors), 2)
        self.assertNotIn("private", str(errors))
        self.assertEqual(calls, ["/system_stats", "/queue"])


if __name__ == "__main__":
    unittest.main()
