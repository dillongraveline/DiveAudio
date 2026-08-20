#!/usr/bin/env python
"""Binaural 3D-ify any track, preserving fidelity.

  usage:  spatialize_cli.py <audio-file> [--beta 0.85] [--orbit 50] [--xover 200]

Moves only the DIRECTIONAL component of the non-percussive layer; the diffuse
field, the bass and the drums are never HRTF-filtered and never reconstructed
from stems, so the original master survives underneath.
"""
import sys, os, json, subprocess, argparse, hashlib, fcntl, shutil, uuid
import numpy as np, soundfile as sf, sofar
from scipy.signal import resample_poly, butter, sosfiltfilt
from math import gcd

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
p.add_argument('--preset', default=None, choices=['subtle','natural','default','wide'])
p.add_argument('--model', default='htdemucs')
p.add_argument('--shifts', type=int, default=0)
a = p.parse_args()
if a.preset:
    a.beta, a.orbit = {'subtle': (0.60, 72.0), 'natural': (0.85, 50.0),
                    'default': (0.92, 43.0), 'wide': (1.0, 36.0)}[a.preset]

def load_audio(path):
    """Decode anything we can. soundfile covers wav/flac/ogg/mp3; macOS
    afconvert covers m4a/aac/alac without needing ffmpeg installed."""
    try:
        return sf.read(path, dtype='float64', always_2d=True)
    except Exception as first:
        conv = '/usr/bin/afconvert'
        if not os.path.exists(conv):
            raise RuntimeError("cannot decode %s (%s)" % (os.path.basename(path), first))
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

orig, sr0 = load_audio(a.infile)
if orig.shape[1] == 1: orig = np.repeat(orig, 2, axis=1)
SR = sr0 if sr0 in SOFA else 96000
print("source : %d Hz %dch %.1fs -> working at %d Hz"
      % (sr0, orig.shape[1], len(orig) / sr0, SR), flush=True)
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
SRC_SHA, SRC_SECONDS = _sha256(a.infile), n / SR
key = SRC_SHA[:24] + '_' + a.model + '_s' + str(a.shifts)

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

# Exclusive lock per cache key: concurrent runs on the same file serialise, and
# the lock is released by the OS even if the process is killed.
with open(os.path.join(stems_root, key + '.lock'), 'w') as _lf:
    fcntl.flock(_lf, fcntl.LOCK_EX)
    if _cache_ok():
        print("stems cached (" + key[:12] + ") - skipping separation", flush=True)
    else:
        print("separating (" + a.model + ", shifts=" + str(a.shifts) + ") -> " + key[:12], flush=True)
        shutil.rmtree(cache_dir, ignore_errors=True)
        tmp_dir = cache_dir + '.tmp.' + uuid.uuid4().hex[:8]
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            # demucs decodes via ffmpeg, which may be absent; soundfile handles
            # wav/flac/ogg/mp3 directly, so feed demucs a plain WAV instead.
            feed = os.path.join(tmp_dir, '_input.wav')
            sf.write(feed, orig.astype('float32'), SR, subtype='FLOAT')
            subprocess.run([sys.executable, '-m', 'demucs', '-n', a.model, '-d', 'mps',
                            '--shifts', str(a.shifts), '-o', tmp_dir,
                            '--filename', '{stem}.wav', feed], check=True, cwd=HERE)
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
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # Per-stem RMS envelopes for the visualiser: computed once per cache entry,
    # normalised against a shared peak so relative loudness between stems is
    # preserved, then quantised to uint8 to keep the payload small.
    env_path = os.path.join(cache_dir, 'envelopes.json')
    if not os.path.exists(env_path):
        ENV_HZ = 20
        raw = {}
        for _s in STEMS:
            _d, _sr = sf.read(os.path.join(cache_dir, a.model, _s + '.wav'),
                              dtype='float32', always_2d=True)
            _m = _d.mean(axis=1)
            _hop = max(1, int(round(_sr / ENV_HZ)))
            _nf = int(np.ceil(len(_m) / _hop))
            if _nf * _hop > len(_m):
                _m = np.pad(_m, (0, _nf * _hop - len(_m)))
            raw[_s] = np.sqrt((_m.reshape(_nf, _hop) ** 2).mean(axis=1))
            del _d, _m
        _peak = max(float(v.max()) for v in raw.values()) or 1.0
        env = {k: np.clip(v / _peak * 255.0, 0, 255).astype(np.uint8).tolist()
               for k, v in raw.items()}
        _tmp = env_path + '.tmp'
        with open(_tmp, 'w') as _fh:
            json.dump({'rate': ENV_HZ, 'peak': _peak, 'stems': env}, _fh)
        os.replace(_tmp, env_path)

other, sro = sf.read(stem_wav, dtype='float64', always_2d=True)
if sro != SR:
    g = gcd(SR, sro); other = resample_poly(other, SR//g, sro//g, axis=0)
if abs(len(other) / SR - n / SR) > 0.25:
    raise RuntimeError("stem/source length mismatch (%.2fs vs %.2fs) - refusing to mix"
                       % (len(other) / SR, n / SR))
other = other[:n] if len(other) >= n else np.pad(other, ((0, n-len(other)), (0,0)))

sos = butter(4, a.xover, 'low', fs=SR, output='sos')
hi  = other - sosfiltfilt(sos, other, axis=0)
M   = (hi[:,0] + hi[:,1]) / 2.0                       # directional part only
beta = a.beta * np.clip(np.arange(n)/SR/a.ramp, 0, 1)
mover_dry = beta * M
anchor = orig - np.stack([mover_dry, mover_dry], 1)   # diffuse side-signal stays in here

# ---- very slow drift for the otherwise-anchored elements -------------------
# These use constant-power AMPLITUDE panning, never HRTF: level only, so their
# timbre is untouched. Only the directional (mid) component above the crossover
# is moved, at low depth, and the period is tied to the track length so a source
# takes roughly half the song to cross its (small) range. Bass never moves --
# low-frequency panning is inaudible and only muddies the centre.
SUBTLE = {'drums': (0.20, 0.34, 1.7), 'vocals': (0.18, 0.28, 4.0)}
_tt   = np.arange(n) / SR
_dur  = max(n / SR, 1.0)
_ramp = np.clip(_tt / a.ramp, 0, 1)
drift_info = {}
for _name, (_bk, _span, _ph) in SUBTLE.items():
    _sp = os.path.join(cache_dir, a.model, _name + '.wav')
    try:
        _d, _dsr = sf.read(_sp, dtype='float64', always_2d=True)
    except Exception:
        continue
    if _dsr != SR:
        _g = gcd(SR, _dsr); _d = resample_poly(_d, SR // _g, _dsr // _g, axis=0)
    _d = _d[:n] if len(_d) >= n else np.pad(_d, ((0, n - len(_d)), (0, 0)))
    _hi = _d - sosfiltfilt(sos, _d, axis=0)
    _Mk = ((_hi[:, 0] + _hi[:, 1]) / 2.0) * _bk * _ramp
    _pan = _span * np.sin(2 * np.pi * _tt / (_dur * 1.7) + _ph)
    _ang = (_pan + 1.0) * (np.pi / 4.0)
    anchor -= np.stack([_Mk, _Mk], 1)
    anchor += np.stack([_Mk * np.cos(_ang), _Mk * np.sin(_ang)], 1) * np.sqrt(2.0)
    drift_info[_name] = {'depth': _bk, 'span': _span, 'phase': _ph, 'period': _dur * 1.7}
    del _d, _hi, _Mk

s  = sofar.read_sofa(os.path.join(HERE,'hrtf','D1_HRIR_SOFA',SOFA[SR]), verify=False)
IR = np.array(s.Data_IR); P = np.array(s.SourcePosition)
ar, er = np.deg2rad(P[:,0]), np.deg2rad(P[:,1])
grid = np.stack([np.cos(er)*np.cos(ar), np.cos(er)*np.sin(ar), np.sin(er)], 1)

BLOCK = 4096; HOP = BLOCK//2
starts = np.arange(0, n, HOP); tb = (starts + BLOCK/2)/SR
# Quasi-random wander: sums of sines whose periods share no common multiple,
# so the path never repeats. Coefficients tuned so RMS angular speed matches
# the ~7 deg/s of the circular orbit this replaced.
_s = a.orbit / 50.0
_TAU = 2.0 * np.pi
az_deg = 180.0 * (0.55 * np.sin(_TAU * tb / (97.0 * _s))
                + 0.30 * np.sin(_TAU * tb / (61.0 * _s) + 1.7)
                + 0.15 * np.sin(_TAU * tb / (37.0 * _s) + 4.1))
el_deg = a.elev * 1.2 * (0.60 * np.sin(_TAU * tb / (71.0 * _s) + 0.8)
                       + 0.40 * np.sin(_TAU * tb / (43.0 * _s) + 2.9))
az = np.deg2rad(az_deg % 360.0); el = np.deg2rad(el_deg)
v = np.stack([np.cos(el)*np.cos(az), np.cos(el)*np.sin(az), np.sin(el)], -1)
idx = np.argmax(grid @ v.T, axis=0)

NFFT = 1 << int(np.ceil(np.log2(BLOCK + IR.shape[2] - 1))); cache = {}
def hf(i):
    if i not in cache: cache[i] = np.fft.rfft(IR[i], NFFT, axis=-1)
    return cache[i]
win = np.hanning(BLOCK+1)[:BLOCK]; out = np.zeros((n+NFFT, 2))
print(f"rendering {len(starts)} blocks...")
for k, st in enumerate(starts):
    blk = mover_dry[st:st+BLOCK]
    if len(blk) < BLOCK: blk = np.concatenate([blk, np.zeros(BLOCK-len(blk))])
    if not blk.any(): continue
    out[st:st+NFFT] += np.fft.irfft(np.fft.rfft(blk*win, NFFT)*hf(idx[k]), NFFT, axis=-1).T
mover = out[:n]
mover *= np.sqrt(np.mean(mover_dry**2))/np.sqrt(np.mean(mover**2))*np.sqrt(2)
mix = anchor + mover
mix *= 0.97/np.abs(mix).max()

outp = a.out or (os.path.splitext(a.infile)[0] + '_binaural.flac')
sf.write(outp, mix, SR, subtype='PCM_24')

def iacc(x):
    L,R = x[:,0]-x[:,0].mean(), x[:,1]-x[:,1].mean()
    return float(np.dot(L,R)/(np.linalg.norm(L)*np.linalg.norm(R)))
lp = butter(4, a.xover, 'low', fs=SR, output='sos')
print(json.dumps({"output": outp, "sr": SR, "drift": drift_info, "env_file": env_path, "orbit": a.orbit, "elev": a.elev, "ramp": a.ramp, "beta": a.beta, "xover": a.xover, "duration": n/SR,
  "iacc_orig": round(iacc(orig),3), "iacc_out": round(iacc(mix),3),
  "bass_iacc_orig": round(iacc(sosfiltfilt(lp,orig,axis=0)),3),
  "bass_iacc_out":  round(iacc(sosfiltfilt(lp,mix,axis=0)),3),
  "peak": round(float(np.abs(mix).max()),3)}, indent=2))
