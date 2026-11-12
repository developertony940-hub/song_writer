# V3 golden-set claim correction (candidate scope)

Date: 2026-11-12
Status: candidate documentation only. The historical report is NOT edited.

## The historical claim (left unchanged)

`recommendations_validation.html` line 450 states that "All twelve
recommendations and the three outstanding items (V3 golden set, V7 routing, R8
boundary) are implemented and verified by 126 offline tests". The V3 row then
says "agents/golden_set.py, agents/golden/ - two real briefs with recorded
hashes; both verify and meet their expectations".

## What the source actually verifies

- `agents/golden_set.py::verify_set()` checks only that each brief file is
  present and that its SHA-256 matches `golden/index.json`.
- `agents/golden_set.py::replay_one()` checks only
  `extract_operator_lyrics(text).found == bool(entry["expect_operator_lyrics"])`.
- There is no check for musical analysis, melody, harmony, rhythm, vocal
  identity, audio, or singer continuity anywhere in `golden_set.py`.

## Corrected wording for future reports

> V3 (golden set) covers **structural presence and operator-lyric extraction
> only**. It does not verify musical analysis, melody, rhythm, vocal identity
> or audio. V7 (routing) is separately verified: the five `/agents/*` routes
> exist in `backend/app/routers/music.py` at lines 149, 175, 187, 198 and 218.

## Scope discipline

- Do not edit `recommendations_validation.html` or `recommendation.html`; they
  are historical evidence.
- Use the corrected wording above in any new readiness or release note.
- This correction does not change any test, code path or project state.
