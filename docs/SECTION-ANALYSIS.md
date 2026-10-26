# Local section suggestions in the mastering workspace

## Use

1. Open `/mastering`, choose the intended project, and import or select an audio source.
2. **Analyze automatically after import** is enabled initially. It runs only when that source has no existing saved map or analysis. For already imported audio, select **Analyze & suggest sections**.
3. Optionally provide ordered section names before analysis, or **Copy labels from a saved lyric revision**. Choose the revision explicitly. Repeated bracketed lyric tags remain separate occurrences. No lyric text, arrangement timestamp, approval, submission cap, or music setting is changed.
4. Review the suggested source map. The analysis is not transcription and cannot establish that each lyric was sung. Section names are tentative hints, not recognized verses or choruses. A recording need not match the intended structure.
5. Use **Section** to audition a complete section and **Join** to hear the transition into it. Edit names and exact source seconds, drag the gold waveform boundaries, or split/merge sections. Moving a shared boundary updates both neighbors and invalidates both confirmations. Sections always cover the complete source, in order, without gaps or overlaps.
6. Check **Listened / confirmed** only after a complete section audition. Interrupted playback does not unlock a new confirmation. This is your attestation, not a machine quality verdict. Saved historical confirmations can be reused for unchanged source intervals.
7. **Save source map** appends a separate source-bound map version. Unconfirmed suggestions can be saved without being promoted to confirmed. Older maps remain available; loading one creates a draft for a new map version, not an overwrite.
8. **Use map in master arrangement** explicitly replaces the unsaved arrangement draft after confirmation. The map uses source times, not edited master positions. Unconfirmed sections remain unconfirmed, and the usual master-revision/export checks still apply. Nothing exports or generates automatically.

## Boundaries and privacy

Analysis uses the already decoded immutable PCM snapshot and standard-library CPU processing. No new packages, downloaded models, cloud APIs, transcription, voice conversion, synthesis, AI generation, model loading, or GPU capacity are involved. Source MP3 bytes are never modified. Map persistence is separate from music approvals/accounting and master recipes. The analyzer is a bounded acoustic heuristic: quiet transitions and energy changes can be false boundaries, while seamless musical changes may be missed. A single suggested section is a valid low-information result; arbitrary evenly spaced verses are not invented.

An analysis and each map retain the raw source SHA-256 and decoded PCM SHA-256. Existing path confinement, source-integrity verification and project locking apply. Maps append with an optimistic expected revision. A save whose outcome is uncertain must not be blindly repeated: reload state and inspect saved map history first. Read failures and active-operation conflicts are displayed, never automatically retried.

## Deployment

Merge the reviewed GitHub change, then fast-forward the local checkout without forcing or resetting local work. New backend routes require a separately approved controlled backend reload; a browser refresh alone cannot load Python code into an already running process. Do not use the generation-enabling `run_music.ps1` launcher just to activate this feature. Hard-refresh the mastering page after the backend is safely running the updated source. No Windows runtime or real-song boundary-quality acceptance is implied by synthetic tests.

## Singing voice work is separate

This feature does not clone a singer, invent missing words, create a melody, or replace a sung verse. Those require separate capability, rights/consent, resource and generation authorization decisions. A saved section map provides source-bound intervals for later review; it does not authorize any synthesis.
