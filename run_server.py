"""Start the backend from any working directory:  python run_server.py  (same as `python -m twin.api`)."""

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
runpy.run_module("twin.api", run_name="__main__")
