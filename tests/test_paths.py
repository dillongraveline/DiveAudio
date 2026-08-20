"""The stage path is the one the vocalist walks. It must stay in front."""
import numpy as np
import pytest

import dsp


T = np.linspace(0, 1200, 60000)


def test_stage_azimuth_never_leaves_the_arc():
    az, _ = dsp.stage_path(T, 55.0, 9.0)
    assert np.abs(az).max() <= 55.0 + 1e-9, "the vocalist walked off the stage"


def test_stage_stays_in_front_of_the_listener():
    """Anything beyond +/-90 deg is behind the head, which is not a stage."""
    az, _ = dsp.stage_path(T, 55.0, 9.0)
    assert np.abs(az).max() < 90.0


def test_stage_actually_uses_the_arc():
    """A bounded path that never approaches its bound would not read as motion."""
    az, _ = dsp.stage_path(T, 55.0, 9.0)
    assert np.abs(az).max() > 0.8 * 55.0


def test_stage_elevation_is_bounded():
    _, el = dsp.stage_path(T, 55.0, 9.0)
    assert np.abs(el).max() <= 9.0 + 1e-9


def test_stage_crosses_the_centre_many_times_in_a_song():
    """The complaint about the old vocal drift was that it never completed a
    pass in 578 s. This path must cross centre repeatedly within a track."""
    t = np.linspace(0, 578, 200000)
    az, _ = dsp.stage_path(t, 55.0, 9.0)
    crossings = np.count_nonzero(np.diff(np.signbit(az)))
    assert crossings > 20, "only %d centre crossings in a 9:38 track" % crossings


def test_stage_path_never_repeats_exactly():
    """Incommensurate periods: no offset should reproduce the path."""
    t = np.linspace(0, 400, 40000)
    az, _ = dsp.stage_path(t, 55.0, 9.0)
    for shift in (41.0, 27.0, 19.0):
        az2, _ = dsp.stage_path(t + shift, 55.0, 9.0)
        assert np.abs(az - az2).max() > 1.0


def test_wander_path_matches_the_original_formula():
    """wander_path was extracted from spatialize_cli; it must be a pure move."""
    t = np.linspace(0, 600, 5000)
    orbit, elev = 43.0, 25.0
    s = orbit / 50.0
    TAU = 2 * np.pi
    az_want = 180.0 * (0.55 * np.sin(TAU * t / (97.0 * s))
                       + 0.30 * np.sin(TAU * t / (61.0 * s) + 1.7)
                       + 0.15 * np.sin(TAU * t / (37.0 * s) + 4.1))
    el_want = elev * 1.2 * (0.60 * np.sin(TAU * t / (71.0 * s) + 0.8)
                            + 0.40 * np.sin(TAU * t / (43.0 * s) + 2.9))
    az, el = dsp.wander_path(t, orbit, elev)
    assert np.abs(az - az_want).max() == 0.0
    assert np.abs(el - el_want).max() == 0.0


@pytest.mark.parametrize("arc", [20.0, 40.0, 70.0])
def test_arc_scales_the_excursion(arc):
    az, _ = dsp.stage_path(T, arc, 9.0)
    assert np.abs(az).max() <= arc + 1e-9
    assert np.abs(az).max() > 0.8 * arc
