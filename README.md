# DiveAudio

Turns ordinary stereo music into a high-fidelity binaural 3D mix, and shows you
the mechanism while it plays.

It is a deliberate opposite of YouTube "8D audio", which pans an entire finished
mix around your head and drowns it in reverb. Here, almost nothing moves.

## The idea

Demucs separates the track into stems, but the stems are **not** re-summed to
produce output — that would bake separation artifacts into everything you hear.
Instead the renderer splits the signal in two:

```
mover_dry = beta * mid(highpass(texture_stem))     # the only thing that moves
anchor    = original_master - mover_dry            # literally everything else
```

The anchor is your original file minus one mono component. Drums, bass, vocals,
the diffuse reverb field and the full bandwidth all survive untouched — never
HRTF-filtered, never reconstructed by a model, never band-limited by Demucs'
44.1 kHz working rate. Every gram of separation error stays buried inside the
anchor, masked by the original signal it was subtracted from.

Only the **directional** (mid) component of the texture layer is rendered
through SADIE II KU100 HRTFs and orbited. The **diffuse** (side) component is
left completely alone, because HRTF point-rendering an already-diffuse signal
makes it *more* correlated, not less — which is the opposite of envelopment.

That last point was learned the hard way; two earlier designs came out
measurably **narrower** than the source before this one worked.

## Measured

On a 96 kHz/24-bit reference track, "natural" preset:

| metric | source | output |
|---|---|---|
| interaural correlation (lower = wider) | 0.794 | **0.562** |
| IACC above 2 kHz | 0.562 | **0.036** |
| directional motion above 2 kHz | 0.64 dB | **11.92 dB** |
| sub-200 Hz correlation (must not change) | 0.988 | 0.987 |
| >22 kHz content retained through separation | — | **99.87%** |

Bass is provably anchored. The ultrasonic figure is the point of the subtraction
design: Demucs works at 44.1 kHz, yet the original's full bandwidth survives.

## Setup

```bash
./setup.sh                     # venv, deps, three.js, SADIE HRTF database
./.venv/bin/python server.py   # binds 0.0.0.0:8765
```

Open `http://localhost:8765`, or your machine's LAN IP from a phone or tablet.

## CLI

```bash
./.venv/bin/python spatialize_cli.py track.flac --preset natural
```

| flag | default | meaning |
|---|---|---|
| `--preset` | — | `subtle` / `natural` / `wide` |
| `--beta` | 0.85 | fraction of the texture layer that moves |
| `--orbit` | 50 | seconds per revolution |
| `--xover` | 200 | Hz below which nothing moves |
| `--stem` | other | which layer becomes the mover |
| `--model` | htdemucs | `htdemucs_ft` is better and ~4x slower |
| `--shifts` | 0 | prediction averaging; raises quality and cost |

Stems cache per input file (hash of path, size, mtime), so a track separates
exactly once. First pass is minutes; every render after is seconds.

## Known limitations

- **No authentication.** Binding `0.0.0.0` exposes your library to the LAN.
- **`.m4a` / AAC is unsupported** — libsndfile has no AAC decoder.
- **FLAC output is large** (~90 MB for 4 min at 96/24), which is rough over
  wifi to a phone. A compressed preview stream is the obvious next step.
- Elevation and orbit are currently fixed rather than derived from tempo.

## Credits

[SADIE II binaural database](https://www.york.ac.uk/sadie-project/database.html)
(Apache-2.0), Neumann KU100 dummy head, 8802 measurements at 96 kHz.
Separation by [Demucs](https://github.com/adefossez/demucs).
