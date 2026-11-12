# Insert without replacing existing audio

The source editor's **Insert section** action creates a new clip. Choose
**Append at end**, **Insert before**, or **Insert after** a named arrangement
clip. Insertion extends the master by the complete selected source interval.
All existing clips keep their source bounds, gain, fades, identifiers and
review status. All following master positions move later. There is no overwrite
mode, fixed-length canvas, or cross-section overlap in this operation.

The save carries an explicit declaration. For a single insert, it names the
inserted clip, the anchor and placement, and the inserted length; the total
duration delta must equal that length. The declared final order must match the
submitted sections. The server applies the insert to the saved base and rejects
any undeclared reorder, removal, truncation or follower change.

To place Verse 3 immediately before the Final Chorus:

1. Preserve current work before refreshing or loading updates. Keep an unsaved
   tab open and capture its section source ranges/settings if saving is blocked.
   This change does not add draft autosave.
2. In the updated editor, select the source audio interval for the new verse and
   enter its name.
3. Choose **Insert before [number]. Final Chorus** as the insertion destination.
4. Click **Insert section**. Verify the reported old/new master duration and
   full added interval.
5. Review the new interval, confirm it only after listening, then save a new
   edit revision. The save declares the insert and its additive delta.

The inspector's **Apply section adjustments** edits the selected existing clip;
it is not insertion. Shortening an existing clip asks for explicit confirmation
and is declared as a replace with its exact length delta. A reused Verse 1 clip
remains Verse 1 audio even if renamed Verse 3; it is a placeholder, not a new
sung performance.

Other editor actions are also declared: **move** is a reorder, **remove** is a
removal with a negative delta, **split** replaces one clip with two adjacent
parts at the same total duration, and a save may combine several actions. The
server preserves every untouched clip and requires the declared actions to
produce the submitted order and duration.

This update does not repair earlier recipes automatically. To restore an
already shortened Final Chorus, select that clip and restore its own source
in/out interval using a trusted earlier saved recipe or listening-confirmed
original markers. Do not substitute its shifted master times into the source
fields. Review gain/fades and listening confirmation afterward. Undo may help
only while the relevant edit remains in this tab's undo history, and can undo
subsequent work too.

For illustration only: inserting a 45-second clip before a 61-second Final
Chorus shifts the entire chorus 45 seconds later, leaves it 61 seconds long, and
shifts the complete Outro too. No arrangement prose timestamps are treated as
measured source boundaries.

Verification for this candidate is offline unit/static testing only, not
Windows browser acceptance, rendered PCM or listening acceptance. No AI
submissions, model operations, dependency installs or service restarts are
involved.
