"""Signal-processing kernels for DiveAudio, split out so they can be tested.

The convolution here replaces a per-block Python loop that ran one rfft and one
irfft per 4096-sample block -- roughly 11,000 round trips for a four-minute
track at 96 kHz. Batching the transforms is the entire optimisation; the maths
is unchanged, and tests/test_dsp.py pins the output against the original loop.
"""
import numpy as np
from scipy.signal import sosfiltfilt

MAX_BATCH = 512
DEFAULT_BUDGET = 128 << 20          # peak working set for the batched transforms


def auto_batch(nfft, budget_bytes=DEFAULT_BUDGET):
    """How many blocks to transform at once inside a memory budget.

    Held concurrently, per block: the mono spectrum, the two-ear product, and
    the two-ear time-domain result. That is what sets the batch, not the input.
    """
    bins = nfft // 2 + 1
    per_block = bins * 16 + 2 * bins * 16 + 2 * nfft * 8
    return max(1, min(MAX_BATCH, int(budget_bytes // max(per_block, 1))))


def hrtf_convolve(mover_dry, idx, IR, block, hop, nfft, batch=None, progress=None):
    """Overlap-add HRTF convolution of a mono signal along a path of HRIR indices.

    idx[k] selects the HRIR for the k-th block. Returns (len(mover_dry), 2).
    progress(done, total) is called once per batch, in blocks.
    """
    n = len(mover_dry)
    starts = np.arange(0, n, hop)
    total = len(starts)
    out = np.zeros((n + nfft, 2))
    if total == 0:
        return out[:n]

    batch = max(1, int(batch if batch is not None else auto_batch(nfft)))
    win = np.hanning(block + 1)[:block]

    # HRIR spectra for the positions this path actually visits, computed once.
    # A wandering path revisits its grid points constantly, so the unique set is
    # far smaller than the block count -- the original recomputed through a dict.
    idx = np.asarray(idx)[:total]
    used = np.unique(idx)
    table = np.fft.rfft(IR[used], nfft, axis=-1)          # (unique, 2, bins)
    row = np.zeros(int(used.max()) + 1, dtype=np.intp)
    row[used] = np.arange(len(used))

    # Pad once so every block -- including the short final one -- is a plain
    # slice, which lets the whole batch be gathered with one fancy index.
    padded = np.zeros(int(starts[-1]) + block)
    padded[:n] = mover_dry
    offsets = np.arange(block)

    for lo in range(0, total, batch):
        hi = min(lo + batch, total)
        st = starts[lo:hi]
        blocks = padded[st[:, None] + offsets]            # (B, block)
        # Silent blocks contribute exactly zero; skipping them is what the
        # original loop did, and it is most of the win on sparse material.
        live = blocks.any(axis=1)
        if live.any():
            spec = np.fft.rfft(blocks[live] * win, nfft, axis=-1)      # (B', bins)
            ear = spec[:, None, :] * table[row[idx[lo:hi][live]]]      # (B', 2, bins)
            time = np.fft.irfft(ear, nfft, axis=-1)                    # (B', 2, nfft)
            for j, s in enumerate(st[live]):
                out[s:s + nfft] += time[j].T
        if progress:
            progress(hi, total)
    return out[:n]


def rms_envelope(path, env_hz=20, frames_per_chunk=512):
    """Streaming RMS envelope of a sound file, averaged across channels.

    Reads in hop-aligned chunks instead of pulling the whole stem into memory,
    which is what the previous inline version did for each of the four stems.
    Returns (envelope, hop, n_frames).
    """
    import soundfile as sf

    info = sf.info(path)
    hop = max(1, int(round(info.samplerate / env_hz)))
    n_frames = int(np.ceil(info.frames / hop))
    acc = np.zeros(n_frames, dtype=np.float64)

    at = 0
    for chunk in sf.blocks(path, blocksize=hop * frames_per_chunk,
                           dtype='float32', always_2d=True):
        mono = chunk.mean(axis=1).astype(np.float64)
        k = int(np.ceil(len(mono) / hop))
        if k * hop > len(mono):                    # only ever the final chunk
            mono = np.pad(mono, (0, k * hop - len(mono)))
        k = min(k, n_frames - at)
        if k <= 0:
            break
        acc[at:at + k] = (mono[:k * hop].reshape(k, hop) ** 2).mean(axis=1)
        at += k

    return np.sqrt(acc), hop, n_frames


def quantise_envelopes(raw):
    """Scale per-stem envelopes against one shared peak, then pack to uint8 so
    relative loudness between stems survives and the payload stays small."""
    peak = max((float(v.max()) for v in raw.values() if len(v)), default=0.0) or 1.0
    # Round rather than truncate: the peak frame lands a hair under 255.0 after
    # float error, and truncation used to quantise the loudest moment to 254.
    stems = {k: np.rint(np.clip(v / peak * 255.0, 0, 255)).astype(np.uint8).tolist()
             for k, v in raw.items()}
    return stems, peak


def mid_highpass(x, sos):
    """Highpassed mid channel of a stereo signal: mid(x) - lowpass(mid(x)).

    The mix stage used to lowpass both channels and then collapse to mid. The
    filter is linear, so collapsing first gives the same answer for half the
    filtering and half the peak memory -- and sosfiltfilt runs the signal
    forwards and backwards, so it is the dominant cost of that stage.
    """
    mid = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    return mid - sosfiltfilt(sos, mid)


TAU = 2.0 * np.pi


def wander_path(t, orbit, elev):
    """The free wander used by the texture layer.

    Sums of sines whose periods share no common multiple, so the path never
    repeats while staying a pure function of time -- which is what lets the
    renderer and the browser compute the identical trajectory independently.
    """
    s = orbit / 50.0
    az = 180.0 * (0.55 * np.sin(TAU * t / (97.0 * s))
                  + 0.30 * np.sin(TAU * t / (61.0 * s) + 1.7)
                  + 0.15 * np.sin(TAU * t / (37.0 * s) + 4.1))
    el = elev * 1.2 * (0.60 * np.sin(TAU * t / (71.0 * s) + 0.8)
                       + 0.40 * np.sin(TAU * t / (43.0 * s) + 2.9))
    return az, el


def stage_path(t, arc, elev):
    """A performer working the front of a stage.

    Unlike the wander, this is bounded: the weights sum to exactly 1, so the
    azimuth never leaves +/-arc and the voice stays in front of the listener
    rather than orbiting behind their head. Periods are much shorter than the
    wander's, because a singer crosses a stage in tens of seconds, not minutes.
    """
    az = arc * (0.58 * np.sin(TAU * t / 41.0)
                + 0.30 * np.sin(TAU * t / 27.0 + 2.2)
                + 0.12 * np.sin(TAU * t / 19.0 + 5.0))
    el = elev * (0.60 * np.sin(TAU * t / 33.0 + 1.1)
                 + 0.40 * np.sin(TAU * t / 23.0 + 3.7))
    return az, el


def a_weighting_sos(fs):
    """IEC 61672 A-weighting as second-order sections.

    Raw energy is a poor stand-in for how loud something sounds: bass carries
    enormous power and modest loudness, so an unweighted comparison of two
    mixes can be wrong by more than 10 dB.
    """
    from scipy.signal import bilinear_zpk, zpk2sos
    f1, f2, f3, f4 = 20.598997, 107.65265, 737.86223, 12194.217
    zeros = np.zeros(4)
    poles = -2 * np.pi * np.array([f1, f1, f2, f3, f4, f4])
    gain = (2 * np.pi * f4) ** 2 * (10 ** (1.9997 / 20))
    return zpk2sos(*bilinear_zpk(zeros, poles, gain, fs))


def a_weighted_db(x, fs):
    """A-weighted level of a signal's mono sum, in dBFS.

    The mono sum, not the per-channel average: two mixes can carry the same
    energy per channel and still differ in perceived level when one of them is
    decorrelated, which is exactly what HRTF rendering does.
    """
    from scipy.signal import sosfilt
    mono = x.mean(axis=1) if getattr(x, "ndim", 1) > 1 else x
    y = sosfilt(a_weighting_sos(fs), mono)
    return float(20.0 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-15))


LOUDNESS_WINDOW_S = 180.0


def loudness_window(n_frames, sr):
    """The span loudness is measured over: the centred LOUDNESS_WINDOW_S, or
    the whole thing if it is shorter.

    Source files and renders must use the same window or their levels are not
    comparable, and comparing them is the entire point.
    """
    span = int(LOUDNESS_WINDOW_S * sr)
    if n_frames <= span:
        return 0, int(n_frames)
    return int((n_frames - span) // 2), span
