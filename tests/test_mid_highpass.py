"""The mix stage filters stereo, then throws away everything but the mid
channel. Filtering the mid channel directly is the same operation by
linearity -- these tests hold that claim to float tolerance."""
import numpy as np
import pytest
from scipy.signal import butter, sosfiltfilt

import dsp


@pytest.fixture
def sig():
    rng = np.random.default_rng(11)
    sr = 44100
    x = rng.standard_normal((sr * 4, 2)) * 0.1
    x[:, 1] += 0.3 * x[:, 0]              # correlated channels, like real music
    return x, sr


def original_mid_highpass(x, sos):
    """What the code used to do: filter both channels, then take the mid."""
    hi = x - sosfiltfilt(sos, x, axis=0)
    return (hi[:, 0] + hi[:, 1]) / 2.0


def test_mid_then_filter_equals_filter_then_mid(sig):
    x, sr = sig
    sos = butter(4, 200.0, 'low', fs=sr, output='sos')
    want = original_mid_highpass(x, sos)
    got = dsp.mid_highpass(x, sos)
    assert got.shape == want.shape
    assert np.max(np.abs(got - want)) < 1e-12 * max(1.0, np.max(np.abs(want)))


def test_mono_input_is_handled(sig):
    x, sr = sig
    sos = butter(4, 200.0, 'low', fs=sr, output='sos')
    mono = x[:, :1]
    want = original_mid_highpass(np.repeat(mono, 2, axis=1), sos)
    got = dsp.mid_highpass(mono, sos)
    assert np.max(np.abs(got - want)) < 1e-12 * max(1.0, np.max(np.abs(want)))


def test_result_is_mono_and_finite(sig):
    x, sr = sig
    sos = butter(4, 200.0, 'low', fs=sr, output='sos')
    got = dsp.mid_highpass(x, sos)
    assert got.ndim == 1 and np.isfinite(got).all()


def test_dc_is_removed_by_the_highpass(sig):
    """A pure DC offset is entirely below a 200 Hz crossover, so the
    highpassed mid must be ~zero -- a sanity check that we kept the sign."""
    x, sr = sig
    sos = butter(4, 200.0, 'low', fs=sr, output='sos')
    dc = np.ones((sr * 2, 2)) * 0.5
    got = dsp.mid_highpass(dc, sos)
    assert np.max(np.abs(got[sr // 2:-sr // 2])) < 1e-6
