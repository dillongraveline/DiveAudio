"""The browser code has no test runner of its own; tests/check_ui.js checks the
parts that are pure logic (ring geometry, the detail formatter) plus that every
$("#id") in the script resolves to an element. Run it through pytest when node
is available so it is not a step anyone has to remember."""
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parent / "check_ui.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_browser_checks_pass():
    r = subprocess.run(["node", str(CHECK)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
