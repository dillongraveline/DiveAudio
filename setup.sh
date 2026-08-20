#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  demucs soundfile numpy scipy sofar fastapi "uvicorn[standard]" python-multipart \
  pytest

mkdir -p static
[ -f static/three.min.js ] || \
  curl -sL -o static/three.min.js https://unpkg.com/three@0.160.0/build/three.min.js

# SADIE II binaural database (Apache-2.0) - Neumann KU100 dummy head
if [ ! -d hrtf/D1_HRIR_SOFA ]; then
  curl -sL -o /tmp/D1_HRIR_SOFA.zip \
    "https://zenodo.org/records/10886409/files/D1_HRIR_SOFA.zip?download=1"
  mkdir -p hrtf && unzip -q -o /tmp/D1_HRIR_SOFA.zip -d hrtf && rm /tmp/D1_HRIR_SOFA.zip
fi

echo
echo "done.  start with:  ./.venv/bin/python server.py"
echo "then open http://localhost:8765  (or your LAN IP for other devices)"
