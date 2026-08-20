import json, os, subprocess, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import progress, render_cache

HERE = Path(__file__).parent
PY   = str(HERE / ".venv/bin/python")
CLI  = str(HERE / "spatialize_cli.py")
OUT  = HERE / "renders"; OUT.mkdir(exist_ok=True)
UP   = HERE / "uploads"; UP.mkdir(exist_ok=True)
CFG  = HERE / "config.json"
EXT  = {".flac", ".wav", ".aif", ".aiff", ".m4a", ".mp3"}
SKIP = {".venv", "renders", "stems", "hrtf", "static", "__pycache__", ".git", "node_modules", "site-packages"}
DEFAULT_RENDER_BUDGET_GB = 10.0

def _cfg():
    if CFG.exists():
        try:
            return json.loads(CFG.read_text())
        except ValueError:
            pass
    return {}

def music_dirs():
    dirs = [UP]
    cfg = _cfg()
    if cfg:
        dirs += [Path(p).expanduser() for p in cfg.get("dirs", [])]
    else:
        dirs.append(Path.home() / "Music")
    return dirs

def render_budget_bytes():
    return int(float(_cfg().get("max_render_gb", DEFAULT_RENDER_BUDGET_GB)) * (1 << 30))

_index = []
def reindex():
    global _index
    out, seen = [], set()
    for d in music_dirs():
        if not d.exists(): continue
        for p in d.rglob("*"):
            if any(part in SKIP for part in p.parts): continue
            try:
                if not (p.is_file() and p.suffix.lower() in EXT): continue
                key = str(p.resolve())
                if key in seen: continue
                seen.add(key)
                out.append({"path": str(p), "name": p.stem, "dir": p.parent.name,
                            "ext": p.suffix.lower().lstrip("."), "mb": round(p.stat().st_size / 1e6, 1)})
            except OSError:
                pass
    out.sort(key=lambda t: t["name"].lower())
    _index = out
    return len(out)

# Jobs are keyed by render key, not by a random id, so a second click on the
# same track joins the job already running instead of starting a rival one.
jobs = {}
jobs_lock = threading.Lock()
RENDER_LOCK = threading.Lock()
_gen = 0
_gen_lock = threading.Lock()
@asynccontextmanager
async def lifespan(_app):
    # Nothing is rendering yet, so any render without metadata is unreachable:
    # random-id files from before the cache was content-addressed, and audio
    # left behind by a render that was killed before its sidecar was written.
    dropped = render_cache.sweep_orphans(OUT)
    if dropped:
        freed = sum(b for _, b in dropped) / 1e9
        print("swept %d unreachable render(s), freed %.2f GB" % (len(dropped), freed), flush=True)
    _prune_renders()
    yield


app = FastAPI(title="DiveAudio", lifespan=lifespan)

class RenderReq(BaseModel):
    path: str
    preset: str = "natural"


def _resolve(path, preset):
    sha = render_cache.source_sha256(path)
    return render_cache.render_key(sha, preset), sha


def worker(key, path, preset, mygen):
    j = jobs[key]
    with RENDER_LOCK:
        if mygen != _gen:
            j["stage"] = "cancelled"
            return
        _work(key, path, preset, j)


def _work(key, path, preset, j):
    tracker = j["tracker"]
    audio, _ = render_cache.paths(OUT, key)
    proc = None
    try:
        j["stage"] = "running"
        proc = subprocess.Popen(
            [PY, "-u", CLI, path, "--preset", preset, "--out", str(audio)],
            cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        j["proc"] = proc
        for line in proc.stdout:
            tracker.feed(line)
        proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(tracker.log[-12:]) or ("exit %d" % proc.returncode)
            j.update(stage="error", error=tail)
            return
        meta = tracker.result or {}
        meta.setdefault("bytes", audio.stat().st_size if audio.exists() else 0)
        meta["key"], meta["preset"], meta["source"] = key, preset, path
        render_cache.write_meta(OUT, key, meta)
        j.update(stage="done", meta=meta)
        _prune_renders(protect={key})
    except Exception as e:
        j.update(stage="error", error=str(e))
    finally:
        j.pop("proc", None)


def _prune_renders(protect=()):
    keep = set(protect) | {k for k, v in jobs.items() if v.get("stage") in ("queued", "running")}
    dropped = render_cache.prune(OUT, render_budget_bytes(), protect=keep)
    if dropped:
        print("pruned %d cached render(s): %s" % (len(dropped), ", ".join(dropped)), flush=True)


def _snapshot(key, j):
    """The whole job state the UI needs, in one payload."""
    if j.get("stage") in ("done", "error", "cancelled") and not j.get("tracker"):
        return {"stage": j["stage"], "key": key, "meta": j.get("meta", {}),
                "error": j.get("error"), "cached": j.get("cached", False), "pct": 1.0}
    snap = j["tracker"].snapshot()
    snap.update(key=key, cached=j.get("cached", False))
    if j.get("stage") in ("error", "cancelled"):
        snap["stage"] = j["stage"]
        snap["error"] = j.get("error")
    elif j.get("stage") == "done":
        snap["stage"] = "done"
        snap["pct"] = 1.0
        snap["meta"] = j.get("meta", {})
    return snap


@app.get("/api/tracks")
def tracks(q: str = ""):
    if not _index: reindex()
    ql = q.lower().strip()
    r = [t for t in _index if not ql or ql in t["name"].lower() or ql in t["dir"].lower()]
    return {"count": len(_index), "results": r[:200]}

@app.post("/api/reindex")
def do_reindex():
    return {"count": reindex()}

@app.get("/api/dirs")
def get_dirs():
    return {"dirs": [str(d) for d in music_dirs()]}

class DirsReq(BaseModel):
    dirs: list[str]

@app.post("/api/dirs")
def set_dirs(r: DirsReq):
    cfg = _cfg(); cfg["dirs"] = r.dirs
    CFG.write_text(json.dumps(cfg, indent=2))
    return {"count": reindex(), "dirs": r.dirs}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = os.path.basename(file.filename or "upload")
    suf = Path(raw).suffix.lower()
    if suf not in EXT:
        return JSONResponse({"error": "unsupported format: " + (suf or "none")}, status_code=400)
    dest, i = UP / raw, 1
    while dest.exists():
        dest = UP / (Path(raw).stem + "-" + str(i) + suf); i += 1
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk: break
            f.write(chunk); size += len(chunk)
    reindex()
    return {"path": str(dest), "name": dest.stem, "dir": "uploads", "bytes": size}


@app.post("/api/resolve")
def resolve(r: RenderReq):
    """Is this track already rendered? Answers without starting any work, so
    restoring a session on page load can never kick off a four-minute job."""
    if not Path(r.path).exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    key, _ = _resolve(r.path, r.preset)
    meta = render_cache.lookup(OUT, key)
    with jobs_lock:
        running = jobs.get(key, {}).get("stage") in ("queued", "running")
    return {"key": key, "cached": meta is not None, "running": running,
            "meta": meta or {}}


@app.post("/api/render")
def render(r: RenderReq):
    if not Path(r.path).exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    key, _ = _resolve(r.path, r.preset)

    # Already on disk: this is the whole point of the content-addressed key.
    meta = render_cache.lookup(OUT, key)
    if meta is not None:
        render_cache.touch(OUT, key)
        with jobs_lock:
            jobs[key] = {"stage": "done", "meta": meta, "cached": True,
                         "path": r.path, "preset": r.preset, "tracker": None}
        return {"job": key, "stage": "done", "cached": True, "meta": meta}

    with jobs_lock:
        live = jobs.get(key)
        if live and live.get("stage") in ("queued", "running"):
            return {"job": key, "stage": live["stage"], "cached": False, "joined": True}

        global _gen
        with _gen_lock:
            _gen += 1
            mygen = _gen
        jobs[key] = {"stage": "queued", "path": r.path, "preset": r.preset,
                     "tracker": progress.Tracker(), "cached": False}
    threading.Thread(target=worker, args=(key, r.path, r.preset, mygen), daemon=True).start()
    return {"job": key, "stage": "queued", "cached": False}


@app.get("/api/job/{key}")
def job(key: str):
    j = jobs.get(key)
    if j is None:
        meta = render_cache.lookup(OUT, key)
        if meta is not None:
            return {"stage": "done", "key": key, "cached": True, "pct": 1.0, "meta": meta}
        return {"stage": "unknown", "key": key}
    return _snapshot(key, j)


@app.get("/api/audio/{key}")
def audio(key: str):
    path, _ = render_cache.paths(OUT, key)
    if not path.exists():
        return JSONResponse({"error": "not ready"}, status_code=409)
    render_cache.touch(OUT, key)
    return FileResponse(path, media_type="audio/flac")


@app.get("/api/env/{key}")
def envelopes(key: str):
    meta = (jobs.get(key) or {}).get("meta") or render_cache.lookup(OUT, key) or {}
    f = meta.get("env_file")
    if not f or not Path(f).exists():
        return JSONResponse({"error": "unavailable"}, status_code=404)
    return FileResponse(f, media_type="application/json")


@app.get("/api/download/{key}")
def download(key: str, fmt: str = "flac"):
    src, _ = render_cache.paths(OUT, key)
    if not src.exists():
        return JSONResponse({"error": "not ready"}, status_code=409)
    meta = (jobs.get(key) or {}).get("meta") or render_cache.lookup(OUT, key) or {}
    base = Path(meta.get("source", "track")).stem
    if fmt == "m4a":
        conv = "/usr/bin/afconvert"
        if not os.path.exists(conv):
            return JSONResponse({"error": "afconvert unavailable"}, status_code=503)
        dst = str(src.with_suffix(".m4a"))
        if not Path(dst).exists():
            r = subprocess.run([conv, "-f", "m4af", "-d", "aac", "-b", "256000",
                                str(src), dst], capture_output=True)
            if r.returncode != 0:
                return JSONResponse({"error": (r.stderr or b"").decode()[:300]},
                                    status_code=500)
        return FileResponse(dst, media_type="audio/mp4",
                            filename=base + " (binaural).m4a")
    return FileResponse(src, media_type="audio/flac",
                        filename=base + " (binaural).flac")


@app.get("/")
def root():
    return FileResponse(HERE / "static/index.html")

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
