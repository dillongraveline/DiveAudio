<div align="center">

# DiveAudio

**High-fidelity binaural spatialisation for music — that shows you its own mechanism.**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://python.org)
[![HRTF](https://img.shields.io/badge/HRTF-SADIE%20II%20KU100-2FE3D0)](https://www.york.ac.uk/sadie-project/database.html)
[![Separation](https://img.shields.io/badge/separation-Demucs-6B4BFF)](https://github.com/adefossez/demucs)
[![Runs](https://img.shields.io/badge/runs-locally%20%C2%B7%20LAN-FF9F45)](#quickstart)

</div>

---

## What this is

Most "8D audio" takes a finished stereo mix, swings the *whole thing* around your
head on an LFO, and buries it in reverb. It sounds impressive for thirty seconds
and fatiguing after two minutes, because your ears know that a drum kit does not
orbit the listener.

DiveAudio moves almost nothing. The rhythm section stays welded in place while a
single directional layer drifts around it — and the 3D view shows you exactly
which is which, in real time, driven by the same maths that rendered the audio.

## The core idea

Demucs separates the track into stems, but the stems are **never re-summed** to
make the output. Re-summing bakes every separation artifact into everything you
hear. Instead the signal is split in two:

```
mover_dry = beta · mid( highpass( texture_stem ) )    # the only thing that moves
anchor    = original_master − mover_dry               # literally everything else
```

`anchor` is your original file minus one mono component. Drums, bass, vocals, the
diffuse reverb field and the full bandwidth survive **untouched** — never
HRTF-filtered, never reconstructed by a model, never band-limited to Demucs'
44.1 kHz working rate. Every gram of separation error stays buried inside the
anchor, masked by the very signal it was subtracted from.

Only the **directional** (mid) component of the texture layer is convolved with
SADIE II KU100 HRTFs and orbited. The **diffuse** (side) component is left
entirely alone.

> That last decision is the whole trick, and it was learned the hard way. Two
> earlier designs rendered the full texture layer and came out measurably
> **narrower** than the source — because HRTF point-rendering an already-diffuse
> signal makes it *more* correlated, not less. Steer what is directional;
> preserve what is diffuse.

## Measured

96 kHz / 24-bit reference track, `natural` preset:

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
Point it at your music by editing `config.json`, or just drag files onto the
sidebar.

## How it fits together

```
 browser  ┌───────────────────────────────────────────┐
          │  three.js scene   ◀── same az(t)/el(t) ──┐ │
          │  <audio> playback                        │ │
          └──────────────┬───────────────────────────┼─┘
                         │ HTTP                      │
          ┌──────────────▼───────────────────────────┼─┐
 server   │  FastAPI · single-flight render lock     │ │
          │  content-addressed stem cache            │ │
          └──────────────┬───────────────────────────┼─┘
                         │                           │
          ┌──────────────▼───────────────────────────┴─┐
 render   │  Demucs ─▶ mid/side split ─▶ HRTF convolve │
          └────────────────────────────────────────────┘
```

The orb you see **is** the position the audio is rendered at — not an animation
approximating it. Azimuth runs on a 50 s period and elevation on 37 s; because
those are non-commensurate, the path never closes and never repeats.

### Colour is meaning, not decoration

| | stem | behaviour |
|---|---|---|
| 🟣 | bass | large, low, centre — **never moves** |
| 🟠 | drums | front-centre, pulses on transients — stationary |
| ⚪ | vocals | dead centre — perfectly still |
| 🟢 | other | **the only orbiting object**, trails behind itself |

## CLI

```bash
./.venv/bin/python spatialize_cli.py track.flac --preset natural
```

| flag | default | meaning |
|---|---|---|
| `--preset` | — | `subtle` · `natural` · `wide` |
| `--beta` | 0.85 | fraction of the texture layer that moves |
| `--orbit` | 50 | seconds per revolution |
| `--xover` | 200 | Hz below which nothing moves |
| `--stem` | other | which layer becomes the mover |
| `--model` | htdemucs | `htdemucs_ft` is better and ~4× slower |
| `--shifts` | 0 | prediction averaging; raises quality and cost |

## Cache correctness

Stems are cached per input file, and the cache is built to make a stem from one
song appearing in another's mix **structurally impossible**:

- keys are a **SHA-256 of file content**, never path or mtime
- separation runs in a temp dir and is **atomically renamed** into place, so a
  killed job never leaves a half-written entry to be read later
- every entry carries a **manifest** (source hash, duration, filename); a cache
  hit requires it to match *this* file and all four stems to match its duration
- a **length mismatch at point of use raises** rather than padding silently
- an `flock` per key serialises concurrent runs on the same file

## Known limitations

- **No authentication.** Binding `0.0.0.0` exposes your library to the LAN.
- **`.m4a` / AAC unsupported** — libsndfile has no AAC decoder.
- **FLAC output is large** (~90 MB for 4 min at 96/24), which is rough over wifi
  to a phone. A compressed preview stream is the obvious next step.
- Orbit period and elevation are fixed, not yet derived from tempo.
- Binaural output is **for headphones**; it partially collapses on speakers.

## Credits

[SADIE II binaural database](https://www.york.ac.uk/sadie-project/database.html)
(Apache-2.0) — Neumann KU100 dummy head, 8802 measurements at 96 kHz.
Source separation by [Demucs](https://github.com/adefossez/demucs).
Rendering with [three.js](https://threejs.org).
