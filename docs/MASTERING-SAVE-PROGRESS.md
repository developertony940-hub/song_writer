# Save progress and return later

**Save progress / edit revision** accepts valid unfinished arrangements. Every
revision after the first must declare its changes; the backend validates the
declaration against the saved base before writing the new immutable revision.
Sections retain their actual listening-confirmation state. Saving is not
approval and does not confirm anything automatically. Unconfirmed revisions
display **DRAFT / REVIEW PENDING**. The current saved revision reopens when the
same project is selected.

1. Click **Apply section adjustments** for any typed inspector changes.
2. Optionally enter a revision note.
3. Click **Save progress / edit revision**. The editor builds a declaration for
   the current edit; if the arrangement is unchanged, the save is refused with
   an actionable message instead of silently doing nothing.
4. Wait for **Saved edit revision [number]** beside the button and the
   corresponding history entry.

The declaration binds the saved base revision and recipe. It lists the editor
actions (insert, replace, reorder, remove, split, or a composite array), the
total duration delta and the final section order. The server applies the
actions to the saved base and requires the submitted section list to match
exactly; untouched clips must keep their source bounds, gain, fades, label and
confirmation hash. A stale base is a `revision_conflict` (409); a missing
declaration is `operation_required`; a wrong action, delta, order or anchor is
`ripple_invalid` (409).

Invalid ranges, stale sources, unsafe paths, invalid settings and revision
conflicts still block saving. Save/export uncertainty still blocks repeated
submissions pending inspection. Local feedback appears near both the save
button and inspector, not just at the top. Closing/reloading warns about unsaved
arrangements and unapplied inspector fields. This is explicit saving, not
autosave.

Building a WAV/MP3 master remains gated by a saved, fully listening-confirmed
revision. The backend checks this before creating an export claim. Older
revision files are not migrated or rewritten. Saving later changes appends a
revision and retains the previous one.

The editor's insert, apply-adjustments, move, remove and split actions all
produce declarations. Undo/redo, dirty-state warnings, confirmation gating and
the save/render gates remain in place. A save that combines several actions is
supported as one declaration; the server applies the actions in order and
checks the final order.

Before updating, verify the stopping-point revision appears in history.
Preserve a local backup of the project folder if needed; GitHub sync is not a
backup of project/audio data. Use the controlled no-generation backend
launch/reload procedure and refresh the editor. Do not use a launcher that
enables generation or starts ComfyUI just to activate editing changes.

Verification for this candidate is recorded outside this document; it is
offline unit/static testing only, not Windows browser acceptance, rendered PCM
or listening acceptance. No models, downloads, installs, service restarts or AI
generation were used.
