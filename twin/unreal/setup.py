"""Compile the Unreal walkthrough template:  python -m twin.unreal.setup

Needs Visual Studio (Build Tools) with the C++ workload, a Windows SDK and the
.NET Framework 4.6+ SDK (e.g. the ".NET Framework 4.8.1 Developer Pack").
"""

import sys
from pathlib import Path

from twin.config import load_settings
from twin.unreal.runner import UnrealError, compile_template, unreal_paths


def main() -> int:
    settings = load_settings()
    try:
        p = unreal_paths(settings)
        print(f"engine : {p.engine_dir}\nproject: {p.project}\ncompiling (first build takes a few minutes)...")
        log = Path(p.project.parent / "Saved" / "Logs" / "twin_compile.log")
        compile_template(settings, log)
    except UnrealError as exc:
        print(f"FAILED {exc.code}: {exc}")
        if exc.detail.get("log_tail"):
            print(exc.detail["log_tail"])
        return 1
    print(f"OK: {p.module_dll}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
