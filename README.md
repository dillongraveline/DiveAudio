<div align="center">

# DiveAudio

### High-fidelity binaural spatialisation for music — that shows you its own mechanism

[![License: MIT](https://img.shields.io/badge/license-MIT-2FE3D0.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![HRTF](https://img.shields.io/badge/HRTF-SADIE%20II%20KU100-6B4BFF)](https://www.york.ac.uk/sadie-project/database.html)
[![Separation](https://img.shields.io/badge/separation-Demucs-FF9F45)](https://github.com/adefossez/demucs)
[![Runs](https://img.shields.io/badge/runs-locally%20%C2%B7%20no%20cloud-1E2B44)](#quickstart)

<img src="docs/room.svg" width="100%" alt="The virtual room: four sources, expanding wavefronts, one moving element">

</div>

---

## Why this exists

Most "8D audio" takes a finished stereo mix, swings the **whole thing** around your
head on an LFO, and drowns it in reverb. It's striking for thirty seconds and
exhausting after two minutes — because your ears know a drum kit does not orbit
the listener.

DiveAudio moves almost nothing. The rhythm section stays welded in place, one
textural layer wanders, and vocals drift so slowly they take half a song to cross
a small arc. The 3D view shows you exactly which is which, live, driven by the
same maths that rendered the audio.

## The core idea

Demucs separates the track into stems — but the stems are **never re-summed** to
produce output. Re-summing bakes every separation artifact into everything you
hear. Instead the signal is split in two:

```
mover_dry = beta · mid( highpass( texture_stem ) )    # the only thing that moves
anchor    = original_master − mover_dry               # literally everything else
```

<img src="docs/signal-flow.svg" width="100%" alt="Signal flow: the anchor is the original minus one mono component">

`anchor` is your original file minus one mono component. Drums, bass, vocals, the
diffuse reverb field and the full bandwidth survive **untouched** — never
HRTF-filtered, never reconstructed by a model, never band-limited to Demucs'
44.1 kHz working rate. Every gram of separation error stays buried inside the
anchor, masked by the very signal it was subtracted from.

Only the **directional** (mid) component of the texture layer is convolved with
SADIE II KU100 HRTFs. The **diffuse** (side) component is left entirely alone.

> **This is the whole trick, and it was learned the hard way.** Two earlier
> designs rendered the full texture layer and came out measurably *narrower* than
> the source — because HRTF point-rendering an already-diffuse signal makes it
> **more** correlated, not less. Steer what is directional; preserve what is diffuse.

## Measured

96 kHz / 24-bit reference track, default preset:

| metric | source | output | |
|---|---:|---:|---|
| interaural correlation *(lower = wider)* | 0.794 | **0.562** | genuinely wider |
| IACC above 2 kHz | 0.562 | **0.036** | near-total decorrelation |
| directional motion above 2 kHz | 0.64 dB | **11.92 dB** | real movement |
| sub-200 Hz correlation *(must not change)* | 0.988 | 0.987 | bass provably anchored |
| >22 kHz content retained through separation | — | **99.87%** | full band survives |

The last row is the point of the subtraction design: Demucs works at 44.1 kHz,
yet the original's ultrasonic content is still there in the output.

## Quickstart

```bash
git clone https://github.com/dillongraveline/DiveAudio.git
cd DiveAudio
./setup.sh                     # venv, deps, three.js, SADIE HRTF database
./.venv/bin/python server.py   # binds 0.0.0.0:8765
```

Open `http://localhost:8765`, or your machine's LAN IP from a phone or tablet.
Point it at your music by editing `config.json`, or drag files onto the sidebar.

Everything runs on your machine. No account, no upload, no cloud.

## How motion works

Nothing follows a circle. The wandering source uses **sums of sines with
pairwise-incommensurate periods** (97 / 61 / 37 s in azimuth, 71 / 43 s in
elevation), so the path never repeats — yet it stays a pure function of time,
which means the renderer and the browser compute the identical trajectory
independently. RMS angular speed is ~7.8 °/s.

The anchored sources drift too, but by **constant-power amplitude panning, never
HRTF** — level only, so their timbre is untouched. Depth is low and the period is
tied to track length, so a source takes roughly half the song to traverse its
range.

**Bass never moves.** Low-frequency panning is inaudible and only muddies the
centre.

| | stem | behaviour |
|---|---|---|
| 🟣 | bass | fixed, dead centre |
| 🟠 | drums | drifts by amplitude pan, very slowly |
| ⚪ | vocals | drifts by amplitude pan, very slowly |
| 🟢 | other | wanders freely, HRTF-rendered |

Orb size is driven by **real per-stem RMS envelopes** computed at separation time,
not by frequency bands of the mix — so each orb pulses with its own instrument.

## CLI

```bash
./.venv/bin/python spatialize_cli.py track.flac
```

| flag | default | meaning |
|---|---|---|
| `--preset` | — | `subtle` · `natural` · `default` · `wide` |
| `--beta` | 0.92 | fraction of the texture layer that moves |
| `--orbit` | 43 | motion time-base, seconds |
| `--xover` | 200 | Hz below which nothing moves |
| `--stem` | other | which layer becomes the mover |
| `--model` | htdemucs | `htdemucs_ft` is better and ~4× slower |
| `--shifts` | 0 | prediction averaging; raises quality and cost |

## Cache correctness

Stems cache per input file, built so that a stem from one song appearing in
another's mix is **structurally impossible**:

- keys are a **SHA-256 of file content**, never path or mtime
- separation runs in a temp dir and is **atomically renamed** into place, so a
  killed job never leaves a half-written entry to be read later
- every entry carries a **manifest** (source hash, duration, filename); a cache
  hit requires it to match *this* file, with all four stems matching its duration
- a **length mismatch at point of use raises** rather than padding silently
- an `flock` per key serialises concurrent runs on the same file

First separation takes minutes; every render after it takes seconds.

## Formats

Decoding uses libsndfile, falling back to macOS `afconvert` — so WAV, FLAC, OGG,
MP3, M4A, AAC and ALAC all work with **no ffmpeg required**.

> MP3 container headers routinely overstate duration (encoder delay and padding —
> one test file claimed 346.9 s and decoded to 340.8 s). All duration checks use
> the decoded audio, never the header.

Output downloads as lossless FLAC, or AAC in M4A at roughly 1/6 the size.

## Known limitations

- **No authentication.** Binding `0.0.0.0` exposes your library to the LAN.
- **FLAC output is large** (~90 MB for 4 min at 96/24). Use the M4A download for phones.
- Motion parameters are not yet derived from tempo.
- Binaural output is **for headphones**; it partially collapses on speakers.
- `afconvert` decoding is macOS-only; other platforms are limited to what
  libsndfile handles.

## Credits

[SADIE II binaural database](https://www.york.ac.uk/sadie-project/database.html)
(Apache-2.0) — Neumann KU100 dummy head, 8802 measurements at 96 kHz.
Separation by [Demucs](https://github.com/adefossez/demucs) (MIT).
Rendering with [three.js](https://threejs.org) (MIT).

Licensed under the [MIT License](LICENSE).
