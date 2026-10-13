"""REAL end-to-end UI test: headless Microsoft Edge (Playwright) drives the app against a live
server with a temporary data dir, and generation runs REAL Blender.

Markers: `ui` (needs Playwright + Edge) and `blender`. Screenshots go to tests/_out/e2e/.
"""

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from twin.config import load_settings

REAL = load_settings()
OUT = Path(__file__).parent / "_out" / "e2e"

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe").is_file()
except ImportError:  # pragma: no cover
    HAVE_PW = False

pytestmark = [
    pytest.mark.ui, pytest.mark.blender,
    pytest.mark.skipif(not HAVE_PW, reason="Playwright or Microsoft Edge not available"),
    pytest.mark.skipif(not REAL.blender_path or not Path(REAL.blender_path).is_file(), reason="Blender not configured"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn
    from twin.api.app import create_app
    data = tmp_path_factory.mktemp("ui_data")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = load_settings(env={"TWIN_DATA_DIR": str(data), "TWIN_BLENDER_PATH": REAL.blender_path,
                                  "TWIN_UNREAL_ENGINE_DIR": str(data / "no-ue")}, env_file=None)
    srv = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base
    srv.should_exit = True
    t.join(timeout=10)


def test_full_flow_in_browser(server):
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{server}/app/")

        # create a project through the dialog
        page.click("#emptyNewBtn")
        page.fill("dialog input[name=name]", "E2E flat")
        page.click("dialog button[value=ok]")
        page.wait_for_selector("text=Project brief")

        # load the known-measurement layout -> review tab
        page.click("text=Use sample apartment")
        page.wait_for_selector("text=ready to generate", timeout=15000)
        assert page.locator("svg text", has_text="Bedroom").count() == 1
        assert page.locator("svg text", has_text="3.85 × 3.45 m").count() == 1
        page.screenshot(path=str(OUT / "1_review.png"))

        # draw a room in empty space, then undo it
        page.click("text=Draw room")
        box = page.locator(".editor-wrap svg").bounding_box()
        x, y = box["x"] + box["width"] - 60, box["y"] + 60
        page.mouse.move(x - 120, y)
        page.mouse.down()
        page.mouse.move(x, y + 100, steps=5)
        page.mouse.up()
        page.click("dialog button[value=ok]")
        page.wait_for_selector("text=Room R6")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(600)
        assert page.locator("svg text", has_text="Room 6").count() == 0

        # select the bedroom via the issues-free path: click its label area, then edit the name
        page.locator("svg polygon.svg-room").nth(2).click(position={"x": 30, "y": 30})
        page.wait_for_selector("h2:has-text('Room R3')")
        name = page.locator(".inspector .kv input").first
        name.fill("Main bedroom")
        name.press("Enter")
        page.wait_for_timeout(500)
        page.click("text=Save version")
        page.fill("dialog input[name=note]", "renamed bedroom")
        page.click("dialog button[value=ok]")
        page.wait_for_selector("text=Saved version 2", timeout=10000)

        # generate with real Blender and wait for the verified build
        page.click("button[data-tab=generate]")
        page.uncheck("text=render preview images")
        page.get_by_role("button", name="Generate", exact=True).click()
        try:
            page.wait_for_selector("text=3D generation completed and verified", timeout=300000)
        except Exception:
            page.screenshot(path=str(OUT / "fail_generate.png"))
            raise
        page.wait_for_selector(".viewer canvas", timeout=20000)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "2_generate.png"))
        assert page.locator("text=Blender project (.blend)").count() == 1
        assert page.locator("text=max deviation").count() == 1

        # click into the 3D view to pick an object
        canvas = page.locator(".viewer canvas").bounding_box()
        page.mouse.click(canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] / 2)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "3_pick.png"))
        browser.close()
    real_errors = [e for e in errors if "favicon" not in e]
    assert real_errors == [], real_errors
