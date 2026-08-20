"""Content-addressed cache for finished renders.

The stem cache already keys on file content; renders did not, so every click on
a track produced a fresh job and another copy of the same audio. Keying the
output on (source content, preset, DSP version) makes a repeat click a lookup
instead of a render, and survives a server restart.
"""
import hashlib
import json
import os
from pathlib import Path

# Bump when a change to the DSP would alter the samples it produces, so old
# renders are not served for new code.
PARAMS_VERSION = 1

_sha_cache = {}


def source_sha256(path):
    """Hash a source file, memoised on (path, mtime, size).

    Hashing ~60 MB costs a fraction of a second, but it happens on every click,
    including the ones that turn out to be cache hits.
    """
    st = os.stat(path)
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    hit = _sha_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    digest = h.hexdigest()
    _sha_cache[str(path)] = (stamp, digest)
    return digest


def render_key(src_sha, preset, params_version=PARAMS_VERSION):
    raw = "%s|%s|%d" % (src_sha, preset, params_version)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def paths(root, key):
    root = Path(root)
    return root / (key + ".flac"), root / (key + ".json")


def write_meta(root, key, meta):
    _, mp = paths(root, key)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, mp)


def lookup(root, key):
    """Return stored metadata if this render is complete on disk, else None."""
    audio, mp = paths(root, key)
    if not audio.exists() or not mp.exists():
        return None
    try:
        meta = json.loads(mp.read_text())
    except ValueError:
        return None
    # A truncated render (killed mid-write) must never be served as finished:
    # the stored byte count is what proves the file on disk is the whole render.
    size = audio.stat().st_size
    if size <= 0 or meta.get("bytes") != size:
        return None
    return meta


def prune(root, max_bytes, protect=()):
    """Keep the renders directory under a budget, dropping least-recently-used
    entries whole. Returns the keys removed."""
    root = Path(root)
    if not root.exists():
        return []
    groups = {}
    for f in root.iterdir():
        if not f.is_file():
            continue
        key = f.name.split(".")[0]
        g = groups.setdefault(key, {"bytes": 0, "atime": 0.0, "files": []})
        st = f.stat()
        g["bytes"] += st.st_size
        g["atime"] = max(g["atime"], st.st_mtime)
        g["files"].append(f)

    total = sum(g["bytes"] for g in groups.values())
    removed = []
    # Oldest first; a protected key is never a candidate however old it is.
    for key, g in sorted(groups.items(), key=lambda kv: kv[1]["atime"]):
        if total <= max_bytes:
            break
        if key in protect:
            continue
        for f in g["files"]:
            try:
                f.unlink()
            except OSError:
                pass
        total -= g["bytes"]
        removed.append(key)
    return removed


def touch(root, key):
    """Mark a render as freshly used, so LRU pruning reflects playback."""
    for f in paths(root, key):
        if f.exists():
            os.utime(f, None)


def sweep_orphans(root):
    """Delete render files that no valid metadata claims.

    Renders made before the cache was content-addressed have random names and
    no sidecar, and a render killed mid-write leaves audio with no metadata.
    Neither can ever be served, so both are dead weight. Only safe to call when
    no render is in flight -- the audio file is written before its sidecar.
    """
    root = Path(root)
    if not root.exists():
        return []
    dropped = []
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.suffix not in (".flac", ".m4a"):
            continue
        if lookup(root, f.name.split(".")[0]) is None:
            try:
                size = f.stat().st_size
                f.unlink()
                dropped.append((f.name, size))
            except OSError:
                pass
    return dropped
