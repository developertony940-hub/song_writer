# Master edit declaration contract

Every revision after the first must carry a `declaration` in
`POST /api/projects/{pid}/master-audio/revisions`. The declaration is the
single server-enforced record of what changed. A bare snapshot with no
declaration is rejected (`operation_required`, 409); a declaration that does not
reproduce the submitted sections is rejected (`ripple_invalid`, 409).

## Envelope

```json
{
  "expected_revision": 1,
  "sections": [ ...normalized section objects... ],
  "note": "optional",
  "declaration": {
    "base_revision": 1,
    "base_recipe_sha256": "fd50600c...",
    "delta_s": 0.5,
    "final_order": ["intro", "verse3", "final-chorus"],
    "actions": [
      {
        "kind": "insert",
        "clip_id": "verse3",
        "placement": "before",
        "anchor_clip_id": "final-chorus",
        "delta_s": 0.5
      }
    ]
  }
}
```

`base_revision` and `base_recipe_sha256` must match the current saved
revision; otherwise the save is `revision_conflict` (409). `delta_s` is the
total master length change. `final_order` is the submitted clip order.

## Actions

| Kind | Fields | Length change |
|---|---|---|
| `insert` | `clip_id`, `placement` (`before`/`after`/`append`), `anchor_clip_id` (except append), `delta_s` | inserted clip length |
| `replace` | `clip_id`, `delta_s` | new length minus old length |
| `reorder` | `clip_id`, `to_index` | 0 |
| `remove` | `clip_id`, `delta_s` | negative removed length |
| `split` | `clip_id`, `at_s`, `left_clip_id`, `right_clip_id` | 0 |

The server applies the actions in array order to the saved base, using the
submitted section objects for inserted/replaced/split clips. It then requires:

- the working list to equal the submitted section order;
- the submitted `final_order` to equal that order;
- every untouched clip to keep its source, bounds, frame range, gain, fades,
  label and confirmed clip hash;
- the sum of action deltas to equal the envelope `delta_s`;
- the submitted master duration to equal the base duration plus `delta_s`;
- inserts to add exactly their clip length, removes to subtract exactly their
  clip length, replaces to carry their exact length delta, and splits to keep
  the old total length with adjacent same-source parts.

A composite save is one declaration with several actions. The UI builds it
from the saved revision and the current arrangement; it returns no declaration
for a no-op, and the editor refuses that save visibly. A declaration with an
empty `actions` array is rejected (`no_changes`, 409).

## Errors

| Code | Meaning |
|---|---|
| `operation_required` | non-initial revision has no declaration |
| `no_changes` | declaration has no actions |
| `revision_conflict` | stale base revision or recipe |
| `ripple_invalid` | wrong anchor/order/delta, altered untouched clip, or invalid action |
| `validation` | malformed values or unknown action kind |

The first revision carries no declaration. Draft saves still work: a replace
that invalidates a listening confirmation is accepted as a draft and remains
blocked from master export until the sections are confirmed.

## UI behavior

The editor's insert, apply-adjustments (trim/gain/fades/label), move, remove and
split actions all produce declarations. The declaration builder is
`buildDeclaration` in `frontend/static/mastering-core.mjs`. Undo/redo, dirty
state, confirmation gating and the save/render gates are unchanged. A no-op
save shows an actionable message and sends no request; no editor action
silently no-ops.

Verification for this candidate is offline unit/static testing only. It is not
Windows browser acceptance, rendered PCM or listening acceptance. No model,
GPU, audio or generation work is involved.
