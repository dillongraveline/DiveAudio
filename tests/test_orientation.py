"""Left must be left.

The 3D view claims to be driven by the same maths that rendered the audio, so a
sign error there is a lie about the mechanism rather than a cosmetic bug. The
mover was drawn mirrored: the orb sat right while the sound came from the left.

These tests close the loop from both ends. This file pins the assumption about
the HRIR data itself; tests/check_ui.js pins the browser's axis mapping against
that assumption.
"""
import os
from pathlib import Path

import numpy as np
import pytest

HRTF = Path(__file__).parent.parent / "hrtf" / "D1_HRIR_SOFA"
SOFA_FILE = HRTF / "D1_44K_16bit_256tap_FIR_SOFA.sofa"

pytestmark = pytest.mark.skipif(
    not SOFA_FILE.exists(), reason="SADIE HRTF database not downloaded (run setup.sh)")


@pytest.fixture(scope="module")
def hrir():
    import sofar
    s = sofar.read_sofa(str(SOFA_FILE), verify=False)
    return np.array(s.Data_IR), np.array(s.SourcePosition)


def nearest(P, az_target, el_target=0.0):
    d = np.abs(((P[:, 0] - az_target + 180) % 360) - 180) + np.abs(P[:, 1] - el_target) * 3
    return int(np.argmin(d))


def test_receiver_zero_is_the_left_ear(hrir):
    """Azimuth +90 deg must favour receiver 0, in level and in arrival time.

    Everything downstream -- the HRIR index path, and the sign the browser draws
    it with -- depends on this. If a future SOFA file flips it, this fails first.
    """
    IR, P = hrir
    ir = IR[nearest(P, 90.0)]
    rms = np.sqrt((ir ** 2).mean(axis=1))
    assert rms[0] > rms[1] * 2, "azimuth +90 should be much louder in the left ear"
    onset = [int(np.argmax(np.abs(r) > 0.15 * np.abs(r).max())) for r in ir]
    assert onset[0] < onset[1], "azimuth +90 should arrive at the left ear first"


def test_azimuth_minus_ninety_is_the_right_ear(hrir):
    IR, P = hrir
    ir = IR[nearest(P, 270.0)]
    rms = np.sqrt((ir ** 2).mean(axis=1))
    assert rms[1] > rms[0] * 2
    onset = [int(np.argmax(np.abs(r) > 0.15 * np.abs(r).max())) for r in ir]
    assert onset[1] < onset[0]


def test_drawn_position_matches_the_ear_the_sound_favours(hrir):
    """Walk the real trajectory and compare, per block, which ear the chosen
    HRIR favours against which side the browser draws the orb on."""
    IR, P = hrir
    ar, er = np.deg2rad(P[:, 0]), np.deg2rad(P[:, 1])
    grid = np.stack([np.cos(er) * np.cos(ar), np.cos(er) * np.sin(ar), np.sin(er)], 1)

    SR, orbit, elev, BLOCK = 44100, 43.0, 25.0, 4096
    starts = np.arange(0, SR * 240, BLOCK // 2)
    tb = (starts + BLOCK / 2) / SR
    sc, TAU = orbit / 50.0, 2 * np.pi
    az_deg = 180.0 * (0.55 * np.sin(TAU * tb / (97.0 * sc))
                      + 0.30 * np.sin(TAU * tb / (61.0 * sc) + 1.7)
                      + 0.15 * np.sin(TAU * tb / (37.0 * sc) + 4.1))
    el_deg = elev * 1.2 * (0.60 * np.sin(TAU * tb / (71.0 * sc) + 0.8)
                           + 0.40 * np.sin(TAU * tb / (43.0 * sc) + 2.9))
    az, el = np.deg2rad(az_deg % 360.0), np.deg2rad(el_deg)
    v = np.stack([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], -1)
    idx = np.argmax(grid @ v.T, axis=0)

    rms = np.sqrt((IR[idx] ** 2).mean(axis=2))
    ear_bias = rms[:, 1] - rms[:, 0]              # > 0 means the RIGHT ear

    # posAt() in static/index.html, with the corrected sign on x.
    three_x = -4.1 * np.cos(el) * np.sin(az)      # three.js +x is screen right

    live = np.abs(ear_bias) > 0.004               # skip near-centre blocks
    agreement = (np.sign(three_x[live]) == np.sign(ear_bias[live])).mean()
    assert agreement > 0.98, (
        "the orb is drawn on the wrong side for %.1f%% of blocks" % (100 * (1 - agreement)))
