"""Bounded local section WAV export: python -m unittest tests.test_section_export -v."""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.services.master_audio import (
    CHANNELS, RATE, MAX_DURATION, MasterAudioError, MasterAudioService,
    canonical, digest, sha_file,
)
from tests.test_master_audio import FakeDecodeService, Store, PID, settings


class SectionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / PID
        (self.project / "music").mkdir(parents=True)
        self.original = self.project / "music/take.wav"
        # Every frame is observable; channels must not be averaged/cancelled.
        with wave.open(str(self.original), "wb") as audio:
            audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(b"".join(struct.pack("<hh", i % 30000, -(i % 30000)) for i in range(RATE)))
        self.service = FakeDecodeService(Store(self.root), settings())
        self.source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        self.sid = self.source["source_id"]
        self.master = self.project / "master-audio"

    def request(self, **changes):
        request = {"source_sha256": self.sid, "start_s": 0.25, "end_s": 0.75, "label": "Verse 3"}
        request.update(changes)
        return request

    def export(self, **changes):
        return self.service.create_section_export(PID, self.sid, self.request(**changes))

    def assert_code(self, code, function, *args):
        with self.assertRaises(MasterAudioError) as result:
            function(*args)
        self.assertEqual(code, result.exception.code)

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}

    def assert_exact(self, result):
        output = self.service.section_export_file(PID, result["export_id"], "wav")
        with wave.open(str(output), "rb") as audio:
            self.assertEqual((CHANNELS, 2, RATE, result["frames"]),
                             (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()))
            actual = audio.readframes(audio.getnframes())
        with wave.open(str(self.master / self.source["pcm_path"]), "rb") as source:
            source.setpos(result["start_frame"])
            expected = source.readframes(result["frames"])
        self.assertEqual(expected, actual)
        self.assertEqual(hashlib.sha256(actual).hexdigest(), result["sample_data_sha256"])
        self.assertEqual(sha_file(output), result["files"]["wav"]["sha256"])
        self.assertEqual(digest({k: v for k, v in result.items() if k != "record_sha256"}), result["record_sha256"])

    def test_exact_frame_copy_lineage_and_manifest_download(self):
        result = self.export()
        self.assertEqual("completed", result["status"])
        self.assertEqual((12000, 36000, 24000), (result["start_frame"], result["end_frame"], result["frames"]))
        self.assertEqual(self.sid, result["source_sha256"])
        self.assertEqual(self.source["pcm_sha256"], result["pcm_sha256"])
        self.assertEqual("Verse 3", result["label"])
        self.assertEqual("full_mix_not_isolated_vocals", result["audio_scope"])
        self.assertFalse(result["generation"])
        self.assertFalse(result["listening_confirmed"])
        self.assertFalse(result["processing"]["source_separation"])
        self.assert_exact(result)
        path = self.service.section_export_file(PID, result["export_id"], "manifest")
        self.assertEqual(result, json.loads(path.read_bytes()))
        claim = json.loads((path.parent / "claim.json").read_bytes())
        self.assertEqual(digest(claim), result["claim_sha256"])

    def test_single_frame_full_source_and_nearest_frame_rounding(self):
        first = self.export(start_s=0.49 / RATE, end_s=0.51 / RATE)
        self.assertEqual((0, 1), (first["start_frame"], first["end_frame"]))
        self.assert_exact(first)
        second = self.export(start_s=0, end_s=1)
        self.assertEqual(RATE, second["frames"])
        self.assert_exact(second)
        third = self.export(start_s=0.5 / RATE, end_s=1.5 / RATE)
        self.assertEqual((1, 2), (third["start_frame"], third["end_frame"]))

    def test_append_only_chronological_persistent_list_and_preserved_histories(self):
        self.service.create_map(PID, self.sid, {"expected_map_revision": 0, "sections": [
            {"label": "All", "start_s": 0, "end_s": 1, "confirmed": False}]})
        self.service.create_revision(PID, {"expected_revision": 0, "sections": [{
            "clip_id": "clip", "source_id": self.sid, "start_s": 0, "end_s": 1,
            "confirmation": {"confirmed": True, "source_sha256": self.sid, "start_s": 0, "end_s": 1}}]})
        before = self.snapshot()
        one = self.export()
        after_one = self.snapshot()
        two = self.export(label="Second")
        for name, value in after_one.items():
            self.assertEqual(value, (self.project / name).read_bytes())
        for name, value in before.items():
            self.assertEqual(value, (self.project / name).read_bytes())
        self.assertNotEqual(one["export_id"], two["export_id"])
        service = FakeDecodeService(Store(self.root), settings())
        self.assertEqual([one, two], service.section_exports(PID, self.sid)["exports"])
        self.assertEqual(1, service.status(PID)["revision"])
        self.assertEqual([], service.status(PID)["renders"])

    def test_finite_range_schema_and_boolean_rejection(self):
        cases = [{"start_s": True}, {"end_s": False}, {"start_s": float("nan")},
                 {"end_s": float("inf")}, {"start_s": "0"}, {"start_s": -1},
                 {"end_s": 1.1}, {"start_s": 0.8}, {"end_s": 0.25},
                 {"start_s": 0, "end_s": 0.1 / RATE}, {"label": ""},
                 {"label": "bad\nlabel"}, {"label": "x" * 121},
                 {"source_sha256": True}, {"source_sha256": "bad"}, {"gain_db": 1},
                 {"confirmed": True}, {"label": False}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_code("validation", self.service.create_section_export, PID, self.sid, self.request(**changes))
        for request in ([], None, {}, {"start_s": 0}):
            self.assert_code("validation", self.service.create_section_export, PID, self.sid, request)
        self.assertFalse((self.master / "section-exports").exists())

    def test_stale_hash_and_original_raw_pcm_tampering(self):
        self.assert_code("stale_source", self.service.create_section_export, PID, self.sid,
                         self.request(source_sha256="0" * 64))
        for path in (self.original, self.master / self.source["raw_path"], self.master / self.source["pcm_path"]):
            original = path.read_bytes()
            path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            self.assert_code("stale_source", self.service.create_section_export, PID, self.sid, self.request())
            path.write_bytes(original)

    def test_missing_source_and_unsafe_identifiers(self):
        self.assert_code("not_found", self.service.create_section_export, PID, "0" * 64, self.request())
        self.assert_code("validation", self.service.create_section_export, PID, "../source", self.request())
        for eid in ("../x", "x", "a" * 32 + "/../x"):
            self.assert_code("not_found", self.service.section_export_file, PID, eid, "wav")
        self.assert_code("not_found", self.service.section_export_file, PID, "a" * 32, "raw")

    def test_label_is_metadata_not_filename(self):
        result = self.export(label="../../Verse 3\\name:label")
        self.assertEqual("section.wav", self.service.section_export_file(PID, result["export_id"], "wav").name)
        self.assertEqual("../../Verse 3\\name:label", result["label"])

    def test_locks_and_symlink_paths(self):
        with self.service._locked(PID):
            self.assert_code("busy", self.service.create_section_export, PID, self.sid, self.request())
        outside = self.root / "outside"
        outside.mkdir()
        (self.master / "section-exports").symlink_to(outside, target_is_directory=True)
        self.assert_code("unsafe_path", self.service.create_section_export, PID, self.sid, self.request())
        self.assertEqual([], list(outside.iterdir()))

    def test_count_and_cumulative_duration_caps(self):
        self.export()
        with patch("app.services.master_audio.MAX_SECTION_EXPORTS", 1):
            self.assert_code("size_limit", self.service.create_section_export, PID, self.sid, self.request())
        with patch("app.services.master_audio.MAX_SECTION_EXPORT_DURATION", 0.75):
            self.assert_code("size_limit", self.service.create_section_export, PID, self.sid, self.request())
        self.assertEqual(1, len(list((self.master / "section-exports").iterdir())))

    def test_source_duration_and_size_caps_before_processing(self):
        source_path = self.master / f"sources/{self.sid}/source.json"
        saved = source_path.read_bytes()
        record = json.loads(saved)
        record["frames"] = (MAX_DURATION + 1) * RATE
        record["duration_s"] = MAX_DURATION + 1
        source_path.write_bytes(canonical(record))
        self.assert_code("evidence_invalid", self.service.create_section_export, PID, self.sid, self.request())
        source_path.write_bytes(saved)
        with patch("app.services.master_audio.MAX_BYTES", 16):
            self.assert_code("stale_source", self.service.create_section_export, PID, self.sid, self.request())

    def test_deadline_failure_preserves_evidence_without_success_manifest(self):
        before = self.original.read_bytes()
        with patch("app.services.master_audio.SECTION_EXPORT_TIMEOUT_S", -1):
            self.assert_code("export_timeout", self.service.create_section_export, PID, self.sid, self.request())
        directory = next((self.master / "section-exports").iterdir())
        self.assertTrue((directory / "claim.json").is_file())
        self.assertTrue((directory / "failure.json").is_file())
        self.assertFalse((directory / "manifest.json").exists())
        self.assertEqual("failed", self.service.section_exports(PID, self.sid)["exports"][0]["status"])
        self.assertEqual(before, self.original.read_bytes())
        self.assert_code("not_found", self.service.section_export_file, PID, directory.name, "wav")

    def test_change_during_copy_never_publishes_success(self):
        real = self.service._export_hash
        def change_after_hash(path, deadline):
            result = real(path, deadline)
            with self.original.open("ab") as stream:
                stream.write(b"changed")
            return result
        with patch.object(self.service, "_export_hash", side_effect=change_after_hash):
            self.assert_code("stale_source", self.service.create_section_export, PID, self.sid, self.request())
        self.assertEqual([], list((self.master / "section-exports").glob("*/manifest.json")))

    def test_failed_manifest_publication_is_never_a_downloadable_success(self):
        from app.services.master_audio import write_new
        def failed_flush(path, value):
            write_new(path, value)
            if path.name == "manifest.json":
                raise OSError("simulated final fsync error")
        with patch("app.services.master_audio.write_new", side_effect=failed_flush):
            self.assert_code("processing_failed", self.service.create_section_export, PID, self.sid, self.request())
        record = self.service.section_exports(PID, self.sid)["exports"][0]
        self.assertEqual("failed", record["status"])
        for kind in ("wav", "manifest"):
            self.assert_code("not_found", self.service.section_export_file, PID, record["export_id"], kind)

    def test_tampered_wav_and_manifest_block_download(self):
        result = self.export()
        path = self.master / result["files"]["wav"]["relative_path"]
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        for kind in ("wav", "manifest"):
            self.assert_code("stale_export", self.service.section_export_file, PID, result["export_id"], kind)
        path.write_bytes(original)
        manifest = path.parent / "manifest.json"
        record = json.loads(manifest.read_bytes())
        record["label"] = "tampered"
        manifest.write_bytes(canonical(record))
        self.assert_code("evidence_invalid", self.service.section_export_file, PID, result["export_id"], "manifest")

    def test_interrupted_claim_consumes_capacity_without_becoming_downloadable(self):
        directory = self.master / "section-exports" / ("a" * 32)
        directory.mkdir(parents=True)
        with patch("app.services.master_audio.MAX_SECTION_EXPORTS", 1):
            self.assert_code("size_limit", self.service.create_section_export, PID, self.sid, self.request())
        self.assert_code("not_found", self.service.section_export_file, PID, directory.name, "wav")

    def test_truncated_pcm_with_matching_hash_fails_closed(self):
        pcm = self.master / self.source["pcm_path"]
        pcm.write_bytes(pcm.read_bytes()[:100])
        source_path = self.master / f"sources/{self.sid}/source.json"
        record = json.loads(source_path.read_bytes())
        record["pcm_sha256"] = sha_file(pcm)
        source_path.write_bytes(canonical(record))
        self.assert_code("stale_source", self.service.create_section_export, PID, self.sid, self.request())
        self.assertEqual([], list((self.master / "section-exports").glob("*/manifest.json")))

    def test_route_contract_compiles_without_optional_web_dependencies(self):
        router = Path(__file__).resolve().parents[1] / "app/routers/master_audio.py"
        source = router.read_text()
        tree = ast.parse(source)
        compile(tree, str(router), "exec")
        routes = {(node.func.attr, node.args[0].value) for function in tree.body
                  if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                  for node in function.decorator_list if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.args and isinstance(node.args[0], ast.Constant)}
        for route in (("post", "/sources/{source_id}/exports"), ("get", "/sources/{source_id}/exports"),
                      ("get", "/section-exports/{export_id}/wav"), ("get", "/section-exports/{export_id}/manifest")):
            self.assertIn(route, routes)


@unittest.skipUnless(shutil.which("ffmpeg"), "existing local ffmpeg unavailable")
class RealFFmpegSectionExportTests(SectionExportTests):
    # Only these two integration tests use the real decoder. Reuse fixtures,
    # not the whole parent test collection, via load_tests below.
    def test_real_ffmpeg_wav_decode_then_exact_export(self):
        self.service = MasterAudioService(Store(self.root), settings())
        other = self.project / "music/real.wav"
        with wave.open(str(other), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(struct.pack("<h", 12345) * 24000)
        self.source = self.service.register_source(PID, {"relative_path": "music/real.wav"})
        self.sid = self.source["source_id"]
        result = self.service.create_section_export(PID, self.sid, self.request())
        self.assert_exact(result)

    def test_real_mp3_import_and_export_matches_decoded_pcm(self):
        self.service = MasterAudioService(Store(self.root), settings())
        mp3 = self.project / "music/take.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(self.original),
                        "-c:a", "libmp3lame", str(mp3)], check=True, timeout=30,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.source = self.service.register_source(PID, {"relative_path": "music/take.mp3"})
        self.sid = self.source["source_id"]
        result = self.export()
        self.assert_exact(result)
        self.assertEqual(sha_file(mp3), result["source_sha256"])


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(SectionExportTests)
    for name in ("test_real_ffmpeg_wav_decode_then_exact_export", "test_real_mp3_import_and_export_matches_decoded_pcm"):
        suite.addTest(RealFFmpegSectionExportTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
