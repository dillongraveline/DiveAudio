"""The browser and the renderer must compute the SAME trajectory.

The README's claim is that the picture is driven by the same maths as the
audio. That only holds if the two implementations agree -- and a sign error
between them is exactly how the mover came to be drawn mirrored. These tests
run the browser's functions and hold them against dsp.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import dsp

NODE = shutil.which("node")
DUMP = Path(__file__).parent / "dump_paths.js"
TS = [0.0, 3.5, 17.25, 61.0, 140.5, 289.0, 433.75, 578.0]

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


@pytest.fixture(scope="module")
def drawn():
    r = subprocess.run([NODE, str(DUMP), json.dumps(TS)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def expected(az_deg, el_deg, radius):
    """The axis mapping the browser must use: SOFA +y (left) -> three.js -x."""
    az, el = np.deg2rad(az_deg), np.deg2rad(el_deg)
    return np.stack([-radius * np.cos(el) * np.sin(az),
                     radius * np.sin(el),
                     -radius * np.cos(el) * np.cos(az)], -1)


def test_wander_path_matches_the_renderer(drawn):
    az, el = dsp.wander_path(np.array(TS), 43.0, 25.0)
    want = expected(az, el, 4.1)
    got = np.array(drawn["wander"])
    assert np.abs(got - want).max() < 1e-9


def test_stage_path_matches_the_renderer(drawn):
    az, el = dsp.stage_path(np.array(TS), 55.0, 9.0)
    want = expected(az, el, 4.1 * 0.72)
    got = np.array(drawn["stage"])
    assert np.abs(got - want).max() < 1e-9


def test_the_vocalist_is_drawn_in_front(drawn):
    """A stage is in front of you. Every sampled position must have z < 0."""
    got = np.array(drawn["stage"])
    assert (got[:, 2] < 0).all(), "the vocalist walked behind the listener"


def test_the_two_paths_are_not_the_same_place(drawn):
    """If the vocal tracked the wanderer they would fuse into one image."""
    sep = np.linalg.norm(np.array(drawn["wander"]) - np.array(drawn["stage"]), axis=1)
    assert sep.min() > 0.5
