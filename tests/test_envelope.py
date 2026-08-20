"""The streaming envelope must agree with the whole-file version it replaces."""
import numpy as np
import soundfile as sf
import pytest

import dsp


def whole_file_envelope(path, env_hz=20):
    """The original inline computation from spatialize_cli.py, as the oracle."""
    d, sr = sf.read(path, dtype='float32', always_2d=True)
    m = d.mean(axis=1)
    hop = max(1, int(round(sr / env_hz)))
    nf = int(np.ceil(len(m) / hop))
    if nf * hop > len(m):
        m = np.pad(m, (0, nf * hop - len(m)))
    return np.sqrt((m.reshape(nf, hop) ** 2).mean(axis=1))


@pytest.fixture
def stem(tmp_path):
    rng = np.random.default_rng(7)
    sr = 44100
    n = int(sr * 3.37)                       # deliberately not a whole number of hops
    sig = (rng.standard_normal((n, 2)) * 0.05).astype('float32')
    sig[sr:sr * 2] *= 8.0                    # a loud passage, so frames differ
    p = tmp_path / "vocals.wav"
    sf.write(p, sig, sr, subtype='FLOAT')
    return str(p)


def test_streaming_matches_whole_file(stem):
    want = whole_file_envelope(stem)
    got, hop, nf = dsp.rms_envelope(stem, env_hz=20)
    assert len(got) == len(want) == nf
    assert np.max(np.abs(got - want)) < 1e-6


def test_quantised_bytes_are_identical(stem):
    """What actually ships is uint8, so that is what must match exactly."""
    want = whole_file_envelope(stem)
    got, _, _ = dsp.rms_envelope(stem, env_hz=20)
    qw, _ = dsp.quantise_envelopes({"s": want})
    qg, _ = dsp.quantise_envelopes({"s": got})
    assert qw["s"] == qg["s"]


@pytest.mark.parametrize("chunk", [1, 3, 64, 100000])
def test_chunk_size_does_not_change_result(stem, chunk):
    base, _, _ = dsp.rms_envelope(stem, env_hz=20, frames_per_chunk=64)
    got, _, _ = dsp.rms_envelope(stem, env_hz=20, frames_per_chunk=chunk)
    assert np.max(np.abs(got - base)) < 1e-9


def test_shared_peak_preserves_relative_loudness():
    raw = {"drums": np.array([1.0, 0.5]), "vocals": np.array([0.25, 0.0])}
    stems, peak = dsp.quantise_envelopes(raw)
    assert peak == 1.0
    assert stems["drums"] == [255, 128]
    assert stems["vocals"] == [64, 0]


def test_quantise_handles_all_silent_stems():
    stems, peak = dsp.quantise_envelopes({"bass": np.zeros(4)})
    assert peak == 1.0 and stems["bass"] == [0, 0, 0, 0]
