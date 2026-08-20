"""Clicking a track twice must not render it twice."""
import json
import os
import time

import pytest

import render_cache as rc


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "song.flac"
    p.write_bytes(b"pretend audio" * 1000)
    return p


# ------------------------------------------------------------------- keying --
def test_same_content_at_a_different_path_gives_the_same_key(tmp_path, src):
    twin = tmp_path / "elsewhere" / "renamed.flac"
    twin.parent.mkdir()
    twin.write_bytes(src.read_bytes())
    assert rc.source_sha256(src) == rc.source_sha256(twin)
    assert rc.render_key(rc.source_sha256(src), "default") == \
           rc.render_key(rc.source_sha256(twin), "default")


def test_different_content_gives_a_different_key(tmp_path, src):
    other = tmp_path / "other.flac"
    other.write_bytes(b"different audio" * 1000)
    assert rc.source_sha256(src) != rc.source_sha256(other)


def test_preset_is_part_of_the_key(src):
    sha = rc.source_sha256(src)
    keys = {rc.render_key(sha, p) for p in ("subtle", "natural", "default", "wide")}
    assert len(keys) == 4, "presets must not share a render"


def test_dsp_version_bump_invalidates_old_renders(src):
    sha = rc.source_sha256(src)
    assert rc.render_key(sha, "default", 1) != rc.render_key(sha, "default", 2)


def test_hash_is_memoised_but_notices_an_edit(src):
    first = rc.source_sha256(src)
    assert rc.source_sha256(src) is first or rc.source_sha256(src) == first
    time.sleep(0.01)
    src.write_bytes(b"edited audio" * 1000)
    os.utime(src, (time.time() + 1, time.time() + 1))
    assert rc.source_sha256(src) != first, "an edited file reused a stale hash"


# ------------------------------------------------------------------ lookup --
def test_lookup_misses_when_nothing_is_stored(tmp_path):
    assert rc.lookup(tmp_path, "deadbeef") is None


def test_lookup_hits_a_complete_render(tmp_path):
    audio, _ = rc.paths(tmp_path, "k1")
    audio.write_bytes(b"x" * 4096)
    rc.write_meta(tmp_path, "k1", {"bytes": 4096, "sr": 96000})
    assert rc.lookup(tmp_path, "k1")["sr"] == 96000


def test_a_truncated_render_is_not_served_as_finished(tmp_path):
    """A render killed mid-write leaves a short file next to good metadata."""
    audio, _ = rc.paths(tmp_path, "k2")
    audio.write_bytes(b"x" * 4096)
    rc.write_meta(tmp_path, "k2", {"bytes": 999999})
    assert rc.lookup(tmp_path, "k2") is None


def test_metadata_without_audio_is_a_miss(tmp_path):
    rc.write_meta(tmp_path, "k3", {"bytes": 10})
    assert rc.lookup(tmp_path, "k3") is None


# ------------------------------------------------------------------- prune --
def _render(tmp_path, key, size, age):
    audio, _ = rc.paths(tmp_path, key)
    audio.write_bytes(b"x" * size)
    rc.write_meta(tmp_path, key, {"bytes": size})
    when = time.time() - age
    for f in rc.paths(tmp_path, key):
        os.utime(f, (when, when))


def test_prune_is_a_no_op_under_budget(tmp_path):
    _render(tmp_path, "a", 1000, 100)
    assert rc.prune(tmp_path, 1 << 20) == []
    assert rc.lookup(tmp_path, "a") is not None


def test_prune_drops_least_recently_used_first(tmp_path):
    _render(tmp_path, "old", 4000, 900)
    _render(tmp_path, "mid", 4000, 500)
    _render(tmp_path, "new", 4000, 10)
    removed = rc.prune(tmp_path, 9000)
    assert removed == ["old"]
    assert rc.lookup(tmp_path, "new") is not None
    assert rc.lookup(tmp_path, "old") is None


def test_prune_removes_every_file_of_an_entry(tmp_path):
    _render(tmp_path, "old", 4000, 900)
    m4a = tmp_path / "old.m4a"
    m4a.write_bytes(b"y" * 100)
    old = time.time() - 900
    os.utime(m4a, (old, old))          # the transcode is as stale as its render
    _render(tmp_path, "new", 4000, 10)
    rc.prune(tmp_path, 4500)
    assert not (tmp_path / "old.m4a").exists(), "left a stray transcode behind"


def test_prune_never_evicts_a_protected_render(tmp_path):
    """The track currently playing must not be deleted out from under it."""
    _render(tmp_path, "playing", 8000, 9999)
    _render(tmp_path, "new", 4000, 1)
    removed = rc.prune(tmp_path, 1000, protect={"playing"})
    assert "playing" not in removed
    assert rc.lookup(tmp_path, "playing") is not None


def test_touch_refreshes_lru_position(tmp_path):
    _render(tmp_path, "a", 4000, 900)
    _render(tmp_path, "b", 4000, 500)
    rc.touch(tmp_path, "a")
    assert rc.prune(tmp_path, 5000) == ["b"]


# ------------------------------------------------------------------ orphans --
def test_sweep_removes_audio_with_no_metadata(tmp_path):
    """Renders from the old random-id scheme can never be served again."""
    (tmp_path / "04af56a95786.flac").write_bytes(b"x" * 5000)
    dropped = rc.sweep_orphans(tmp_path)
    assert [n for n, _ in dropped] == ["04af56a95786.flac"]
    assert not (tmp_path / "04af56a95786.flac").exists()


def test_sweep_keeps_a_valid_render(tmp_path):
    _render(tmp_path, "good", 4000, 5)
    assert rc.sweep_orphans(tmp_path) == []
    assert rc.lookup(tmp_path, "good") is not None


def test_sweep_removes_a_half_written_render(tmp_path):
    """Audio present, sidecar never written: killed mid-encode."""
    (tmp_path / "abcdef0123456789.flac").write_bytes(b"x" * 5000)
    rc.sweep_orphans(tmp_path)
    assert not (tmp_path / "abcdef0123456789.flac").exists()


def test_sweep_leaves_unrelated_files_alone(tmp_path):
    (tmp_path / "notes.txt").write_text("keep me")
    _render(tmp_path, "good", 4000, 5)
    rc.sweep_orphans(tmp_path)
    assert (tmp_path / "notes.txt").exists()
