"""The convolution refactor must not change the audio. These tests pin the
batched implementation against the exact per-block loop it replaces."""
import numpy as np
import pytest

import dsp


# ---------------------------------------------------------------- reference --
def reference_convolve(mover_dry, idx, IR, block, hop, nfft):
    """Verbatim transcription of the original loop in spatialize_cli.py.

    Kept in the test suite, never shipped: it is the oracle the batched
    implementation is measured against, so it must stay a literal copy.
    """
    n = len(mover_dry)
    starts = np.arange(0, n, hop)
    cache = {}

    def hf(i):
        if i not in cache:
            cache[i] = np.fft.rfft(IR[i], nfft, axis=-1)
        return cache[i]

    win = np.hanning(block + 1)[:block]
    out = np.zeros((n + nfft, 2))
    for k, st in enumerate(starts):
        blk = mover_dry[st:st + block]
        if len(blk) < block:
            blk = np.concatenate([blk, np.zeros(block - len(blk))])
        if not blk.any():
            continue
        out[st:st + nfft] += np.fft.irfft(
            np.fft.rfft(blk * win, nfft) * hf(idx[k]), nfft, axis=-1).T
    return out[:n]


# ------------------------------------------------------------------ fixtures --
@pytest.fixture
def rig():
    """A small but structurally faithful rig: real HRIR shape (pos, 2, taps),
    a signal with silence in it, and a wandering index path."""
    rng = np.random.default_rng(20260820)
    block, hop, taps = 512, 256, 64
    nfft = 1 << int(np.ceil(np.log2(block + taps - 1)))
    n = block * 9 + 137                      # deliberately not a block multiple
    sig = rng.standard_normal(n) * 0.1
    sig[block * 3:block * 5] = 0.0           # a silent stretch to exercise skipping
    IR = rng.standard_normal((24, 2, taps)) * 0.2
    idx = np.abs(np.sin(np.arange(len(np.arange(0, n, hop))) * 0.4) * 23).astype(int)
    return dict(sig=sig, idx=idx, IR=IR, block=block, hop=hop, nfft=nfft, n=n)


# --------------------------------------------------------------------- tests --
def test_batched_matches_reference(rig):
    want = reference_convolve(rig["sig"], rig["idx"], rig["IR"],
                              rig["block"], rig["hop"], rig["nfft"])
    got = dsp.hrtf_convolve(rig["sig"], rig["idx"], rig["IR"],
                            rig["block"], rig["hop"], rig["nfft"], batch=8)
    assert got.shape == want.shape == (rig["n"], 2)
    # FFT reassociation only: this must be float noise, not a real difference.
    assert np.max(np.abs(got - want)) < 1e-9 * max(1.0, np.max(np.abs(want)))


@pytest.mark.parametrize("batch", [1, 2, 3, 7, 64, 4096])
def test_result_is_independent_of_batch_size(rig, batch):
    base = dsp.hrtf_convolve(rig["sig"], rig["idx"], rig["IR"],
                             rig["block"], rig["hop"], rig["nfft"], batch=1)
    got = dsp.hrtf_convolve(rig["sig"], rig["idx"], rig["IR"],
                            rig["block"], rig["hop"], rig["nfft"], batch=batch)
    assert np.max(np.abs(got - base)) < 1e-9 * max(1.0, np.max(np.abs(base)))


def test_all_silent_input_yields_silence(rig):
    quiet = np.zeros(rig["n"])
    got = dsp.hrtf_convolve(quiet, rig["idx"], rig["IR"],
                            rig["block"], rig["hop"], rig["nfft"], batch=8)
    assert not got.any()


def test_progress_reports_every_block_exactly_once(rig):
    seen = []
    dsp.hrtf_convolve(rig["sig"], rig["idx"], rig["IR"],
                      rig["block"], rig["hop"], rig["nfft"], batch=4,
                      progress=lambda done, total: seen.append((done, total)))
    total_blocks = len(np.arange(0, rig["n"], rig["hop"]))
    assert seen, "convolution reported no progress at all"
    assert {t for _, t in seen} == {total_blocks}
    done = [d for d, _ in seen]
    assert done == sorted(done), "progress went backwards"
    assert done[-1] == total_blocks, "final progress did not reach 100%"


def test_auto_batch_respects_budget_and_bounds():
    nfft = 8192
    small = dsp.auto_batch(nfft, budget_bytes=1)
    assert small >= 1, "batch must never be zero"
    big = dsp.auto_batch(nfft, budget_bytes=1 << 40)
    assert big <= dsp.MAX_BATCH
    # A realistic budget must land somewhere genuinely batched.
    mid = dsp.auto_batch(nfft, budget_bytes=128 << 20)
    assert 16 <= mid <= dsp.MAX_BATCH
    assert dsp.auto_batch(nfft, budget_bytes=1 << 30) >= mid
