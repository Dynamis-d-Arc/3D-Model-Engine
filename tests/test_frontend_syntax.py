"""Automated: every frontend ES module must parse (node --check). Skipped when Node.js is absent."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS = sorted((Path(__file__).parent.parent / "frontend" / "js").rglob("*.js"))
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
@pytest.mark.parametrize("path", JS, ids=[p.name for p in JS])
def test_module_parses(path, tmp_path):
    mjs = tmp_path / (path.stem + ".mjs")   # .mjs so node parses import/export syntax
    mjs.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    res = subprocess.run([NODE, "--check", str(mjs)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
