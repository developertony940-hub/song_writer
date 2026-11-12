"""Approved Verse 3 text survives extraction, save and reopen.

The fixture is the exact 378-byte approved lyric text (SHA-256 eb42af6c...),
including the single internal stanza break after line four. No model, audio,
ffmpeg or real project is involved.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.services.operator_lyrics import extract_operator_lyrics, merge_operator_lyrics

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verse3-approved-lyrics.txt"
EXPECTED_SHA = "eb42af6cca06da22ff9a7b7e791297939628b57889901873e683946cd1db0d56"


class Verse3StanzaRoundTripTests(unittest.TestCase):
    def test_extract_save_reopen_preserves_exact_text_and_stanza_break(self):
        approved_bytes = FIXTURE.read_bytes()
        self.assertEqual(378, len(approved_bytes))
        self.assertEqual(EXPECTED_SHA, hashlib.sha256(approved_bytes).hexdigest())
        approved = approved_bytes.decode("utf-8")
        self.assertNotIn("\r", approved)
        self.assertNotIn("\ufffd", approved)

        extracted = extract_operator_lyrics("[Verse 3]\n" + approved)
        self.assertTrue(extracted.found)
        self.assertIn("Verse 3", extracted.tags)
        section = next(lines for tag, lines in extracted.section_order if tag == "Verse 3")
        self.assertEqual(9, len(section))
        self.assertEqual("", section[4])
        self.assertEqual(approved, "\n".join(section))
        self.assertIn("that old fiddle", approved)

        merged, replaced, added = merge_operator_lyrics(
            extracted, "[Verse 3]\nmodel placeholder")
        self.assertEqual(["Verse 3"], replaced)
        self.assertEqual([], added)
        self.assertIn(approved, merged)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "lyrics.txt"
            path.write_text(merged, encoding="utf-8")
            reopened = path.read_text(encoding="utf-8")
        self.assertIn(approved, reopened)
        re_extracted = extract_operator_lyrics(reopened)
        section = next(lines for tag, lines in re_extracted.section_order if tag == "Verse 3")
        self.assertEqual(9, len(section))
        self.assertEqual("", section[4])
        self.assertEqual(approved, "\n".join(section))
        merged_again, replaced_again, added_again = merge_operator_lyrics(
            re_extracted, "[Verse 3]\nmodel placeholder")
        self.assertEqual(["Verse 3"], replaced_again)
        self.assertEqual([], added_again)
        self.assertIn(approved, merged_again)


if __name__ == "__main__":
    unittest.main()
