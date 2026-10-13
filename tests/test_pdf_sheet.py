"""Vector-PDF sheets: scale detection, label positions in metres, auto-calibration, sheet interpretation.
Automated + MOCKED AI (no network)."""

import io
import json

import httpx
import pymupdf
import pytest

from tests.ai_mock import Recorder, responses_payload, sample_interpretation
from tests.test_api import make_client, new_project
from twin.ai.client import AIClient
from twin.ai.interpret import interpret_plan
from twin.config import load_settings
from twin.fixtures import SAMPLE_BRIEF
from twin.imaging.pdf_text import calibration_for, detect_scale, extract_pdf_text, labels_in_metres
from twin.imaging.preprocess import normalize_upload

A3 = (1190.55, 841.89)  # points


def make_sheet() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=A3[0], height=A3[1])
    page.draw_rect(pymupdf.Rect(100, 100, 700, 600), width=4)
    page.insert_text((200, 300), "Kitchen", fontsize=12)          # baseline at y=300 pt
    page.insert_text((200, 314), "12 SQM", fontsize=10)
    page.insert_text((900, 780), "SCALE 1:100 @ A3", fontsize=10)  # title block
    return doc.tobytes()


def test_detect_scale():
    assert detect_scale("SCALE 1:100 @ A1 ... 1:100")[0] == 100
    assert detect_scale("ratio 1:3 and 21:100")[0] is None   # not a drawing scale
    n, note = detect_scale("plan 1:50, detail 1:20, 1:50")
    assert n == 50 and "1:20" in note


def test_labels_are_converted_to_full_scale_metres():
    info = extract_pdf_text(make_sheet())
    assert info["scale"] == 100
    k = 0.0254 / 72 * 100
    assert info["m_per_pt"] == pytest.approx(k)
    labels = labels_in_metres(info)
    kitchen = next(lab for lab in labels if lab["text"].startswith("Kitchen"))
    assert "12 SQM" in kitchen["text"]                       # lines of one block are joined
    raw = next(lab for lab in info["labels"] if lab["text"].startswith("Kitchen"))
    assert 200 < raw["x_pt"] < 260 and 285 < raw["y_pt"] < 320    # block centre on the sheet
    assert kitchen["x"] == pytest.approx(raw["x_pt"] * k, abs=0.01)
    assert kitchen["y"] == pytest.approx((A3[1] - raw["y_pt"]) * k, abs=0.01)   # sheet y points down
    inside = labels_in_metres(info, (0, 0, 30, 30))
    assert all("SCALE" not in lab["text"] for lab in inside)  # title block falls outside the region


def test_calibration_matches_raster(tmp_path):
    data = make_sheet()
    norm = normalize_upload(data, tmp_path)
    cal = calibration_for(extract_pdf_text(data), norm.width)
    sheet_w_m = A3[0] * 0.0254 / 72 * 100
    assert cal["m_per_px"] * norm.width == pytest.approx(sheet_w_m)
    assert cal["origin_py"] == pytest.approx(norm.height, abs=1.0)


def test_pdf_upload_is_auto_calibrated(tmp_path):
    c = make_client(tmp_path)
    pid = new_project(c)
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"}, files={"file": ("sheet.pdf", make_sheet(), "application/pdf")})
    rec = r.json()
    assert r.status_code == 201 and rec["pdf_scale"] == 100 and rec["calibration"]["source"].startswith("PDF scale 1:100")


def test_sheet_interpretation_crops_and_sends_labels(tmp_path):
    data = make_sheet()
    norm = normalize_upload(data, tmp_path)
    info = extract_pdf_text(data)
    region = {"found": True, "x0": 0.05, "y0": 0.08, "x1": 0.62, "y1": 0.75, "note": "plan"}
    rec = Recorder(httpx.Response(200, json=responses_payload(region)),
                   httpx.Response(200, json=responses_payload(sample_interpretation())))
    s = load_settings(env={"TWIN_AI_API_KEY": "sk-test-NOT-REAL-123456789", "TWIN_AI_MODEL": "m",
                           "TWIN_AI_BASE_URL": "https://ai.example.test/v1", "TWIN_AI_MAX_RETRIES": "0"}, env_file=None).ai
    out = interpret_plan(AIClient(s, transport=rec.transport()), SAMPLE_BRIEF, "Sheet", norm.normalized,
                         {"kind": "pdf", "pdf_text": info})
    assert len(rec.requests) == 2
    locate, main = (json.loads(r.content) for r in rec.requests)
    assert locate["max_output_tokens"] == 2000
    texts = [c.get("text", "") for c in main["input"][0]["content"]]
    frame = next(t for t in texts if t.startswith("COORDINATE FRAME"))
    assert "1:100" in frame and "bottom-left corner of the drawing sheet" in frame
    labels = next(t for t in texts if t.startswith("PDF TEXT LABELS"))
    assert "Kitchen | 12 SQM" in labels and "SCALE 1:100" not in labels
    assert len(out.scene.rooms) == 5
