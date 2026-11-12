"""Insert-specific declaration guard and boundary guards.

Uses a synthetic nonmusical WAV and a fake decoder. No ffmpeg, real project,
model or audio processing is involved.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.master_audio import MasterAudioError
from tests.test_master_audio import FakeDecodeService, PID, Store, make_wav, settings


class InsertGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root / PID
        (project / "music").mkdir(parents=True)
        make_wav(project / "music" / "take.wav", seconds=2)
        self.service = FakeDecodeService(Store(self.root), settings())
        self.source = self.service.register_source(PID, {"relative_path": "music/take.wav"})
        self.sid = self.source["source_id"]
        self.base = self._base_revision()

    def _clip(self, clip_id, start, end, *, gain=0.0, confirmed=True):
        return {
            "clip_id": clip_id,
            "source_id": self.sid,
            "start_s": start,
            "end_s": end,
            "gain_db": gain,
            "fade_in_s": 0,
            "fade_out_s": 0,
            "label": clip_id,
            "confirmation": {
                "confirmed": confirmed,
                "source_sha256": self.sid,
                "start_s": start,
                "end_s": end,
            },
        }

    def _base_revision(self):
        sections = [
            self._clip("a", 0.0, 0.4),
            self._clip("b", 0.4, 0.8),
        ]
        return self.service.create_revision(
            self.pid_request(),
            {"expected_revision": 0, "sections": sections, "note": "base"},
        )

    def pid_request(self):
        return PID

    def _declaration(self, actions, final_order, delta):
        return {
            "base_revision": 1,
            "base_recipe_sha256": self.base["recipe_sha256"],
            "delta_s": delta,
            "final_order": final_order,
            "actions": actions,
        }

    def test_insert_declaration_preserves_followers_and_adds_duration(self):
        sections = [
            self._clip("a", 0.0, 0.4),
            self._clip("c", 0.0, 0.2, confirmed=False),
            self._clip("b", 0.4, 0.8),
        ]
        declaration = self._declaration(
            [{"kind": "insert", "clip_id": "c", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.2}],
            ["a", "c", "b"], 0.2)
        revision = self.service.create_revision(PID, {
            "expected_revision": 1, "sections": sections,
            "note": "insert c before b", "declaration": declaration,
        })
        self.assertEqual(revision["revision"], 2)
        self.assertAlmostEqual(revision["duration_s"], 1.0, places=6)

    def test_insert_may_not_alter_a_follower(self):
        sections = [
            self._clip("a", 0.0, 0.4, gain=-3.0),
            self._clip("c", 0.0, 0.2, confirmed=False),
            self._clip("b", 0.4, 0.8),
        ]
        declaration = self._declaration(
            [{"kind": "insert", "clip_id": "c", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.2}],
            ["a", "c", "b"], 0.2)
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": sections,
                "declaration": declaration,
            })
        self.assertEqual(caught.exception.code, "ripple_invalid")

    def test_insert_may_not_shorten_a_follower(self):
        sections = [
            self._clip("a", 0.0, 0.4),
            self._clip("c", 0.0, 0.2, confirmed=False),
            self._clip("b", 0.4, 0.7),
        ]
        declaration = self._declaration(
            [{"kind": "insert", "clip_id": "c", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.2}],
            ["a", "c", "b"], 0.2)
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": sections,
                "declaration": declaration,
            })
        self.assertEqual(caught.exception.code, "ripple_invalid")

    def test_insert_may_not_remove_or_reorder_followers(self):
        declaration = self._declaration(
            [{"kind": "insert", "clip_id": "c", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.2}],
            ["a", "c"], 0.2)
        removed = [self._clip("a", 0.0, 0.4), self._clip("c", 0.0, 0.2, confirmed=False)]
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": removed,
                "declaration": declaration,
            })
        self.assertEqual(caught.exception.code, "ripple_invalid")
        reordered = [
            self._clip("b", 0.4, 0.8),
            self._clip("a", 0.0, 0.4),
            self._clip("c", 0.0, 0.2, confirmed=False),
        ]
        declaration = self._declaration(
            [{"kind": "insert", "clip_id": "c", "placement": "append",
              "anchor_clip_id": None, "delta_s": 0.2}],
            ["b", "a", "c"], 0.2)
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": reordered,
                "declaration": declaration,
            })
        self.assertEqual(caught.exception.code, "ripple_invalid")

    def test_invalid_numbers_and_stale_confirmation_are_rejected(self):
        bad = [self._clip("a", 0.0, 0.4), self._clip("b", 0.4, "x")]
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": bad,
            })
        self.assertEqual(caught.exception.status, 422)
        stale = [self._clip("a", 0.0, 0.4), self._clip("b", 0.4, 0.8)]
        stale[1]["confirmation"]["end_s"] = 0.9
        with self.assertRaises(MasterAudioError) as caught:
            self.service.create_revision(PID, {
                "expected_revision": 1, "sections": stale,
            })
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_unconfirmed_save_is_a_draft_and_render_is_refused(self):
        sections = [self._clip("a", 0.0, 0.4, confirmed=False),
                    self._clip("b", 0.4, 0.8, confirmed=False)]
        declaration = self._declaration(
            [{"kind": "replace", "clip_id": "a", "delta_s": 0.0},
             {"kind": "replace", "clip_id": "b", "delta_s": 0.0}],
            ["a", "b"], 0.0)
        draft = self.service.create_revision(PID, {
            "expected_revision": 1, "sections": sections,
            "declaration": declaration,
        })
        self.assertFalse(draft["sections"][0]["confirmation"]["confirmed"])
        with self.assertRaises(MasterAudioError) as caught:
            self.service.render(PID, {"expected_revision": 2,
                                      "render_id": "render-one"})
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_ordinary_edit_with_a_declared_replace_still_works(self):
        sections = [self._clip("a", 0.0, 0.4, gain=-3.0, confirmed=False),
                    self._clip("b", 0.4, 0.8)]
        declaration = self._declaration(
            [{"kind": "replace", "clip_id": "a", "delta_s": 0.0}],
            ["a", "b"], 0.0)
        revision = self.service.create_revision(PID, {
            "expected_revision": 1, "sections": sections,
            "note": "gain edit", "declaration": declaration,
        })
        self.assertEqual(revision["revision"], 2)


if __name__ == "__main__":
    unittest.main()
