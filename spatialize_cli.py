#!/usr/bin/env python
"""Binaural 3D-ify any track, preserving fidelity.

  usage:  spatialize_cli.py <audio-file> [--beta 0.85] [--orbit 50] [--xover 200]

Moves only the DIRECTIONAL component of the non-percussive layer; the diffuse
field, the bass and the drums are never HRTF-filtered and never reconstructed
from stems, so the original master survives underneath.

Progress is reported on stdout as machine-readable lines (see progress.py)
interleaved with ordinary human output, so the server can show what is actually
being processed instead of guessing at a duration.
"""
import sys, os, json, subprocess, argparse, hashlib, fcntl, shutil, uuid, re, time
import numpy as np, soundfile as sf, sofar
from scipy.signal import resample_poly, butter, sosfiltfilt
from math import gcd

import dsp, progress

HERE = os.path.dirname(os.path.abspath(__file__))
SOFA = {44100: 'D1_44K_16bit_256tap_FIR_SOFA.sofa',
        48000: 'D1_48K_24bit_256tap_FIR_SOFA.sofa',
        96000: 'D1_96K_24bit_512tap_FIR_SOFA.sofa'}

p = argparse.ArgumentParser()
p.add_argument('infile'); p.add_argument('--beta', type=float, default=0.92)
p.add_argument('--orbit', type=float, default=43.0); p.add_argument('--xover', type=float, default=200.0)
p.add_argument('--elev', type=float, default=25.0); p.add_argument('--ramp', type=float, default=20.0)
p.add_argument('--stem', default='other', choices=['other','vocals','drums','bass'])
p.add_argument('--out', default=None)
p.add_argument('--preset', default=None,
               choices=['subtle','natural','default','wide','stage'])
p.add_argument('--vocal-beta', dest='vocal_beta', type=float, default=0.0,
               help='fraction of the vocal mid to HRTF-render; 0 disables (default)')
p.add_argument('--vocal-arc', dest='vocal_arc', type=float, default=55.0,
               help='half-width in degrees of the stage the vocal walks')
p.add_argument('--vocal-elev', dest='vocal_elev', type=float, default=9.0)
p.add_argument('--model', default='htdemucs')
p.add_argument('--shifts', type=int, default=0)
p.add_argument('--batch', type=int, default=None,
               help='HRTF blocks per batched transform (default: auto from memory budget)')
p.add_argument('--json', action='store_true', help='print the result metadata as JSON')
a = p.parse_args()
# 'stage' is the only preset that HRTF-renders a second stem. Every other
# preset is byte-for-byte what it always was.
PRESETS = {'subtle':  {'beta': 0.60, 'orbit': 72.0},
           'natural': {'beta': 0.85, 'orbit': 50.0},
           'default': {'beta': 0.92, 'orbit': 43.0},
           'wide':    {'beta': 1.00, 'orbit': 36.0},
           'stage':   {'beta': 0.92, 'orbit': 43.0, 'vocal_beta': 0.50}}
if a.preset:
    for _k, _v in PRESETS[a.preset].items():
        setattr(a, _k, _v)
VOCAL_HRTF = a.vocal_beta > 0.0

emit = progress.emit

# Weights are wall-clock shares measured on a 9:38 track at 96 kHz, so the ring
# tracks reality rather than counting stages evenly.
COLD_PLAN = [{"id": "decode", "weight": 1}, {"id": "separate", "weight": 85},
             {"id": "envelopes", "weight": 4}, {"id": "mix", "weight": 5},
             {"id": "convolve", "weight": 2}, {"id": "encode", "weight": 3}]
WARM_PLAN = [{"id": "decode", "weight": 10}, {"id": "envelopes", "weight": 25},
             {"id": "mix", "weight": 40}, {"id": "convolve", "weight": 15},
             {"id": "encode", "weight": 20}]


def load_audio(path):
    """Decode anything we can. soundfile covers wav/flac/ogg/mp3; macOS
    afconvert covers m4a/aac/alac without needing ffmpeg installed."""
    try:
        return sf.read(path, dtype='float64', always_2d=True)
    except Exception as first:
        conv = '/usr/bin/afconvert'
        if not os.path.exists(conv):
            raise RuntimeError("cannot decode %s (%s)" % (os.path.basename(path), first))
        emit('decode', frac=0.1, note='transcoding via afconvert')
        tmp = os.path.join(HERE, '_decode_%s.wav' % uuid.uuid4().hex[:8])
        try:
            subprocess.run([conv, '-f', 'WAVE', '-d', 'LEF32', path, tmp],
                           check=True, capture_output=True)
            return sf.read(tmp, dtype='float64', always_2d=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError("cannot decode %s: %s"
                               % (os.path.basename(path), (e.stderr or b'').decode()[:200]))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# ---------------------------------------------------------------- decode ----
emit('decode', frac=0.0, file=os.path.basename(a.infile),
     mb=round(os.path.getsize(a.infile) / 1e6, 1))
orig, sr0 = load_audio(a.infile)
if orig.shape[1] == 1: orig = np.repeat(orig, 2, axis=1)
SR = sr0 if sr0 in SOFA else 96000
print("source : %d Hz %dch %.1fs -> working at %d Hz"
      % (sr0, orig.shape[1], len(orig) / sr0, SR), flush=True)
emit('decode', frac=0.55, src_sr=sr0, work_sr=SR, channels=orig.shape[1],
     seconds=round(len(orig) / sr0, 1), resampling=(sr0 != SR))
if sr0 != SR:
    g = gcd(SR, sr0); orig = resample_poly(orig, SR//g, sr0//g, axis=0)
n = len(orig)

STEMS = ('drums', 'bass', 'other', 'vocals')

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

# Key the cache on the file's CONTENT, never its path or mtime, so two different
# songs can never collide and a re-encoded file never reuses stale stems.
emit('decode', frac=0.8, note='hashing source')
SRC_SHA, SRC_SECONDS = _sha256(a.infile), n / SR
key = SRC_SHA[:24] + '_' + a.model + '_s' + str(a.shifts)
emit('decode', frac=1.0, sha=SRC_SHA[:12], seconds=round(SRC_SECONDS, 1),
     src_sr=sr0, work_sr=SR)

stems_root = os.path.join(HERE, 'stems')
os.makedirs(stems_root, exist_ok=True)
cache_dir = os.path.join(stems_root, key)
stem_wav  = os.path.join(cache_dir, a.model, a.stem + '.wav')

def _cache_ok():
    """A cache entry counts only if its manifest proves it came from THIS file."""
    try:
        m = json.load(open(os.path.join(cache_dir, 'manifest.json')))
    except Exception:
        return False
    if m.get('src_sha256') != SRC_SHA: return False
    if m.get('model') != a.model or m.get('shifts') != a.shifts: return False
    for s in STEMS:
        sp = os.path.join(cache_dir, a.model, s + '.wav')
        try:
            si = sf.info(sp)
        except Exception:
            return False
        if abs(si.frames / si.samplerate - SRC_SECONDS) > 0.25:
            return False
    return True


_BAR = re.compile(rb'(\d+(?:\.\d+)?)%')

def _run_demucs(cmd, cwd):
    """Run demucs, translating its progress bar into structured progress.

    demucs draws a tqdm bar on stderr using carriage returns, so this reads raw
    bytes and splits on \\r as well as \\n rather than iterating lines.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    buf, last, tail = b'', -1.0, []
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb'[\r\n]', buf)
        buf = parts.pop()
        for raw in parts:
            text = raw.decode('utf-8', 'replace').strip()
            if not text:
                continue
            tail.append(text); del tail[:-20]
            m = _BAR.search(raw)
            if m:
                pct = float(m.group(1)) / 100.0
                if pct - last >= 0.01:
                    last = pct
                    emit('separate', frac=pct, model=a.model, device='mps',
                         shifts=a.shifts, percent=round(pct * 100, 1))
            elif not text.startswith('|'):
                print(text, flush=True)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError('demucs failed (exit %d): %s'
                           % (proc.returncode, ' / '.join(tail[-5:])))


# Exclusive lock per cache key: concurrent runs on the same file serialise, and
# the lock is released by the OS even if the process is killed.
with open(os.path.join(stems_root, key + '.lock'), 'w') as _lf:
    fcntl.flock(_lf, fcntl.LOCK_EX)
    cached = _cache_ok()
    env_path = os.path.join(cache_dir, 'envelopes.json')
    have_env = cached and os.path.exists(env_path)

    plan = [s for s in (WARM_PLAN if cached else COLD_PLAN)
            if not (s["id"] == "envelopes" and have_env)]
    emit('plan', stages=plan, cached=cached, key=key[:12])

    if cached:
        print("stems cached (" + key[:12] + ") - skipping separation", flush=True)
    else:
        print("separating (" + a.model + ", shifts=" + str(a.shifts) + ") -> " + key[:12], flush=True)
        emit('separate', frac=0.0, model=a.model, device='mps', shifts=a.shifts,
             stems=len(STEMS), note='running demucs (' + a.model + ') on mps')
        shutil.rmtree(cache_dir, ignore_errors=True)
        tmp_dir = cache_dir + '.tmp.' + uuid.uuid4().hex[:8]
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            # demucs decodes via ffmpeg, which may be absent; soundfile handles
            # wav/flac/ogg/mp3 directly, so feed demucs a plain WAV instead.
            feed = os.path.join(tmp_dir, '_input.wav')
            sf.write(feed, orig.astype('float32'), SR, subtype='FLOAT')
            _run_demucs([sys.executable, '-m', 'demucs', '-n', a.model, '-d', 'mps',
                         '--shifts', str(a.shifts), '-o', tmp_dir,
                         '--filename', '{stem}.wav', feed], HERE)
            os.remove(feed)
            # Validate BEFORE publishing: a stem from another song would not
            # match this source's duration.
            for s in STEMS:
                si = sf.info(os.path.join(tmp_dir, a.model, s + '.wav'))
                if abs(si.frames / si.samplerate - SRC_SECONDS) > 0.25:
                    raise RuntimeError('stem %s is %.2fs, source is %.2fs' % (s, si.frames / si.samplerate, SRC_SECONDS))
            json.dump({'src_sha256': SRC_SHA, 'src_seconds': SRC_SECONDS,
                       'src_name': os.path.basename(a.infile),
                       'model': a.model, 'shifts': a.shifts, 'stems': list(STEMS)},
                      open(os.path.join(tmp_dir, 'manifest.json'), 'w'), indent=2)
            # Atomic publish: the cache dir appears complete or not at all.
            os.rename(tmp_dir, cache_dir)
            emit('separate', frac=1.0, model=a.model, stems=len(STEMS))
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # Per-stem RMS envelopes for the visualiser: computed once per cache entry,
    # normalised against a shared peak so relative loudness between stems is
    # preserved, then quantised to uint8 to keep the payload small. Read in
    # hop-aligned chunks so a long stem never lands in memory whole.
    if not os.path.exists(env_path):
        ENV_HZ = 20
        raw = {}
        for _i, _s in enumerate(STEMS):
            emit('envelopes', frac=_i / len(STEMS), stem=_s,
                 index=_i + 1, stems=len(STEMS), rate=ENV_HZ)
            raw[_s], _hop, _nf = dsp.rms_envelope(
                os.path.join(cache_dir, a.model, _s + '.wav'), env_hz=ENV_HZ)
        env, _peak = dsp.quantise_envelopes(raw)
        _tmp = env_path + '.tmp'
        with open(_tmp, 'w') as _fh:
            json.dump({'rate': ENV_HZ, 'peak': _peak, 'stems': env}, _fh)
        os.replace(_tmp, env_path)
        emit('envelopes', frac=1.0, stems=len(STEMS), rate=ENV_HZ,
             frames=len(next(iter(env.values()), [])))

# ------------------------------------------------------------------- mix ----
emit('mix', frac=0.0, stem=a.stem, note='building anchor and mover')
other, sro = sf.read(stem_wav, dtype='float64', always_2d=True)
if sro != SR:
    g = gcd(SR, sro); other = resample_poly(other, SR//g, sro//g, axis=0)
if abs(len(other) / SR - n / SR) > 0.25:
    raise RuntimeError("stem/source length mismatch (%.2fs vs %.2fs) - refusing to mix"
                       % (len(other) / SR, n / SR))
other = other[:n] if len(other) >= n else np.pad(other, ((0, n-len(other)), (0,0)))

emit('mix', frac=0.25, xover=a.xover, beta=a.beta,
     note='crossover %.0f Hz, beta %.2f' % (a.xover, a.beta))
sos = butter(4, a.xover, 'low', fs=SR, output='sos')
M   = dsp.mid_highpass(other, sos)                    # directional part only
del other                                             # ~200 MB on a long track
beta = a.beta * np.clip(np.arange(n)/SR/a.ramp, 0, 1)
mover_dry = beta * M
anchor = orig - np.stack([mover_dry, mover_dry], 1)   # diffuse side-signal stays in here

def iacc(x):
    L,R = x[:,0]-x[:,0].mean(), x[:,1]-x[:,1].mean()
    return float(np.dot(L,R)/(np.linalg.norm(L)*np.linalg.norm(R)))

# Measure the source now and release it. Holding the original master alive just
# to compute two numbers at the end cost ~400 MB on a ten-minute track, and the
# stage preset needs that headroom for a second mover.
lp = butter(4, a.xover, 'low', fs=SR, output='sos')
IACC_ORIG = iacc(orig)
BASS_IACC_ORIG = iacc(sosfiltfilt(lp, orig, axis=0))
# Level of the source, so the player can match modes without re-rendering.
_ls, _lf = dsp.loudness_window(len(orig), SR)
AW_SRC = dsp.a_weighted_db(orig[_ls:_ls + _lf], SR)
del orig

# ---- very slow drift for the otherwise-anchored elements -------------------
# These use constant-power AMPLITUDE panning, never HRTF: level only, so their
# timbre is untouched. Only the directional (mid) component above the crossover
# is moved, at low depth, and the period is tied to the track length so a source
# takes roughly half the song to cross its (small) range. Bass never moves --
# low-frequency panning is inaudible and only muddies the centre.
SUBTLE = {'drums': (0.20, 0.34, 1.7), 'vocals': (0.18, 0.28, 4.0)}
if VOCAL_HRTF:
    # A stem is steered one way or the other, never both.
    SUBTLE.pop('vocals')
_tt   = np.arange(n) / SR
_dur  = max(n / SR, 1.0)
_ramp = np.clip(_tt / a.ramp, 0, 1)
drift_info = {}
for _di, (_name, (_bk, _span, _ph)) in enumerate(SUBTLE.items()):
    emit('mix', frac=0.45 + 0.25 * _di, drifting=_name,
         note='drift pan: ' + _name)
    _sp = os.path.join(cache_dir, a.model, _name + '.wav')
    try:
        _d, _dsr = sf.read(_sp, dtype='float64', always_2d=True)
    except Exception:
        continue
    if _dsr != SR:
        _g = gcd(SR, _dsr); _d = resample_poly(_d, SR // _g, _dsr // _g, axis=0)
    _d = _d[:n] if len(_d) >= n else np.pad(_d, ((0, n - len(_d)), (0, 0)))
    _Mk = dsp.mid_highpass(_d, sos) * _bk * _ramp
    _pan = _span * np.sin(2 * np.pi * _tt / (_dur * 1.7) + _ph)
    _ang = (_pan + 1.0) * (np.pi / 4.0)
    anchor -= np.stack([_Mk, _Mk], 1)
    anchor += np.stack([_Mk * np.cos(_ang), _Mk * np.sin(_ang)], 1) * np.sqrt(2.0)
    drift_info[_name] = {'depth': _bk, 'span': _span, 'phase': _ph, 'period': _dur * 1.7}
    del _d, _Mk
vocal_dry = None
if VOCAL_HRTF:
    emit('mix', frac=0.9, drifting='vocals',
         note='vocal mover: mid above %.0f Hz, beta %.2f' % (a.xover, a.vocal_beta))
    _v, _vsr = sf.read(os.path.join(cache_dir, a.model, 'vocals.wav'),
                       dtype='float64', always_2d=True)
    if _vsr != SR:
        _g = gcd(SR, _vsr); _v = resample_poly(_v, SR // _g, _vsr // _g, axis=0)
    _v = _v[:n] if len(_v) >= n else np.pad(_v, ((0, n - len(_v)), (0, 0)))
    # Same subtraction design as the texture layer: only the directional mid
    # leaves the anchor, so the diffuse part of the vocal is never touched.
    vocal_dry = dsp.mid_highpass(_v, sos) * (a.vocal_beta * _ramp)
    anchor -= np.stack([vocal_dry, vocal_dry], 1)
    del _v

emit('mix', frac=1.0, drifting=None,
     sources=len(drift_info) + 1 + (1 if VOCAL_HRTF else 0))

# -------------------------------------------------------------- convolve ----
s  = sofar.read_sofa(os.path.join(HERE,'hrtf','D1_HRIR_SOFA',SOFA[SR]), verify=False)
IR = np.array(s.Data_IR); P = np.array(s.SourcePosition)
ar, er = np.deg2rad(P[:,0]), np.deg2rad(P[:,1])
grid = np.stack([np.cos(er)*np.cos(ar), np.cos(er)*np.sin(ar), np.sin(er)], 1)

BLOCK = 4096; HOP = BLOCK//2
starts = np.arange(0, n, HOP); tb = (starts + BLOCK/2)/SR
NFFT = 1 << int(np.ceil(np.log2(BLOCK + IR.shape[2] - 1)))
BATCH = a.batch or dsp.auto_batch(NFFT)

MOVERS = [('other', mover_dry, dsp.wander_path(tb, a.orbit, a.elev))]
if VOCAL_HRTF:
    MOVERS.append(('vocals', vocal_dry, dsp.stage_path(tb, a.vocal_arc, a.vocal_elev)))

print("rendering %d blocks x %d source(s) (batch %d)..."
      % (len(starts), len(MOVERS), BATCH), flush=True)

def _render(dry, az_deg, el_deg, tag, base, span):
    """HRTF-render one mono mover along one path, level-matched to its input."""
    az = np.deg2rad(az_deg % 360.0); el = np.deg2rad(el_deg)
    v = np.stack([np.cos(el)*np.cos(az), np.cos(el)*np.sin(az), np.sin(el)], -1)
    idx = np.argmax(grid @ v.T, axis=0)
    t0 = time.monotonic()

    def cb(done, total):
        dt = time.monotonic() - t0
        emit('convolve', frac=base + span * done / total, source=tag,
             blocks=int(done), total=int(total), batch=int(BATCH), block=BLOCK,
             hop=HOP, nfft=NFFT, taps=int(IR.shape[2]), sr=SR,
             positions=int(len(np.unique(idx))),
             rate=round(done / dt) if dt > 0.05 else None)

    cb(0, len(starts))
    out = dsp.hrtf_convolve(dry, idx, IR, BLOCK, HOP, NFFT, batch=BATCH, progress=cb)
    # Level-match to the dry mover. A silent stem would otherwise divide by zero.
    rms_out = np.sqrt(np.mean(out ** 2))
    if rms_out > 0:
        out *= np.sqrt(np.mean(dry ** 2)) / rms_out * np.sqrt(2)
    return out

_span = 1.0 / len(MOVERS)
mover = None
for _mi in range(len(MOVERS)):
    _tag, _dry, (_az, _el) = MOVERS[_mi]
    _r = _render(_dry, _az, _el, _tag, _mi * _span, _span)
    MOVERS[_mi] = (_tag, None, None)          # release this mover's dry signal
    if mover is None:
        mover = _r
    else:
        mover += _r                            # in place: no third full buffer
        del _r
del mover_dry, vocal_dry

anchor += mover                                # in place; anchor becomes the mix
del mover
mix = anchor
# Peak normalisation only. Subtracting the mover and adding an HRTF'd copy back
# raises the peak well above the source's, so this costs real loudness -- about
# 7.5 dB on a limited master. It is not corrected here: raising the render would
# clip, and limiting it would spend the fidelity this whole design protects. The
# player matches levels across modes instead, using the figures below.
mix *= 0.97/np.abs(mix).max()
_ls, _lf = dsp.loudness_window(len(mix), SR)
AW_OUT = dsp.a_weighted_db(mix[_ls:_ls + _lf], SR)

# ---------------------------------------------------------------- encode ----
outp = a.out or (os.path.splitext(a.infile)[0] + '_binaural.flac')
emit('encode', frac=0.0, fmt='FLAC', bits=24, sr=SR,
     note='encoding %s' % os.path.basename(outp))
sf.write(outp, mix, SR, subtype='PCM_24')

meta = {"output": outp, "sr": SR, "drift": drift_info, "env_file": env_path,
  "orbit": a.orbit, "elev": a.elev, "ramp": a.ramp, "beta": a.beta,
  "xover": a.xover, "duration": n/SR, "blocks": int(len(starts)), "batch": int(BATCH),
  "preset": a.preset, "aw_src": round(AW_SRC, 2), "aw_out": round(AW_OUT, 2), "movers": [m[0] for m in MOVERS],
  "vocal_path": ({"arc": a.vocal_arc, "elev": a.vocal_elev, "beta": a.vocal_beta}
                 if VOCAL_HRTF else None),
  "iacc_orig": round(IACC_ORIG,3), "iacc_out": round(iacc(mix),3),
  "bass_iacc_orig": round(BASS_IACC_ORIG,3),
  "bass_iacc_out":  round(iacc(sosfiltfilt(lp,mix,axis=0)),3),
  "peak": round(float(np.abs(mix).max()),3),
  "bytes": os.path.getsize(outp)}
emit('encode', frac=1.0, fmt='FLAC', bits=24, sr=SR,
     mb=round(meta["bytes"] / 1e6, 1))
emit('result', meta=meta)

if a.json:
    print(json.dumps(meta, indent=2))
else:
    print("wrote %s (%.1f MB, %.0f Hz, IACC %.3f -> %.3f)"
          % (outp, meta["bytes"] / 1e6, SR, meta["iacc_orig"], meta["iacc_out"]))
