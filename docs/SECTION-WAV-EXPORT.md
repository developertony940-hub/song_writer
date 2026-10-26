# Export one song section as WAV

## What is implemented

In `/mastering`, import/select a source and use **Export just this piece**. Drag an interval on the waveform or enter exact Source In/Out seconds. If you have a source map, clicking a section's numbered button copies its source bounds and name into the selection. Audition the selection, then click **Export selection WAV**. Download the WAV and provenance JSON from the saved export history immediately below.

An arrangement section also has **Export source section WAV** in its inspector. This exports that section's original source interval, **not its gain/fades or master-timeline neighbors**. For the edited arrangement, continue using the existing saved-revision master export.

You do not need to save or confirm an entire arrangement merely to export a reference clip. Export does not confirm the section map, approve audio, approve lyrics, select a preferred take, or change a generation allowance. Suggested boundaries are not verified by exporting them.

## Exact meaning of the WAV

The imported source is already decoded by the existing mastering service to 48 kHz stereo PCM WAV. Section export copies a bounded interval of those PCM frames to a new WAV. The manifest records requested times, actual frame-aligned times, end-exclusive frame indices, source and decoded-PCM hashes, WAV hash and format. It does not claim recovery of quality lost during MP3 encoding or identity to the compressed MP3 bytes.

The result contains **both vocals and instruments**. It is not an isolated vocal, stem, cloned singer, or new performance. No gain/fades, silence padding, stretching, normalization or additional lossy encoding are applied. Each export has a unique immutable identity. Source audio, source maps, edit recipes, master renders and generation journals remain unchanged. Exports and execution/failure records stay inside the local project's mastering storage; do not commit them to GitHub.

Use the export-history downloads rather than renaming or editing internal project records. Never retry a request whose result is uncertain; reload the saved state and inspect its records first. Limits bound export count, cumulative duration and per-clip length. They are disk/processing safeguards, not AI-generation permissions.

## Singer-matching workflow: separate, not yet operational

1. Export a sung reference section from your own voice or an AI-generated singer you have permission to reuse.
2. A separately approved/configured local source-separation engine can attempt to isolate its vocal. Artifacts, backing-vocal overlap and instrumental leakage require listening review. Simple stereo subtraction is **not** a reliable lead-vocal extractor and is not substituted here.
3. Record a dry microphone-only Verse 3 guide, listening to Verse 1 in headphones. Preserve both guide and exact reference. Different lyrics may need different phrasing or extra bars.
4. A separately configured singing-voice conversion engine can attempt to convert the guide's identity toward the reference/model while retaining its performance. Some engines require voice-specific training; a short separated clip is not automatically a sufficient model. Speech-cloning capability does not establish singing capability.
5. Review the candidate, provide suitable accompaniment, align/mix, and import it as a separate source for a new master revision. Never overwrite the original or silently replace a section with whole-song generation.

No source-separation or singing-voice conversion runtime/model is bundled or configured in this repository. Consequently this update does not expose a misleading active Extract Voice or Generate Verse 3 control. Host inventory, model/license suitability, exact assets, setup permission and bounded execution approval are prerequisites for that next implementation stage. It does not install/download/load/train/run any model, restart services, call the cloud, or consume generation submissions.

## Deployment and acceptance

Delivered through the existing section-analysis pull request. Merge it before pulling `main`; an `Already up to date` message alone does not mean an open PR is installed. Preserve local configuration, MP3s, projects and private execution records. New backend routes require a separately authorized controlled backend reload; do not use a generation-enabling launcher merely to activate export. Hard-refresh `/mastering` after the updated backend is serving.

Offline synthetic tests verify frame-copy and API/helper contracts, not Windows integration, actual browser playback, real-song section accuracy, separated-vocal quality or singer similarity. On the host, verify a known short interval downloads, plays, has the expected start/end and leaves source/master history unchanged before relying on it.
