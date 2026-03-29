# Use Cases & Expansion Opportunities

`auto-rb-recorder` is a **process-triggered, silence-splitting audio recorder**.
Rekordbox is the default target, but the entire pipeline — process monitoring,
platform audio capture, PCM silence detection, WAV/MP3 export — is fully generic.
Only two things in the codebase are Rekordbox-specific: the default value of
`process_name` in `config.default.toml` and the binary name.

---

## Drop-in fits (change `process_name` only)

### Other DJ software

| Software | `process_name` |
|---|---|
| Serato DJ Pro | `Serato DJ Pro` |
| Traktor Pro | `Traktor Pro 3` |
| Virtual DJ | `VirtualDJ` |
| djay Pro | `djay Pro` |

Same use case as Rekordbox: boot the app, play a set, recordings appear
automatically split into segments by silence between tracks.

### DAW jam capture

| Software | `process_name` |
|---|---|
| Ableton Live | `Live` |
| Logic Pro | `Logic Pro X` |
| FL Studio | `FL64` |
| GarageBand | `GarageBand` |

Musicians frequently improvise without bothering to arm a track first.
Every distinct idea separated by silence becomes its own timestamped file.
Nothing is ever lost to "I forgot to hit record."

### Amp simulators & virtual instruments

| Software | `process_name` |
|---|---|
| BIAS FX 2 | `BIAS FX 2` |
| Neural DSP plugins (standalone) | varies by plugin |
| Amplitube | `AmpliTube 5` |
| Pianoteq | `Pianoteq 8` |
| Kontakt (standalone) | `Kontakt` |

Practice sessions are captured automatically, split by natural pauses,
building a chronological archive of improvement over time.

---

## Strong fits with minor additions

### Meeting recorder

Monitor `zoom`, `Microsoft Teams`, `Discord`, or `Google Chrome` (for Meet).
Auto-record when the app opens; silence-splitting creates per-topic clips
that naturally segment a meeting into distinct conversation threads.

> **Note:** Recording calls requires all-party consent in most jurisdictions.
> This use case is best suited for recording your own side of the call, or
> for single-participant review sessions.

### Podcast production safety net

Monitor the host DAW (Reaper, Hindenburg, Ardour). The recorder runs as a
parallel safety capture — independent of the DAW's own recording — providing
a fallback if the session file is corrupted or accidentally closed.
Silence-splitting mirrors the per-take structure of the DAW session.

### Lecture / seminar capture

Monitor a video conferencing or presentation app. Every lecture is
automatically segmented into Q&A blocks, topic shifts, and breaks.
Pairs naturally with a post-processing transcription step.

---

## Structural expansions the codebase is already ready for

### Multi-profile / multi-process support

The config dataclass and `ProcessMonitor` already treat `process_name` as
a runtime parameter. A small extension would let users define named profiles
("Rekordbox DJ set", "Ableton jam", "Zoom meeting") selectable from the
menu bar without restarting.

Approximate scope: add a `profiles: list[Config]` field to the top-level
config, a profile-picker submenu, and swap the active config in `DaemonBridge`.

### Post-processing pipeline

The `on_segment_saved(path, duration)` callback added for the GUI is the
exact hook needed for arbitrary post-processing. After each segment is saved:

- **Transcription**: pipe to [Whisper](https://github.com/openai/whisper) (local) or a cloud STT API — meeting and lecture recordings become searchable text automatically.
- **Music identification**: submit to AcoustID / MusicBrainz to auto-tag recorded DJ sets with track metadata.
- **Upload / sync**: push to S3, Dropbox, or a NAS immediately after conversion.
- **BPM / key analysis**: run Essentia or librosa on the segment for DJ metadata.

None of these require architectural changes — they register as `on_segment_saved` listeners.

### Output routing by process

Route recordings to different folders depending on which app triggered them:

```toml
[[profiles]]
process_name = "rekordbox"
output_dir = "~/Music/DJ Sets"

[[profiles]]
process_name = "zoom"
output_dir = "~/Meetings"

[[profiles]]
process_name = "Live"
output_dir = "~/Sessions/Ableton"
```

### System-audio / virtual device monitoring

Instead of targeting a specific process, monitor a virtual audio device
(BlackHole, VB-Cable, Loopback) to capture all system audio during a
defined time window — useful for radio monitoring or scheduled capture.
`AudioCapture` already accepts an injectable `CaptureBackend`; a
`VirtualDeviceBackend` would be a clean addition alongside the existing
`AudioteeCaptureBackend` and `WindowsCaptureBackend`.

---

## Positioning summary

The project is one config field and a rename away from being a general-purpose
"set and forget" audio recorder for any audio application on macOS or Windows.
The silence-based auto-segmentation is the key differentiator — it removes the
need for manual session management, which is the main reason musicians and DJs
miss recordings in practice.
