"""Loudness matching across modes rests on this measurement being sane."""
import numpy as np
import pytest

import dsp


SR = 44100


def tone(f, secs=2.0, amp=0.5, stereo=True):
    t = np.arange(int(SR * secs)) / SR
    x = amp * np.sin(2 * np.pi * f * t)
    return np.stack([x, x], 1) if stereo else x


def test_doubling_amplitude_is_six_db():
    a = dsp.a_weighted_db(tone(1000, amp=0.25), SR)
    b = dsp.a_weighted_db(tone(1000, amp=0.50), SR)
    assert b - a == pytest.approx(6.02, abs=0.05)


def test_bass_is_weighted_far_below_a_kilohertz():
    """The whole point: equal-energy bass must measure much quieter, which is
    why raw energy said guitar was 3.6% of a track it dominates."""
    low = dsp.a_weighted_db(tone(50), SR)
    mid = dsp.a_weighted_db(tone(1000), SR)
    assert mid - low > 25, "A-weighting is not discounting bass"


def test_one_kilohertz_is_the_reference_point():
    """A-weighting is defined as 0 dB at 1 kHz."""
    x = tone(1000, amp=1.0)
    unweighted = 20 * np.log10(np.sqrt((x.mean(axis=1) ** 2).mean()))
    assert dsp.a_weighted_db(x, SR) == pytest.approx(unweighted, abs=0.2)


def test_silence_does_not_blow_up():
    assert dsp.a_weighted_db(np.zeros((SR, 2)), SR) < -200


def test_mono_sum_notices_decorrelation():
    """Two channels of the same signal are louder than two uncorrelated ones
    at the same per-channel level. That difference is why Stage Simulator
    measured quieter than Enhanced even though its channels matched."""
    rng = np.random.default_rng(5)
    a = rng.standard_normal(SR * 2) * 0.2
    b = rng.standard_normal(SR * 2) * 0.2
    correlated = np.stack([a, a], 1)
    decorrelated = np.stack([a, b], 1)
    assert dsp.a_weighted_db(correlated, SR) > dsp.a_weighted_db(decorrelated, SR) + 2


def test_accepts_mono_input():
    assert np.isfinite(dsp.a_weighted_db(tone(1000, stereo=False), SR))
