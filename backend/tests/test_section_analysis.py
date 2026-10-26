"""stdlib-only: python -m unittest tests.test_section_analysis -v.

Real synthetic PCM passes through the production streaming worker. Import
decode alone is faked, as in test_master_audio; no models/network/installs.
"""
from __future__ import annotations

import ast
import json
import math
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.services.master_audio import (
    CHANNELS, RATE, MAX_DURATION, MasterAudioError, analyze_pcm, canonical, sha_file,
)
from tests.test_master_audio import FakeDecodeService, Store, PID, settings


class SectionAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / PID
        (self.project / "music").mkdir(parents=True)
        self.original = self.project / "music/take.wav"
        self.make_pcm([(3, 5000), (2, 0), (3, 18000), (3, 2000)])
        self.service = FakeDecodeService(Store(self.root), settings())
        self.source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        self.sid = self.source["source_id"]
        self.master = self.project / "master-audio"

    def make_pcm(self, regions):
        with wave.open(str(self.original), "wb") as audio:
            audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
            for seconds, amplitude in regions:
                audio.writeframes(struct.pack("<hh", amplitude, -amplitude) * int(seconds * RATE))

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(MasterAudioError) as result:
            method(*args, **kwargs)
        self.assertEqual(code, result.exception.code)

    def analysis(self, request=None):
        return self.service.create_analysis(PID, self.sid, request if request is not None else {})

    def ranges(self):
        return [{"label": "Tentative verse", "start_s": 0, "end_s": 5, "confirmed": False},
                {"label": "Tentative chorus", "start_s": 5, "end_s": 11, "confirmed": True}]

    def save_map(self, **changes):
        request = {"expected_map_revision": 0, "analysis_id": None, "sections": self.ranges()}
        request.update(changes)
        return self.service.create_map(PID, self.sid, request)

    def test_real_pcm_transitions_contiguous_and_tentative_hints(self):
        record = self.analysis({"section_labels": ["Verse?", "Chorus?"]})
        self.assertEqual("suggestion", record["kind"])
        self.assertEqual(self.sid, record["source_sha256"])
        self.assertEqual(self.source["pcm_sha256"], record["pcm_sha256"])
        self.assertEqual(110, record["feature_windows"])
        sections = record["sections"]
        self.assertEqual(4, len(sections))  # Not the hint count.
        self.assertEqual(["Verse?", "Chorus?", "Section 3", "Section 4"], [s["label"] for s in sections])
        self.assertEqual(0, sections[0]["start_s"])
        self.assertEqual(11, sections[-1]["end_s"])
        for left, right in zip(sections, sections[1:]):
            self.assertEqual(left["end_s"], right["start_s"])
        for expected in (3, 5, 8):
            self.assertTrue(any(abs(s["start_s"] - expected) <= 0.2 for s in sections))
        self.assertTrue(all(s["confirmed"] is False for s in sections))
        self.assertTrue(all(s["confidence"] in {"low", "medium"} for s in sections))
        self.assertIn("not semantic", record["warnings"][0])

    def test_silence_uniform_short_sources_not_forced_to_hint_count(self):
        for regions in ([(1, 0)], [(4, 6000)], [(0.01, 1000)]):
            self.make_pcm(regions)
            source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
            result = self.service.create_analysis(PID, source["source_id"], {"section_labels": ["A", "B", "C"]})
            self.assertEqual(1, len(result["sections"]))
            self.assertEqual(source["duration_s"], result["sections"][0]["end_s"])

    def test_real_tone_texture_change_and_cross_source_analysis_binding(self):
        with wave.open(str(self.original), "wb") as audio:
            audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
            for frequency in (100, 800):
                period = b"".join(struct.pack("<hh", *([int(10000 * math.sin(2 * math.pi * frequency * i / RATE))] * 2))
                                  for i in range(RATE))
                audio.writeframes(period * 3)
        source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        record = self.service.create_analysis(PID, source["source_id"], {})
        self.assertEqual(2, len(record["sections"]))
        self.assertAlmostEqual(3, record["sections"][1]["start_s"], delta=0.2)
        self.assertEqual("waveform_change", record["sections"][1]["boundary_reason"])
        # A valid id from another source is never accepted as this source's evidence.
        self.make_pcm([(3, 5000), (2, 0), (3, 18000), (3, 2000)])
        self.assert_code("analysis_not_found", self.service.create_map, PID, self.sid,
                         {"expected_map_revision": 0, "analysis_id": record["analysis_id"], "sections": self.ranges()})

    def test_history_is_chronological_append_only_separate_from_recipes(self):
        first = self.analysis()
        path = self.master / f"sources/{self.sid}/analyses/000001.json"
        before = path.read_bytes()
        second = self.analysis()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([first, second], self.service.analysis_history(PID, self.sid)["analyses"])
        self.assertEqual(first["record_sha256"], second["parent_record_sha256"])
        self.assertEqual([], self.service.status(PID)["revisions"])

    def test_reviewed_maps_preserve_exact_user_booleans_and_prior_evidence(self):
        analysis = self.analysis()
        first = self.save_map(analysis_id=analysis["analysis_id"])
        self.assertEqual("reviewed", first["kind"])
        self.assertFalse(first["all_confirmed"])
        self.assertFalse(first["sections"][0]["confirmed"])
        self.assertTrue(first["sections"][1]["confirmed"])
        path = self.master / f"sources/{self.sid}/maps/000001.json"
        before = path.read_bytes()
        self.assert_code("map_revision_conflict", self.save_map)
        latest = self.save_map(expected_map_revision=1)
        self.assertEqual(2, latest["map_revision"])
        self.assertEqual(before, path.read_bytes())
        history = self.service.map_history(PID, self.sid)
        self.assertEqual(2, history["map_revision"])
        self.assertEqual([first, latest], history["maps"])
        self.assertEqual([], self.service.status(PID)["revisions"])
        self.assertFalse(any(s["confirmed"] for s in self.service.analysis_history(PID, self.sid)["analyses"][0]["sections"]))

    def test_labels_schema_and_caps(self):
        for request in (None, [], {"lyrics": "x"}, {"section_labels": None}, {"section_labels": "verse"},
                        {"section_labels": ["x"] * 65}, {"section_labels": [True]},
                        {"section_labels": [""]}, {"section_labels": ["x" * 121]}, {"section_labels": ["bad\nlabel"]}):
            self.assert_code("validation", self.service.create_analysis, PID, self.sid, request)
        result = self.analysis({"section_labels": ["x"] * 64})
        self.assertLessEqual(len(result["sections"]), 64)

    def test_maps_reject_bool_numbers_nonfinite_and_invalid_schema(self):
        for field, value in (("start_s", True), ("end_s", float("nan")), ("end_s", float("inf")),
                             ("confirmed", 1), ("confirmed", "true"), ("label", False)):
            sections = self.ranges()
            sections[0][field] = value
            self.assert_code("validation", self.save_map, sections=sections)
        self.assert_code("validation", self.save_map, expected_map_revision=True)
        self.assert_code("validation", self.save_map, expected_map_revision=0.0)
        self.assert_code("validation", self.save_map, sections=[])
        self.assert_code("validation", self.save_map, sections=self.ranges() * 33)
        sections = self.ranges()
        sections[0]["confidence"] = "low"
        self.assert_code("validation", self.save_map, sections=sections)
        self.assert_code("validation", self.save_map, analysis_id="../bad")
        self.assert_code("analysis_not_found", self.save_map, analysis_id="a" * 32)

    def test_complete_contiguous_coverage_no_gaps_overlaps_or_empty_ranges(self):
        for index, field, value in ((0, "start_s", 0.1), (0, "end_s", 4.9), (0, "end_s", 5.1),
                                    (1, "end_s", 10.9), (0, "end_s", 0), (1, "end_s", 12)):
            sections = self.ranges()
            sections[index][field] = value
            self.assert_code("validation", self.save_map, sections=sections)
        self.assert_code("validation", self.save_map, sections=list(reversed(self.ranges())))

    def test_missing_and_stale_sources_for_all_operations(self):
        self.assert_code("not_found", self.service.analysis_history, PID, "f" * 64)
        self.assert_code("validation", self.service.map_history, PID, "../source")
        self.analysis()
        self.original.write_bytes(b"changed original")
        for method in (self.analysis, self.save_map):
            self.assert_code("stale_source", method)
        for method in (self.service.analysis_history, self.service.map_history):
            self.assert_code("stale_source", method, PID, self.sid)

    def test_pcm_stale_and_changes_during_analysis_preserve_no_false_record(self):
        path = self.master / self.source["pcm_path"]
        path.write_bytes(b"changed PCM")
        self.assert_code("stale_source", self.analysis)
        self.assertEqual([], list((path.parent / "analyses").glob("*.json")) if (path.parent / "analyses").exists() else [])

    def test_detect_changes_between_worker_and_save(self):
        real = analyze_pcm
        def mutate(path, frames, labels):
            result = real(path, frames, labels)
            self.original.write_bytes(b"changed during computation")
            return result
        with patch("app.services.master_audio.bounded_analysis", side_effect=mutate):
            self.assert_code("stale_source", self.analysis)
        self.assertEqual([], list((self.master / f"sources/{self.sid}/analyses").glob("*.json")))

    def test_resource_caps_before_work_and_timeout_no_record(self):
        with patch("app.services.master_audio.MAX_BYTES", self.original.stat().st_size - 1), \
                patch("app.services.master_audio.bounded_analysis") as worker:
            self.assert_code("stale_source", self.analysis)
            worker.assert_not_called()
        with patch("app.services.master_audio.MAX_ANALYSES", 1):
            self.analysis()
            self.assert_code("size_limit", self.analysis)
        with patch("app.services.master_audio.ANALYSIS_TIMEOUT_S", 0):
            self.assert_code("analysis_timeout", self.analysis)
        self.assertEqual(1, len(self.service.analysis_history(PID, self.sid)["analyses"]))
        with patch("app.services.master_audio.MAX_MAPS", 1):
            self.save_map()
            self.assert_code("size_limit", self.save_map, expected_map_revision=1)
        source_json = self.master / f"sources/{self.sid}/source.json"
        record = json.loads(source_json.read_bytes())
        record.update(frames=(MAX_DURATION + 1) * RATE, duration_s=MAX_DURATION + 1)
        source_json.write_bytes(canonical(record))
        self.assert_code("evidence_invalid", self.analysis)

    def test_hash_chain_tampering_and_partial_records_fail_closed(self):
        self.save_map()
        path = self.master / f"sources/{self.sid}/maps/000001.json"
        record = json.loads(path.read_bytes())
        record["sections"][0]["confirmed"] = True
        path.write_bytes(canonical(record))
        self.assert_code("evidence_invalid", self.service.map_history, PID, self.sid)
        path.write_text("{")
        self.assert_code("evidence_invalid", self.save_map, expected_map_revision=1)

    @unittest.skipIf(os.name == "nt", "symlink privilege not assumed")
    def test_symlink_history_and_lock_rejected(self):
        directory = self.master / f"sources/{self.sid}/maps"
        directory.symlink_to(self.root, target_is_directory=True)
        self.assert_code("unsafe_path", self.save_map)
        with self.service._locked(PID):
            self.assert_code("busy", self.analysis)

    def test_original_bytes_and_sources_preserved(self):
        before = {path: sha_file(path) for path in (self.original, self.master / self.source["raw_path"], self.master / self.source["pcm_path"])}
        self.analysis()
        self.save_map()
        self.assertEqual(before, {path: sha_file(path) for path in before})

    def test_router_contract_static_compile_without_optional_fastapi(self):
        path = Path(__file__).parents[1] / "app/routers/master_audio.py"
        source = path.read_text()
        compile(source, str(path), "exec")
        module = ast.parse(source)
        endpoints = {}
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                            and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                        endpoints[(decorator.func.attr, decorator.args[0].value)] = ast.get_source_segment(source, node)
        for path_suffix, create, history in (("analysis", "create_analysis", "analysis_history"), ("maps", "create_map", "map_history")):
            route = "/sources/{source_id}/" + path_suffix
            self.assertIn('"' + create + '"', endpoints[("post", route)])
            self.assertIn("run_in_threadpool", endpoints[("post", route)])
            self.assertIn('"' + history + '"', endpoints[("get", route)])


if __name__ == "__main__":
    unittest.main()
