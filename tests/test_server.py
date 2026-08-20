"""End-to-end behaviour of the render endpoints, with the actual DSP stubbed
out -- what is under test is the caching and job bookkeeping, not the audio."""
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import render_cache


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A server rooted in a temp dir, with spatialize_cli replaced by a stub
    that emits the real progress protocol and writes a plausible render."""
    import server

    renders = tmp_path / "renders"; renders.mkdir()
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(server, "OUT", renders)
    monkeypatch.setattr(server, "UP", uploads)
    monkeypatch.setattr(server, "CFG", tmp_path / "config.json")
    server.jobs.clear()

    runs = tmp_path / "invocations.log"
    stub = tmp_path / "stub_cli.py"
    stub.write_text(
        "import sys, json, os\n"
        "open(%r,'a').write('run\\n')\n" % str(runs) +
        "out = sys.argv[sys.argv.index('--out') + 1]\n"
        "print('@@PROG ' + json.dumps({'stage':'plan','stages':["
        "{'id':'decode','weight':50},{'id':'encode','weight':50}]}))\n"
        "print('ordinary chatter from the renderer')\n"
        "print('@@PROG ' + json.dumps({'stage':'decode','frac':1.0,'src_sr':44100}))\n"
        "open(out,'wb').write(b'FLACDATA' * 200)\n"
        "print('@@PROG ' + json.dumps({'stage':'result','meta':"
        "{'bytes':os.path.getsize(out),'sr':44100,'duration':12.5}}))\n")
    monkeypatch.setattr(server, "PY", sys.executable)
    monkeypatch.setattr(server, "CLI", str(stub))

    song = uploads / "song.flac"
    song.write_bytes(b"audio bytes" * 5000)
    return TestClient(server.app), server, str(song), renders, runs


def wait_done(client, key, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get("/api/job/" + key).json()
        if j["stage"] in ("done", "error", "cancelled"):
            if j["stage"] == "error":
                print("JOB ERROR:", j.get("error"))
            return j
        time.sleep(0.02)
    raise AssertionError("job never finished")


# ------------------------------------------------------------------ resolve --
def test_resolve_reports_a_miss_without_starting_work(app):
    client, server, song, _, runs = app
    r = client.post("/api/resolve", json={"path": song, "preset": "default"}).json()
    assert r["cached"] is False and r["running"] is False
    assert server.jobs == {}, "resolving must never queue a render"


def test_resolve_rejects_a_missing_file(app):
    client, _, _, _, _ = app
    r = client.post("/api/resolve", json={"path": "/nope.flac", "preset": "default"})
    assert r.status_code == 404


# ------------------------------------------------------------------- render --
def test_first_render_runs_and_reports_progress(app):
    client, server, song, renders, runs = app
    r = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    assert r["cached"] is False
    key = r["job"]
    done = wait_done(client, key)
    assert done["stage"] == "done"
    assert done["meta"]["sr"] == 44100
    assert (renders / (key + ".flac")).exists()
    assert (renders / (key + ".json")).exists()


def test_second_render_is_a_cache_hit_and_starts_no_job(app):
    client, server, song, renders, runs = app
    first = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    wait_done(client, first["job"])
    before = (renders / (first["job"] + ".flac")).read_bytes()
    assert runs.read_text().count("run") == 1

    server.jobs.clear()                      # as if the server had restarted
    second = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    assert second["cached"] is True
    assert second["stage"] == "done"
    assert second["job"] == first["job"], "same content must map to the same render"
    assert runs.read_text().count("run") == 1, "a cache hit ran the renderer again"
    assert (renders / (first["job"] + ".flac")).read_bytes() == before


def test_a_different_preset_renders_separately(app):
    client, _, song, _, runs = app
    a = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    wait_done(client, a["job"])
    b = client.post("/api/render", json={"path": song, "preset": "wide"}).json()
    assert b["job"] != a["job"] and b["cached"] is False


def test_renaming_the_file_still_hits_the_cache(app, tmp_path):
    """The key is the content, so a moved or renamed file is not a new render."""
    client, _, song, _, runs = app
    first = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    wait_done(client, first["job"])
    twin = tmp_path / "uploads" / "renamed.flac"
    twin.write_bytes(open(song, "rb").read())
    second = client.post("/api/render", json={"path": str(twin), "preset": "default"}).json()
    assert second["cached"] is True and second["job"] == first["job"]
    assert runs.read_text().count("run") == 1, "a rename triggered a re-render"


def test_concurrent_clicks_join_one_job(app):
    client, server, song, _, runs = app
    first = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    second = client.post("/api/render", json={"path": song, "preset": "default"}).json()
    assert second["job"] == first["job"]
    assert second.get("joined") or second.get("cached"), "a rival job was started"
    wait_done(client, first["job"])
    assert runs.read_text().count("run") == 1, "two renderers ran for one track"


# --------------------------------------------------------------- progress ---
def test_job_payload_carries_progress_detail(app):
    client, server, song, _, runs = app
    key = client.post("/api/render", json={"path": song, "preset": "default"}).json()["job"]
    done = wait_done(client, key)
    assert done["pct"] == 1.0
    assert any("ordinary chatter" in line for line in done.get("log", [])), \
        "plain renderer output should survive as a log tail"


def test_unknown_job_is_reported_not_crashed(app):
    client, _, _, _, _ = app
    assert client.get("/api/job/nosuchkey").json()["stage"] == "unknown"


def test_job_survives_a_server_restart_via_disk(app):
    """jobs{} is memory; the render is on disk. After a restart the key must
    still resolve, which is what makes session restore work."""
    client, server, song, _, runs = app
    key = client.post("/api/render", json={"path": song, "preset": "default"}).json()["job"]
    wait_done(client, key)
    server.jobs.clear()
    j = client.get("/api/job/" + key).json()
    assert j["stage"] == "done" and j["cached"] is True


# --------------------------------------------------------------- delivery ---
def test_audio_and_download_serve_the_cached_render(app):
    client, _, song, _, runs = app
    key = client.post("/api/render", json={"path": song, "preset": "default"}).json()["job"]
    wait_done(client, key)
    assert client.get("/api/audio/" + key).status_code == 200
    assert client.get("/api/download/" + key + "?fmt=flac").status_code == 200


def test_audio_for_an_unrendered_key_is_409(app):
    client, _, _, _, _ = app
    assert client.get("/api/audio/deadbeefdeadbeef").status_code == 409


def test_render_failure_is_surfaced_with_output(app, tmp_path, monkeypatch):
    client, server, song, _, runs = app
    bad = tmp_path / "bad_cli.py"
    bad.write_text("import sys\nprint('boom: could not decode')\nsys.exit(3)\n")

    monkeypatch.setattr(server, "CLI", str(bad))
    key = client.post("/api/render", json={"path": song, "preset": "default"}).json()["job"]
    done = wait_done(client, key)
    assert done["stage"] == "error"
    assert "boom" in done["error"]


# ---------------------------------------------------------------- Off mode --
def test_source_serves_a_library_file(app):
    """'Off' plays the original file, so it has to be reachable."""
    client, _, song, _, _ = app
    client.post("/api/reindex")
    r = client.get("/api/source", params={"path": song})
    assert r.status_code == 200


def test_source_refuses_a_file_outside_the_library(app, tmp_path):
    """The server binds 0.0.0.0 with no auth; a path parameter must not be a
    way to read arbitrary files off the machine."""
    client, _, _, _, _ = app
    client.post("/api/reindex")
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    assert client.get("/api/source", params={"path": str(secret)}).status_code == 403


def test_source_refuses_a_traversal(app):
    client, _, song, _, _ = app
    client.post("/api/reindex")
    r = client.get("/api/source", params={"path": song + "/../../../../etc/passwd"})
    assert r.status_code in (403, 404)


def test_source_refuses_a_missing_file(app):
    client, _, _, _, _ = app
    assert client.get("/api/source", params={"path": "/nope/none.flac"}).status_code == 404
