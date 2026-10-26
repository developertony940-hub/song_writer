# Save progress and return later

**Save progress / edit revision** accepts valid unfinished arrangements. Sections retain their actual listening-confirmation state. Saving is not approval and does not confirm anything automatically. Unconfirmed revisions display **DRAFT / REVIEW PENDING**. The current saved revision reopens when the same project is selected.

1. Click **Apply section adjustments** for any typed inspector changes.
2. Optionally enter a revision note.
3. Click **Save progress / edit revision**.
4. Wait for **Saved edit revision [number]** beside the button and the corresponding history entry.

Invalid ranges, stale sources, unsafe paths, invalid settings and revision conflicts still block saving. Save/export uncertainty still blocks repeated submissions pending inspection. Local feedback appears near both the save button and inspector, not just at the top. Closing/reloading warns about unsaved arrangements and unapplied inspector fields. This is explicit saving, not autosave.

Building a WAV/MP3 master remains gated by a saved, fully listening-confirmed revision. The backend checks this before creating an export claim. Older revision files are not migrated or rewritten. Saving later changes appends a revision and retains the previous one.

The combined PR also includes explicit ripple insertion. It never repairs, trims or reorders an existing saved arrangement automatically. In the user's screenshot, Verse 3a/3b follow the Outro; that order is preserved, not interpreted as permission to move them.

Before updating, verify the stopping-point revision appears in history. Preserve a local backup of the project folder if needed; GitHub sync is not a backup of project/audio data. Merge the PR, pull without force/reset, then use the controlled no-generation backend launch/reload procedure and refresh the editor. Do not use a launcher that enables generation or starts ComfyUI just to activate editing changes.

Verification: 76 Python tests (including seven real FFmpeg synthetic-audio tests), 56 JavaScript tests, and eight mocked-interface checks passed. Windows playback and this user's saved project were not directly tested or modified. No models, downloads, installs, service restarts or AI generation were used.
