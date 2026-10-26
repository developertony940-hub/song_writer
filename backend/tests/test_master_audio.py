"""No third-party dependencies: python -m unittest tests.test_master_audio -v."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.master_audio import (
    CHANNELS, RATE, MasterAudioError, MasterAudioService,
    canonical, sha_file, write_new, wav_quality,
)

PID = "abcdef012345"


class Store:
    def __init__(self, root):
        self.root = Path(root)

    def _dir(self, pid):
        path = self.root / pid
        if not path.is_dir():
            raise KeyError(pid)
        return path


def settings(binary="ffmpeg"):
    return SimpleNamespace(ffmpeg=SimpleNamespace(binary=binary, process_timeout_s=30))


def make_wav(path, seconds=1):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
        # Opposite DC levels in each half make trimming/order/gain observable.
        audio.writeframes(b"".join(struct.pack("<hh", *([12000 if i < RATE * seconds / 2 else -8000] * 2))
                                   for i in range(int(RATE * seconds))))


class FakeDecodeService(MasterAudioService):
    """Only import decoding is faked; ordinary validations need no ffmpeg."""
    def _run(self, argv, directory, evidence):
        if evidence != "decode.stderr":
            raise AssertionError("validation test unexpectedly rendered")
        shutil.copyfile(argv[argv.index("-i") + 1], argv[-1])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / PID
        (self.project / "music").mkdir(parents=True)
        self.original = self.project / "music" / "take.wav"
        make_wav(self.original)
        self.service = FakeDecodeService(Store(self.root), settings())
        self.source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        self.sid = self.source["source_id"]

    def clip(self, **changes):
        value = {"clip_id": "verse", "source_id": self.sid, "start_s": 0, "end_s": 0.5,
                 "confirmation": {"confirmed": True, "source_sha256": self.sid, "start_s": 0, "end_s": 0.5}}
        value.update(changes)
        return value

    def revision(self, sections=None, expected=0):
        return self.service.create_revision(PID, {"expected_revision": expected, "sections": sections or [self.clip()]})

    def assert_code(self, code, function, *args):
        with self.assertRaises(MasterAudioError) as raised:
            function(*args)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_initial_status_and_exact_pcm_duration(self):
        status = self.service.status(PID)
        self.assertEqual(0, status["revision"])
        self.assertFalse(status["generation"])
        self.assertEqual(1, self.source["duration_s"])
        self.assertEqual("decoded_pcm_frames", self.source["duration_basis"])
        self.assertEqual("ready", status["sources"][0]["status"])

    def test_content_addressed_immutable_raw_and_idempotent_import(self):
        self.assertEqual(sha_file(self.original), self.sid)
        self.assertEqual(self.source, self.service.register_source(PID, {"relative_path": "music/take.wav"}))
        path = self.service.file(PID, self.source["raw_path"])
        self.assertEqual(path.read_bytes(), self.original.read_bytes())

    def test_append_only_chain_and_reorder_split(self):
        first = self.revision()
        before = (self.project / "master-audio/revisions/000001.json").read_bytes()
        second = self.clip(clip_id="last", start_s=0.5, end_s=1,
                           confirmation={"confirmed": True, "source_sha256": self.sid, "start_s": 0.5, "end_s": 1})
        latest = self.revision([second, self.clip()], expected=1)
        self.assertEqual(first["recipe_sha256"], latest["parent_recipe_sha256"])
        self.assertEqual(["last", "verse"], [c["clip_id"] for c in latest["sections"]])
        self.assertEqual(before, (self.project / "master-audio/revisions/000001.json").read_bytes())
        self.assertEqual(2, self.service.status(PID)["revision"])

    def test_expected_revision_conflict_and_bool_rejected(self):
        self.revision()
        self.assert_code("revision_conflict", self.revision)
        self.assert_code("validation", self.revision, None, True)

    def test_confirmation_required_exact_boolean_and_source_hash(self):
        for value in (1, "true", None):
            clip = self.clip()
            clip["confirmation"]["confirmed"] = value
            self.assert_code("confirmation_required", self.revision, [clip])
        clip = self.clip()
        clip["confirmation"]["source_sha256"] = "a" * 64
        self.assert_code("confirmation_required", self.revision, [clip])

    def test_changed_range_invalidates_old_confirmation(self):
        self.assert_code("confirmation_required", self.revision, [self.clip(end_s=0.7)])

    def test_unconfirmed_draft_saves_reloads_and_blocks_render_before_claim(self):
        clip = self.clip()
        clip["confirmation"]["confirmed"] = False
        rev = self.revision([clip])
        self.assertFalse(rev["sections"][0]["confirmation"]["confirmed"])
        self.assertIsNone(rev["sections"][0]["confirmation"]["confirmed_at"])
        self.assertIsNone(rev["sections"][0]["confirmation"]["clip_sha256"])
        reopened = FakeDecodeService(Store(self.root), settings()).status(PID)
        self.assertEqual(rev, reopened["revisions"][0])
        self.assert_code("confirmation_required", self.service.render, PID,
                         {"expected_revision": 1, "render_id": "draft_render"})
        self.assertEqual([], self.service.status(PID)["renders"])
        self.assertFalse((self.project / "master-audio/renders/draft_render").exists())

    def test_draft_following_saved_revision_preserves_prior_bytes_and_review_state(self):
        first = self.revision()
        path = self.project / "master-audio/revisions/000001.json"
        original = path.read_bytes()
        clip = self.clip(clip_id="draft")
        clip["confirmation"]["confirmed"] = False
        second = self.revision([self.clip(), clip], expected=1)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(first["recipe_sha256"], second["parent_recipe_sha256"])
        self.assertEqual([True, False], [c["confirmation"]["confirmed"] for c in second["sections"]])
        self.assertEqual(2, self.service.status(PID)["revision"])
        self.assert_code("confirmation_required", self.service.render, PID,
                         {"expected_revision": 2, "render_id": "mixed_draft"})

    def test_confirming_draft_creates_new_revision_without_mutating_saved_draft(self):
        clip = self.clip()
        clip["confirmation"]["confirmed"] = False
        self.revision([clip])
        path = self.project / "master-audio/revisions/000001.json"
        original = path.read_bytes()
        second = self.revision(expected=1)
        self.assertTrue(second["sections"][0]["confirmation"]["confirmed"])
        self.assertIsNotNone(second["sections"][0]["confirmation"]["clip_sha256"])
        self.assertEqual(original, path.read_bytes())

    def test_unconfirmed_drafts_still_require_current_source_bounds_and_review_binding(self):
        clip = self.clip()
        clip["confirmation"]["confirmed"] = False
        clip["end_s"] = 0.7
        self.assert_code("confirmation_required", self.revision, [clip])
        clip["confirmation"]["end_s"] = 0.7
        clip["gain_db"] = float("inf")
        self.assert_code("validation", self.revision, [clip])

    def test_source_bounds_and_nonfinite_inputs(self):
        for field, value in (("start_s", -1), ("end_s", 2), ("start_s", float("nan")),
                             ("gain_db", float("inf")), ("gain_db", 13), ("fade_in_s", True)):
            self.assert_code("validation", self.revision, [self.clip(**{field: value})])
        self.assert_code("validation", self.revision, [self.clip(start_s=0.5)])

    def test_fades_cannot_overlap(self):
        self.assert_code("validation", self.revision, [self.clip(fade_in_s=0.4, fade_out_s=0.4)])

    def test_unknown_fields_no_arrangement_timings(self):
        self.assert_code("validation", self.revision, [self.clip(arrangement_index=0)])
        self.assert_code("validation", self.service.register_source, PID, {"relative_path": "music/take.wav", "path": "/tmp/x"})

    def test_duplicate_ids_and_limit(self):
        self.assert_code("validation", self.revision, [self.clip(), self.clip()])
        self.assert_code("validation", self.revision, [self.clip(clip_id=str(i)) for i in range(65)])

    def test_unsafe_paths(self):
        for value in ("../music/take.wav", "/tmp/take.wav", "music/../music/take.wav", "music\\take.wav", "C:/take.wav", "music//take.wav"):
            self.assert_code("validation", self.service.register_source, PID, {"relative_path": value})
        (self.project / "other").mkdir()
        shutil.copyfile(self.original, self.project / "other/take.wav")
        self.assert_code("validation", self.service.register_source, PID, {"relative_path": "other/take.wav"})

    @unittest.skipIf(os.name == "nt", "symlink privilege not assumed")
    def test_symlink_source_and_project_and_evidence(self):
        (self.project / "music/link.wav").symlink_to(self.original)
        self.assert_code("unsafe_path", self.service.register_source, PID, {"relative_path": "music/link.wav"})
        link_id = "000000000000"
        (self.root / link_id).symlink_to(self.project, target_is_directory=True)
        self.assert_code("unsafe_path", self.service.status, link_id)
        pcm = self.project / "master-audio" / self.source["pcm_path"]
        pcm.unlink()
        pcm.symlink_to(self.original)
        self.assert_code("unsafe_path", self.revision)

    def test_stale_original_rejects_revision_peaks_and_delivery(self):
        self.original.write_bytes(b"changed")
        self.assert_code("stale_source", self.revision)
        self.assert_code("stale_source", self.service.peaks, PID, self.sid)
        self.assert_code("stale_source", self.service.file, PID, self.source["raw_path"])
        self.assertEqual("stale", self.service.status(PID)["sources"][0]["status"])

    def test_stale_copied_source_rejected(self):
        raw = self.project / "master-audio" / self.source["raw_path"]
        raw.write_bytes(b"changed")
        self.assert_code("stale_source", self.revision)

    def test_reimport_changed_asset_creates_new_id_confirmation_not_reused(self):
        make_wav(self.original, seconds=0.8)
        new = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        self.assertNotEqual(self.sid, new["source_id"])
        self.assert_code("confirmation_required", self.revision, [self.clip(source_id=new["source_id"])])

    def test_peaks_bounded_and_signs(self):
        result = self.service.peaks(PID, self.sid, 16)
        self.assertEqual(16, len(result["peaks"]))
        self.assertGreater(result["peaks"][0][1], 0.3)
        self.assertLess(result["peaks"][-1][0], -0.2)
        self.assert_code("validation", self.service.peaks, PID, self.sid, 4097)

    def test_upload_and_size_bounds(self):
        self.assert_code("validation", self.service.upload_source, PID, "../take.wav", b"x")
        self.assert_code("size_limit", self.service.upload_source, PID, "take.wav", b"")
        with patch("app.services.master_audio.MAX_BYTES", 5):
            self.assert_code("size_limit", self.service.upload_source, PID, "take.wav", b"123456")
        other = self.original.read_bytes()
        # Existing identical bytes deduplicate; upload is not a new pathname.
        uploaded = self.service.upload_source(PID, "take.wav", other)
        self.assertEqual(self.sid, uploaded["source_id"])

    def test_upload_unicode_spaces_and_original_filename(self):
        make_wav(self.original, seconds=0.8)
        uploaded = self.service.upload_source(PID, "My Café — final mix 🎵.WAV", self.original.read_bytes())
        self.assertEqual("My Café — final mix 🎵.WAV", uploaded["original_filename"])
        self.assertIsNone(uploaded["original_relative_path"])
        self.assertEqual("raw.wav", Path(uploaded["raw_path"]).name)
        for filename in ("..", "bad:name.wav", "bad/name.wav", "bad\\name.wav", "bad\x00name.wav", "bad\nname.wav"):
            self.assert_code("validation", self.service.upload_source, PID, filename, b"audio")

    def test_lock_same_process(self):
        with self.service._locked(PID):
            self.assert_code("busy", self.service.status, PID)

    @unittest.skipIf(os.name == "nt", "POSIX child lock test")
    def test_lock_other_process(self):
        script = """import sys
from pathlib import Path
from types import SimpleNamespace
from app.services.master_audio import MasterAudioService, MasterAudioError
class Store:
    root=Path(sys.argv[1])
    def _dir(self,pid): return self.root/pid
s=MasterAudioService(Store(),SimpleNamespace(ffmpeg=SimpleNamespace(binary='ffmpeg',process_timeout_s=10)))
try: s.status(sys.argv[2])
except MasterAudioError as e: print(e.code)
"""
        with self.service._locked(PID):
            child = subprocess.run([os.sys.executable, "-c", script, str(self.root), PID], capture_output=True, text=True, check=True)
        self.assertEqual("busy", child.stdout.strip())

    def test_failed_render_retains_claim_and_cannot_retry(self):
        self.revision()
        with patch.object(self.service, "_run", side_effect=MasterAudioError("synthetic failure", "processing_failed", 502)):
            self.assert_code("processing_failed", self.service.render, PID, {"expected_revision": 1, "render_id": "first"})
        result = self.service.status(PID)["renders"][0]
        self.assertEqual("failed", result["status"])
        self.assertTrue(result["evidence_preserved"])
        self.assertTrue((self.project / "master-audio/renders/first/recipe.json").is_file())
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "retry"})

    def test_uncertain_render_claim_blocks_retry(self):
        self.revision()
        directory = self.project / "master-audio/renders/uncertain"
        directory.mkdir()
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "retry"})

    def test_claim_without_result_blocks_same_revision_and_id(self):
        self.revision()
        directory = self.project / "master-audio/renders/interrupted"
        directory.mkdir()
        write_new(directory / "claim.json", {"render_id": "interrupted", "revision": 1})
        self.assertEqual("uncertain", self.service.status(PID)["renders"][0]["status"])
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "other"})
        self.revision(expected=1)
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 2, "render_id": "interrupted"})

    def test_source_changed_after_revision_cannot_render(self):
        self.revision()
        self.original.write_bytes(b"changed by concurrent generation")
        self.assert_code("stale_source", self.service.render, PID, {"expected_revision": 1, "render_id": "stale"})
        self.assertEqual([], self.service.status(PID)["renders"])

    def test_tampered_revision_and_partial_json_fail_closed(self):
        self.revision()
        path = self.project / "master-audio/revisions/000001.json"
        data = json.loads(path.read_text())
        data["sections"][0]["gain_db"] = 9
        path.write_bytes(canonical(data))
        self.assert_code("evidence_invalid", self.service.status, PID)
        path.write_text("{")
        self.assert_code("evidence_invalid", self.service.status, PID)

    def test_file_delivery_only_allowlisted(self):
        self.assert_code("not_found", self.service.file, PID, f"sources/{self.sid}/source.json")
        self.assert_code("not_found", self.service.file, PID, ".lock")
        self.assert_code("validation", self.service.file, PID, "../manifest.json")

    def test_no_render_without_revision_and_no_arbitrary_project(self):
        self.assert_code("confirmation_required", self.service.render, PID, {"expected_revision": 0, "render_id": "test"})
        self.assert_code("not_found", self.service.status, "../music")

    def test_json_records_exclusive(self):
        path = self.root / "record.json"
        write_new(path, {"a": 1})
        self.assert_code("conflict", write_new, path, {"a": 2})
        self.assertEqual({"a": 1}, json.loads(path.read_bytes()))

    def test_quality_measures_signed_rails_and_silence(self):
        path = self.root / "quality.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(struct.pack("<hhhh", -32768, 32767, 0, 123))
        quality = wav_quality(path)
        self.assertEqual(0, quality["sample_peak_dbfs"])
        self.assertEqual(2, quality["full_scale_samples"])
        self.assertEqual(4, quality["total_samples"])
        self.assertIn("not proof of clipping", quality["warnings"][0])
        with wave.open(str(path), "wb") as audio:
            audio.setparams((CHANNELS, 2, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(8))
        quality = wav_quality(path)
        self.assertIsNone(quality["sample_peak_dbfs"])
        self.assertEqual(0, quality["full_scale_samples"])
        self.assertEqual([], quality["warnings"])

    def test_process_timeout_retains_append_only_failure_evidence(self):
        self.revision()
        service = MasterAudioService(Store(self.root), settings())
        with patch("app.services.master_audio.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)) as run:
            self.assert_code("processing_failed", service.render, PID, {"expected_revision": 1, "render_id": "timeout"})
        self.assertEqual(30, run.call_args.kwargs["timeout"])
        self.assertFalse(run.call_args.kwargs["shell"])
        directory = self.project / "master-audio/renders/timeout"
        self.assertTrue((directory / "clip-000.stderr.argv.json").is_file())
        self.assertTrue((directory / "clip-000.stderr").is_file())
        before = (directory / "result.json").read_bytes()
        self.assertEqual("failed", json.loads(before)["status"])
        self.assert_code("render_claimed", service.render, PID, {"expected_revision": 1, "render_id": "timeout"})
        self.assertEqual(before, (directory / "result.json").read_bytes())


@unittest.skipUnless(shutil.which("ffmpeg"), "system ffmpeg unavailable; no install attempted")
class RealFFmpegTests(ValidationTests):
    # Run only the explicit integration tests below, not inherited validations.
    def setUp(self):
        super().setUp()
        self.service = MasterAudioService(Store(self.root), settings())

    def test_real_trim_split_reorder_gain_fades_wav_then_mp3(self):
        raw_before = self.original.read_bytes()
        last = self.clip(clip_id="second-half", start_s=0.5, end_s=1, gain_db=-6,
                         confirmation={"confirmed": True, "source_sha256": self.sid, "start_s": 0.5, "end_s": 1})
        self.revision([last, self.clip(fade_in_s=0.1, fade_out_s=0.1)])
        result = self.service.render(PID, {"expected_revision": 1, "render_id": "master-1"})
        self.assertEqual("completed", result["status"])
        self.assertEqual(RATE, result["frames"])
        wav_path = self.service.file(PID, result["files"]["wav"]["relative_path"])
        with wave.open(str(wav_path), "rb") as audio:
            pcm = struct.unpack("<" + "h" * audio.getnframes() * CHANNELS, audio.readframes(audio.getnframes()))
        self.assertAlmostEqual(-8000 * 10 ** (-6 / 20), pcm[1000], delta=2)
        self.assertAlmostEqual(0, pcm[RATE], delta=2)
        self.assertAlmostEqual(12000, pcm[RATE + 12000], delta=2)
        self.assertLess(abs(pcm[-2]), 10)
        self.assertEqual(raw_before, self.original.read_bytes())
        argv = json.loads((wav_path.parent / "mp3.stderr.argv.json").read_text())
        self.assertEqual(str(wav_path), argv[argv.index("-i") + 1])
        self.assertEqual(sha_file(wav_path), result["mp3_input_sha256"])
        self.assertTrue(result["mp3_verified"])
        self.assertEqual(320, result["mp3_bitrate_kbps"])
        self.assertEqual(RATE * CHANNELS, result["quality"]["total_samples"])
        self.assertEqual(0, result["quality"]["full_scale_samples"])
        self.assertEqual([], result["quality"]["warnings"])
        self.assertAlmostEqual(result["frames"], result["mp3_decoded_frames"], delta=2304)
        self.assertEqual("320k", argv[argv.index("-b:a") + 1])
        self.assertTrue(self.service.file(PID, result["files"]["mp3"]["relative_path"]).stat().st_size > 1000)
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "again"})

    def test_real_import_mp3_and_decode_observed_duration(self):
        mp3 = self.project / "music" / "encoded.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(self.original), "-c:a", "libmp3lame", str(mp3)], check=True)
        source = self.service.register_source(PID, {"relative_path": "music/encoded.mp3"})
        self.assertEqual(sha_file(mp3), source["sha256"])
        self.assertAlmostEqual(1, source["duration_s"], delta=0.03)
        self.assertEqual(source["frames"], round(source["duration_s"] * RATE))

    def test_real_gain_fullscale_quality_is_measured_persisted_and_warned(self):
        original_hash = sha_file(self.original)
        self.revision([self.clip(gain_db=12)])
        result = self.service.render(PID, {"expected_revision": 1, "render_id": "gain-fullscale"})
        quality = result["quality"]
        self.assertEqual("completed", result["status"])
        self.assertEqual("master_wav_pcm_s16le", quality["basis"])
        self.assertEqual(RATE, quality["total_samples"])
        self.assertEqual(RATE, quality["full_scale_samples"])
        self.assertAlmostEqual(0, quality["sample_peak_dbfs"], delta=0.001)
        self.assertTrue(quality["warnings"])
        record = result["files"]["quality"]
        path = self.service.file(PID, record["relative_path"])
        self.assertEqual(quality, json.loads(path.read_bytes()))
        self.assertEqual(sha_file(path), record["sha256"])
        self.assertEqual(original_hash, sha_file(self.original))
        path.write_bytes(b"tampered")
        self.assert_code("not_found", self.service.file, PID, record["relative_path"])

    def test_real_missing_ffmpeg_preserves_import_evidence(self):
        make_wav(self.original, seconds=0.9)
        self.service.ffmpeg = str(self.root / "missing-ffmpeg")
        self.assert_code("tool_unavailable", self.service.register_source, PID, {"relative_path": "music/take.wav"})
        sources = self.service.status(PID)["sources"]
        self.assertEqual(1, sum(s.get("status") == "uncertain" for s in sources))
        self.assert_code("source_uncertain", self.service.register_source, PID, {"relative_path": "music/take.wav"})

    def test_real_mp3_failure_preserves_wav_and_forbids_retry(self):
        self.revision()
        run = self.service._run
        def fail_mp3(argv, directory, evidence):
            if evidence == "mp3.stderr":
                raise MasterAudioError("synthetic encoder failure", "processing_failed", 502)
            return run(argv, directory, evidence)
        with patch.object(self.service, "_run", side_effect=fail_mp3):
            self.assert_code("processing_failed", self.service.render, PID, {"expected_revision": 1, "render_id": "wav-only"})
        wav = self.project / "master-audio/renders/wav-only/master.wav"
        self.assertTrue(wav.is_file())
        self.assertTrue((wav.parent / "quality.json").is_file())
        with wave.open(str(wav), "rb") as audio:
            self.assertEqual(RATE // 2, audio.getnframes())
        self.assert_code("not_found", self.service.file, PID, "renders/wav-only/master.wav")
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "retry"})

    def test_real_completed_master_hash_tamper_refuses_delivery(self):
        self.revision()
        result = self.service.render(PID, {"expected_revision": 1, "render_id": "tamper"})
        relative = result["files"]["mp3"]["relative_path"]
        (self.project / "master-audio" / relative).write_bytes(b"tampered")
        self.assert_code("not_found", self.service.file, PID, relative)

    def test_real_corrupt_mp3_is_not_declared_complete(self):
        self.revision()
        run = self.service._run
        def corrupt_mp3(argv, directory, evidence):
            if evidence == "mp3.stderr":
                (directory / "master.mp3").write_bytes(b"not a playable mp3")
                return
            return run(argv, directory, evidence)
        with patch.object(self.service, "_run", side_effect=corrupt_mp3):
            self.assert_code("processing_failed", self.service.render, PID, {"expected_revision": 1, "render_id": "bad-mp3"})
        result = self.service.status(PID)["renders"][0]
        self.assertEqual("failed", result["status"])
        self.assertTrue((self.project / "master-audio/renders/bad-mp3/master.wav").is_file())
        self.assert_code("render_claimed", self.service.render, PID, {"expected_revision": 1, "render_id": "retry-corrupt"})


# Avoid duplicating the validation suite through inheritance while retaining
# its fixture helpers for real ffmpeg tests.
for _name in list(ValidationTests.__dict__):
    if _name.startswith("test_"):
        setattr(RealFFmpegTests, _name, None)


if __name__ == "__main__":
    unittest.main()
