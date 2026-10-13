"""Developer helper: screenshot the running app with headless Edge (Playwright, no browser download).

    python tests/ui_shots.py [base_url] [out_dir]

Opens the most recent project and captures each tab. Used while developing the UI; the
automated UI test lives in tests/test_ui_e2e.py.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "tests/_out/ui")
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    logs = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    page.goto(f"{BASE}/app/")
    page.wait_for_timeout(1200)
    options = page.locator("#projectSelect option")
    if options.count() > 1:
        page.select_option("#projectSelect", index=1)
        page.wait_for_timeout(1200)
    for tab in ("brief", "review", "generate", "activity"):
        page.click(f"button[data-tab={tab}]")
        page.wait_for_timeout(2500 if tab == "generate" else 1200)
        page.screenshot(path=str(OUT / f"{tab}.png"))
    browser.close()
print("\n".join(logs[-30:]) or "no console output")
print("saved to", OUT)
