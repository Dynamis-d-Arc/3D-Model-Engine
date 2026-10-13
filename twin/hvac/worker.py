"""Run one HVAC import in its own process (the API calls this through subprocess).

    python -m twin.hvac.worker SPEC.json

SPEC: {"hvac_pdf", "scene", "out", "cache_dir", "file_id", "file_name", "manual",
       "plan": {"pdf", "page_h_pt", "m_per_pt", "cache_dir", "scale"} | null}
Progress is printed as JSON lines {"progress": f, "stage": s}; the result (hvac + report) is written to
SPEC["out"]. OCR (OpenCV / ONNX Runtime) runs native code that must not be able to take the API server
down, so it never runs inside the server process.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    out = Path(spec["out"])
    try:
        from twin.hvac.importer import HvacImportError, PlanSheetInfo, import_hvac
        from twin.scene.model import Scene
        scene = Scene.model_validate_json(Path(spec["scene"]).read_text(encoding="utf-8"))
        plan = None
        if spec.get("plan"):
            p = spec["plan"]
            plan = PlanSheetInfo(pdf=Path(p["pdf"]).read_bytes(), page_h_pt=p["page_h_pt"], m_per_pt=p["m_per_pt"],
                                 cache_dir=Path(p["cache_dir"]) if p.get("cache_dir") else None, scale=p.get("scale"))

        def progress(f: float, stage: str) -> None:
            print(json.dumps({"progress": round(f, 3), "stage": stage}), flush=True)

        res = import_hvac(Path(spec["hvac_pdf"]).read_bytes(), plan, scene, file_id=spec.get("file_id"),
                          file_name=spec.get("file_name", ""),
                          cache_dir=Path(spec["cache_dir"]) if spec.get("cache_dir") else None,
                          manual=spec.get("manual"), on_progress=progress)
        out.write_text(json.dumps({"ok": True, "hvac": res.hvac.model_dump(mode="json"), "report": res.report}),
                       encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001 - report every failure to the API
        code = getattr(exc, "code", None) if "HvacImportError" in type(exc).__name__ else None
        out.write_text(json.dumps({"ok": False, "code": code or "HVAC_IMPORT_FAILED", "message": str(exc),
                                   "detail": getattr(exc, "detail", None) or traceback.format_exc()[-3000:]}),
                       encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
