import json, os, subprocess, sys, threading, uuid
from pathlib import Path
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).parent
PY   = str(HERE / ".venv/bin/python")
OUT  = HERE / "renders"; OUT.mkdir(exist_ok=True)
UP   = HERE / "uploads"; UP.mkdir(exist_ok=True)
CFG  = HERE / "config.json"
EXT  = {".flac", ".wav", ".aif", ".aiff", ".m4a", ".mp3"}
SKIP = {".venv", "renders", "stems", "hrtf", "static", "__pycache__", ".git", "node_modules", "site-packages"}

def music_dirs():
    dirs = [UP]
    if CFG.exists():
        dirs += [Path(p).expanduser() for p in json.loads(CFG.read_text()).get("dirs", [])]
    else:
        dirs.append(Path.home() / "Music")
    return dirs

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

jobs = {}
RENDER_LOCK = threading.Lock()
_gen = 0
_gen_lock = threading.Lock()
app = FastAPI(title="DiveAudio")

class RenderReq(BaseModel):
    path: str
    preset: str = "natural"

def worker(jid, path, preset, mygen):
    j = jobs[jid]
    with RENDER_LOCK:
        if mygen != _gen:
            j.update(stage="cancelled"); return
        _work(jid, path, preset, j)

def _work(jid, path, preset, j):
    try:
        j["stage"] = "separating"
        outp = str(OUT / (jid + ".flac"))
        proc = subprocess.run(
            [PY, str(HERE / "spatialize_cli.py"), path, "--preset", preset, "--out", outp],
            cwd=str(HERE), capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            j.update(stage="error", error=(proc.stderr or "")[-1500:]); return
        try:
            meta = json.loads(proc.stdout[proc.stdout.index("{"):])
        except Exception:
            meta = {}
        j.update(stage="done", meta=meta, file=outp)
    except Exception as e:
        j.update(stage="error", error=str(e))

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
    CFG.write_text(json.dumps({"dirs": r.dirs}, indent=2))
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

@app.post("/api/render")
def render(r: RenderReq):
    if not Path(r.path).exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    global _gen
    with _gen_lock:
        _gen += 1
        mygen = _gen
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"stage": "queued", "path": r.path, "preset": r.preset}
    threading.Thread(target=worker, args=(jid, r.path, r.preset, mygen), daemon=True).start()
    return {"job": jid}

@app.get("/api/job/{jid}")
def job(jid: str):
    return jobs.get(jid, {"stage": "unknown"})

@app.get("/api/audio/{jid}")
def audio(jid: str):
    j = jobs.get(jid) or {}
    if j.get("stage") != "done":
        return JSONResponse({"error": "not ready"}, status_code=409)
    return FileResponse(j["file"], media_type="audio/flac")

@app.get("/")
def root():
    return FileResponse(HERE / "static/index.html")

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
