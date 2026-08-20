"""The progress channel has to survive real subprocess output: interleaved
human lines, demucs' own chatter, and stages that get skipped by the cache."""
import json
import pytest

import progress


PLAN = {"stage": "plan", "stages": [
    {"id": "decode", "weight": 10},
    {"id": "separate", "weight": 60},
    {"id": "convolve", "weight": 20},
    {"id": "encode", "weight": 10},
]}


def line(payload):
    return progress.MARK + json.dumps(payload)


@pytest.fixture
def tracker():
    ticks = [0.0]

    def clock():
        return ticks[0]

    t = progress.Tracker(clock=clock)
    t.tick = lambda dt: ticks.__setitem__(0, ticks[0] + dt)
    return t


# ------------------------------------------------------------------ parsing --
def test_parse_ignores_ordinary_output():
    assert progress.parse("separating (htdemucs, shifts=0)") is None
    assert progress.parse("") is None


def test_parse_survives_malformed_payloads():
    assert progress.parse(progress.MARK + "{not json") is None
    assert progress.parse(progress.MARK + '"a string"') is None


def test_parse_reads_a_marked_line():
    assert progress.parse(line({"stage": "decode", "frac": 0.5})) == {
        "stage": "decode", "frac": 0.5}


# ------------------------------------------------------------------ tracking --
def test_unmarked_lines_become_the_log_tail(tracker):
    for i in range(60):
        tracker.feed("demucs chatter %d" % i)
    snap = tracker.snapshot()
    assert len(snap["log"]) == progress.Tracker.LOG_LINES
    assert snap["log"][-1] == "demucs chatter 59"
    assert snap["pct"] == 0.0


def test_percent_advances_within_and_across_stages(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "decode", "frac": 1.0}))
    assert tracker.snapshot()["pct"] == pytest.approx(0.10)
    tracker.feed(line({"stage": "separate", "frac": 0.5}))
    assert tracker.snapshot()["pct"] == pytest.approx(0.10 + 0.30)


def test_a_skipped_stage_does_not_strand_the_bar(tracker):
    """Cached stems mean separation never reports. Jumping straight to
    convolve must credit the skipped weight, not sit at 10%."""
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "decode", "frac": 1.0}))
    tracker.feed(line({"stage": "convolve", "frac": 0.0}))
    assert tracker.snapshot()["pct"] == pytest.approx(0.70)


def test_progress_never_goes_backwards_within_a_stage(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "separate", "frac": 0.8}))
    tracker.feed(line({"stage": "separate", "frac": 0.3}))
    assert tracker.snapshot()["frac"] == pytest.approx(0.8)


def test_plan_issued_before_any_progress_gives_exact_weights(tracker):
    """The normal path: the CLI knows its cache state before it reports a
    stage, so the arithmetic is exact and the clamp never engages."""
    tracker.feed(line({"stage": "plan", "stages": [
        {"id": "decode", "weight": 20},
        {"id": "convolve", "weight": 50},
        {"id": "encode", "weight": 30},
    ]}))
    tracker.feed(line({"stage": "convolve", "frac": 0.5}))
    assert tracker.snapshot()["pct"] == pytest.approx(0.20 + 0.5 * 0.50)


def test_detail_fields_reach_the_snapshot(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "convolve", "frac": 0.5,
                       "blocks": 5000, "total": 10000, "batch": 409}))
    d = tracker.snapshot()["detail"]
    assert d["blocks"] == 5000 and d["batch"] == 409
    assert "frac" not in d


def test_label_is_human_readable(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "convolve", "frac": 0.1}))
    assert tracker.snapshot()["label"] == "HRTF convolution"


def test_result_completes_the_job(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "result", "meta": {"sr": 96000}}))
    snap = tracker.snapshot()
    assert snap["stage"] == "done" and snap["pct"] == 1.0
    assert tracker.result == {"sr": 96000}
    assert snap["eta"] is None


def test_error_is_captured(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "error", "message": "boom"}))
    assert tracker.snapshot()["stage"] == "error"
    assert tracker.error == "boom"


# ---------------------------------------------------------------------- eta --
def test_eta_withheld_until_meaningful(tracker):
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "decode", "frac": 0.01}))
    assert tracker.snapshot()["eta"] is None      # too early to guess


def test_eta_extrapolates_from_elapsed(tracker):
    tracker.feed(line(PLAN))
    tracker.tick(10.0)
    tracker.feed(line({"stage": "separate", "frac": 0.5}))   # 40% done in 10s
    eta = tracker.snapshot()["eta"]
    assert eta == pytest.approx(15.0, rel=0.01)              # 60% left -> 15s


def test_revised_plan_does_not_move_the_bar_backwards(tracker):
    """The CLI issues a provisional plan, then revises it once it knows the
    stems are cached. Mid-flight revision must not rewind the ring."""
    tracker.feed(line(PLAN))
    tracker.feed(line({"stage": "decode", "frac": 1.0}))
    tracker.feed(line({"stage": "convolve", "frac": 0.5}))
    before = tracker.snapshot()["pct"]
    tracker.feed(line({"stage": "plan", "stages": [
        {"id": "decode", "weight": 20},
        {"id": "convolve", "weight": 50},
        {"id": "encode", "weight": 30},
    ]}))
    after = tracker.snapshot()["pct"]
    assert after >= before - 1e-9, "the ring rewound on a plan revision"
