# Insert without replacing existing audio

The source editor's **Insert section** action creates a new clip. Choose **Append at end**, **Insert before**, or **Insert after** a named arrangement clip. Insertion extends the master by the complete selected source interval. All existing clips keep their source bounds, gain, fades, identifiers and review status. All following master positions move later. There is no overwrite mode, fixed-length canvas, or cross-section overlap in this operation.

To place Verse 3 immediately before the Final Chorus:

1. Preserve current work before refreshing or loading updates. The current save restriction requires reviewed boundaries; do not falsely confirm unfinished edits. Keep an unsaved tab open and capture its section source ranges/settings if saving is blocked. This change does not add draft autosave.
2. In the updated editor, select the source audio interval for the new verse and enter its name.
3. Choose **Insert before [number]. Final Chorus** as the insertion destination.
4. Click **Insert section**. Verify the reported old/new master duration and full added interval.
5. Review the new interval, confirm it only after listening, then save a new edit revision.

The inspector's **Apply section adjustments** edits the selected existing clip; it is not insertion. Shortening an existing clip now asks for explicit confirmation. A reused Verse 1 clip remains Verse 1 audio even if renamed Verse 3; it is a placeholder, not a new sung performance.

This update does not repair earlier recipes automatically. To restore an already shortened Final Chorus, select that clip and restore its own source in/out interval using a trusted earlier saved recipe or listening-confirmed original markers. Do not substitute its shifted master times into the source fields. Review gain/fades and listening confirmation afterward. Undo may help only while the relevant edit remains in this tab's undo history, and can undo subsequent work too.

For illustration only: inserting a 45-second clip before a 61-second Final Chorus shifts the entire chorus 45 seconds later, leaves it 61 seconds long, and shifts the complete Outro too. No arrangement prose timestamps are treated as measured source boundaries.

Verification: 14 JavaScript tests passed, including exact following-clip preservation, additive duration, before/after/append, invalid anchors, recipe serialization, undo/redo, and UI wiring checks. These are offline unit/static checks, not Windows browser, rendered PCM, or listening acceptance. No AI submissions, model operations, dependency installs or service restarts are involved.
