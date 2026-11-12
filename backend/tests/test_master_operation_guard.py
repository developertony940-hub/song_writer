"""Explicit editor-operation contract for master revisions.

Covers insert, replace, reorder, remove, split, composite, stale base,
undeclared structural edits, unknown/wrong actions, no-op, label-only and
confirmation-invalidating drafts. Uses a synthetic nonmusical WAV and a fake
decoder; no ffmpeg, real project, model or audio processing.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.master_audio import MasterAudioError
from tests.test_master_audio import FakeDecodeService, PID, Store, make_wav, settings


class OperationContractTests(unittest.TestCase):
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
        self.first = self.service.create_revision(PID, {
            "expected_revision": 0,
            "sections": self.base_clips(),
            "note": "base",
        })

    def clip(self, clip_id, start_s, end_s, *, confirmed=True, label=None,
             gain_db=0.0, fade_in_s=0.0, fade_out_s=0.0):
        return {
            "clip_id": clip_id,
            "source_id": self.sid,
            "label": label if label is not None else clip_id,
            "start_s": start_s,
            "end_s": end_s,
            "gain_db": gain_db,
            "fade_in_s": fade_in_s,
            "fade_out_s": fade_out_s,
            "confirmation": {
                "confirmed": confirmed,
                "source_sha256": self.sid,
                "start_s": start_s,
                "end_s": end_s,
            },
        }

    def base_clips(self):
        return [
            self.clip("a", 0.0, 0.5),
            self.clip("b", 0.5, 1.0),
            self.clip("c", 1.0, 1.5),
        ]

    def declaration(self, actions, final_order, delta, *, base_revision=1,
                    base_recipe=None):
        return {
            "base_revision": base_revision,
            "base_recipe_sha256": base_recipe or self.first["recipe_sha256"],
            "delta_s": delta,
            "final_order": final_order,
            "actions": actions,
        }

    def save(self, sections, declaration=None, expected=1):
        body = {"expected_revision": expected, "sections": sections}
        if declaration is not None:
            body["declaration"] = declaration
        return self.service.create_revision(PID, body)

    def assert_code(self, code, sections, declaration=None, expected=1):
        with self.assertRaises(MasterAudioError) as raised:
            self.save(sections, declaration, expected)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_first_revision_has_no_declaration(self):
        self.assertEqual(1, self.first["revision"])
        self.assertIsNone(self.first["declaration"])

    def test_non_initial_revision_requires_declaration(self):
        self.assert_code("operation_required", self.base_clips())

    def test_insert_declaration_preserves_followers_and_adds_duration(self):
        sections = [
            self.clip("a", 0.0, 0.5),
            self.clip("x", 0.2, 0.7),
            self.clip("b", 0.5, 1.0),
            self.clip("c", 1.0, 1.5),
        ]
        declaration = self.declaration(
            [{"kind": "insert", "clip_id": "x", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.5}],
            ["a", "x", "b", "c"], 0.5)
        result = self.save(sections, declaration)
        self.assertEqual(2, result["revision"])
        self.assertAlmostEqual(2.0, result["duration_s"])
        self.assertEqual(["a", "x", "b", "c"], [c["clip_id"] for c in result["sections"]])
        self.assertEqual(declaration, result["declaration"])

    def test_replace_declaration_carries_explicit_delta(self):
        sections = [self.clip("a", 0.0, 0.5),
                    self.clip("b", 0.5, 0.9),
                    self.clip("c", 1.0, 1.5)]
        declaration = self.declaration(
            [{"kind": "replace", "clip_id": "b", "delta_s": -0.1}],
            ["a", "b", "c"], -0.1)
        result = self.save(sections, declaration)
        self.assertEqual(2, result["revision"])
        self.assertAlmostEqual(1.4, result["duration_s"])

    def test_label_only_edit_is_a_declared_replace(self):
        sections = [self.clip("a", 0.0, 0.5),
                    self.clip("b", 0.5, 1.0, confirmed=False, label="B label"),
                    self.clip("c", 1.0, 1.5)]
        declaration = self.declaration(
            [{"kind": "replace", "clip_id": "b", "delta_s": 0.0}],
            ["a", "b", "c"], 0.0)
        result = self.save(sections, declaration)
        self.assertEqual("B label", result["sections"][1]["label"])
        self.assertFalse(result["sections"][1]["confirmation"]["confirmed"])

    def test_confirmation_invalidating_edit_is_a_draft(self):
        sections = [self.clip("a", 0.0, 0.5),
                    self.clip("b", 0.5, 0.9, confirmed=False),
                    self.clip("c", 1.0, 1.5)]
        declaration = self.declaration(
            [{"kind": "replace", "clip_id": "b", "delta_s": -0.1}],
            ["a", "b", "c"], -0.1)
        result = self.save(sections, declaration)
        self.assertFalse(result["sections"][1]["confirmation"]["confirmed"])
        with self.assertRaises(MasterAudioError) as raised:
            self.service.render(PID, {"expected_revision": 2, "render_id": "draft"})
        self.assertEqual("confirmation_required", raised.exception.code)

    def test_reorder_declared_succeeds_and_undeclared_rejected(self):
        reordered = [self.clip("b", 0.5, 1.0),
                     self.clip("a", 0.0, 0.5),
                     self.clip("c", 1.0, 1.5)]
        self.assert_code("operation_required", reordered)
        declaration = self.declaration(
            [{"kind": "reorder", "clip_id": "b", "to_index": 0}],
            ["b", "a", "c"], 0.0)
        result = self.save(reordered, declaration)
        self.assertEqual(["b", "a", "c"], [c["clip_id"] for c in result["sections"]])
        self.assertAlmostEqual(1.5, result["duration_s"])

    def test_remove_declaration_reduces_duration(self):
        sections = [self.clip("a", 0.0, 0.5), self.clip("c", 1.0, 1.5)]
        declaration = self.declaration(
            [{"kind": "remove", "clip_id": "b", "delta_s": -0.5}],
            ["a", "c"], -0.5)
        result = self.save(sections, declaration)
        self.assertEqual(["a", "c"], [c["clip_id"] for c in result["sections"]])
        self.assertAlmostEqual(1.0, result["duration_s"])

    def test_split_declaration_preserves_total_duration(self):
        sections = [
            self.clip("a", 0.0, 0.5),
            self.clip("bL", 0.5, 0.75, confirmed=False, label="b"),
            self.clip("bR", 0.75, 1.0, confirmed=False, label="b"),
            self.clip("c", 1.0, 1.5),
        ]
        declaration = self.declaration(
            [{"kind": "split", "clip_id": "b", "at_s": 0.75,
              "left_clip_id": "bL", "right_clip_id": "bR"}],
            ["a", "bL", "bR", "c"], 0.0)
        result = self.save(sections, declaration)
        self.assertEqual(["a", "bL", "bR", "c"], [c["clip_id"] for c in result["sections"]])
        self.assertAlmostEqual(1.5, result["duration_s"])
        self.assertFalse(result["sections"][1]["confirmation"]["confirmed"])

    def test_composite_declaration_combines_replace_insert_and_reorder(self):
        sections = [
            self.clip("c", 1.0, 1.5),
            self.clip("a", 0.0, 0.5),
            self.clip("x", 0.2, 0.7, confirmed=False),
            self.clip("b", 0.5, 0.9, confirmed=False),
        ]
        declaration = self.declaration(
            [
                {"kind": "replace", "clip_id": "b", "delta_s": -0.1},
                {"kind": "insert", "clip_id": "x", "anchor_clip_id": "b",
                 "placement": "before", "delta_s": 0.5},
                {"kind": "reorder", "clip_id": "c", "to_index": 0},
            ],
            ["c", "a", "x", "b"], 0.4)
        result = self.save(sections, declaration)
        self.assertEqual(["c", "a", "x", "b"], [c["clip_id"] for c in result["sections"]])
        self.assertAlmostEqual(1.9, result["duration_s"])

    def test_stale_declaration_base_rejected(self):
        declaration = self.declaration(
            [{"kind": "replace", "clip_id": "a", "delta_s": 0.0}],
            ["a", "b", "c"], 0.0, base_revision=0)
        self.assert_code("revision_conflict", self.base_clips(), declaration)

    def test_unknown_kind_rejected(self):
        declaration = self.declaration(
            [{"kind": "explode", "clip_id": "a"}],
            ["a", "b", "c"], 0.0)
        self.assert_code("validation", self.base_clips(), declaration)

    def test_wrong_delta_rejected(self):
        sections = [
            self.clip("a", 0.0, 0.5),
            self.clip("x", 0.2, 0.7),
            self.clip("b", 0.5, 1.0),
            self.clip("c", 1.0, 1.5),
        ]
        declaration = self.declaration(
            [{"kind": "insert", "clip_id": "x", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.4}],
            ["a", "x", "b", "c"], 0.4)
        self.assert_code("ripple_invalid", sections, declaration)

    def test_wrong_order_rejected(self):
        reordered = [self.clip("b", 0.5, 1.0),
                     self.clip("a", 0.0, 0.5),
                     self.clip("c", 1.0, 1.5)]
        declaration = self.declaration(
            [{"kind": "reorder", "clip_id": "b", "to_index": 0}],
            ["a", "b", "c"], 0.0)
        self.assert_code("ripple_invalid", reordered, declaration)

    def test_wrong_anchor_rejected(self):
        sections = [
            self.clip("a", 0.0, 0.5),
            self.clip("b", 0.5, 1.0),
            self.clip("x", 0.2, 0.7),
            self.clip("c", 1.0, 1.5),
        ]
        declaration = self.declaration(
            [{"kind": "insert", "clip_id": "x", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.5}],
            ["a", "b", "x", "c"], 0.5)
        self.assert_code("ripple_invalid", sections, declaration)

    def test_undeclared_truncation_removal_and_reorder_rejected(self):
        truncated = [self.clip("a", 0.0, 0.5),
                     self.clip("b", 0.5, 0.8),
                     self.clip("c", 1.0, 1.5)]
        self.assert_code("operation_required", truncated)
        removed = [self.clip("a", 0.0, 0.5), self.clip("c", 1.0, 1.5)]
        self.assert_code("operation_required", removed)
        reordered = [self.clip("b", 0.5, 1.0),
                     self.clip("a", 0.0, 0.5),
                     self.clip("c", 1.0, 1.5)]
        self.assert_code("operation_required", reordered)

    def test_no_op_declaration_rejected(self):
        declaration = self.declaration([], ["a", "b", "c"], 0.0)
        self.assert_code("no_changes", self.base_clips(), declaration)

    def test_declaration_persists_and_reopens(self):
        sections = [
            self.clip("a", 0.0, 0.5),
            self.clip("x", 0.2, 0.7),
            self.clip("b", 0.5, 1.0),
            self.clip("c", 1.0, 1.5),
        ]
        declaration = self.declaration(
            [{"kind": "insert", "clip_id": "x", "anchor_clip_id": "b",
              "placement": "before", "delta_s": 0.5}],
            ["a", "x", "b", "c"], 0.5)
        self.save(sections, declaration)
        reopened = FakeDecodeService(Store(self.root), settings()).status(PID)
        self.assertEqual(declaration, reopened["revisions"][-1]["declaration"])
        self.assertEqual(2, reopened["revision"])


if __name__ == "__main__":
    unittest.main()
