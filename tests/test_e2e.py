"""Click through the app in a real (windowless) browser and fail on any "Not Found", server error or
page error — the kind of bug a person would otherwise meet first.

Runs against a throwaway library (FILMLOOK_LIBRARY in a temp folder) with a few generated photos,
never your real library or drives. Needs Playwright and its browser in .playwright-browsers
(skipped otherwise). Takes about a minute: `FLS_E2E=1 .venv/bin/python -m pytest tests/test_e2e.py -q`.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = ROOT / ".playwright-browsers"
pytestmark = [pytest.mark.skipif(not BROWSERS.exists(), reason="Playwright's browser isn't installed"),
              pytest.mark.skipif(not os.environ.get("FLS_E2E"), reason="slow: run with FLS_E2E=1 (before every upload)")]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SETUP = r"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
from PIL import Image
from filmlook.library import db, essay, index, marks, scan
shoot = Path(sys.argv[1])
shoot.mkdir(parents=True)
rng = np.random.default_rng(1)
t0 = datetime(2025, 5, 4, 18, 15, 0)
for i in range(6):
    a = (rng.random((40, 60, 3)) * 255).astype(np.uint8)
    im = Image.fromarray(a).resize((1200, 800), Image.BICUBIC)
    ex = Image.Exif(); ex[0x9003] = (t0 + timedelta(seconds=3 * i)).strftime("%Y:%m:%d %H:%M:%S")
    ex[0x010F] = "LEICA CAMERA AG"; ex[0x0110] = "LEICA Q3 43"
    im.save(shoot / f"25-05-04-18-15-{i:02d}.jpg", exif=ex, quality=85)
con = db.connect()
scan.add_root(con, str(shoot))
scan.scan(con)
index.build_thumbnails(con)
index.build_quality(con)
ids = [r[0] for r in con.execute("SELECT id FROM photos ORDER BY capture_time")]
marks.set_mark(con, ids[0], flag="pick", rating=3)
eid = essay.create(con, "E2E test essay", {"type": "photos", "ids": ids}, target=6)
essay.set_order(con, eid, ids)
folder = con.execute("SELECT folder_id FROM photos LIMIT 1").fetchone()[0]
print(len(ids), eid, folder)
"""


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    env = {**os.environ, "FILMLOOK_LIBRARY": str(tmp / "library"), "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS)}
    shoot = tmp / "photos" / "2025 05 04 Test shoot"
    out = subprocess.run([sys.executable, "-c", SETUP, str(shoot)], env=env, cwd=ROOT, capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    n, eid, folder = map(int, out.stdout.split()[-3:])
    port = _free_port()
    server = subprocess.Popen([sys.executable, "-m", "filmlook.cli", "serve", "--port", str(port), "--no-browser",
                               str(shoot)], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(url + "/api/ping", timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    yield {"url": url, "photos": n, "essay": eid, "folder": folder, "env": env}
    server.terminate()
    server.wait(10)


def test_click_through_every_tab(app):
    from playwright.sync_api import sync_playwright
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    bad, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}")
                if "/api/" in r.url and (r.status == 404 or r.status >= 500) else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(app["url"])
        page.wait_for_timeout(2500)
        tab = lambda name: page.get_by_role("button", name=name, exact=True).first.click()

        # Library: the shoot, a search, Worth a second look / On this day
        tab("Library")
        page.wait_for_timeout(1500)
        page.evaluate("""async () => { for (const v of [...$("lbMode").options].map(o => o.value)) {
            $("lbMode").value = v; $("lbMode").dispatchEvent(new Event("change")); await new Promise(r => setTimeout(r, 1200)); } }""")

        # Cull: start a cull of the test shoot, compare two frames, zoom
        tab("Cull")
        page.wait_for_timeout(1500)
        page.evaluate(f"""async () => {{ const r = await (await api("/api/cull/start", esJSON({{ scope: {{ folder_id: {app['folder']},
            categories: [] }}, label: "E2E" }}))).json();
            await cuLoad(r.session); await new Promise(r => setTimeout(r, 2000));
            await cuZoom(cuGroup().ids[0], 0.5, 0.5); cuZoomClose();
            cuCompare(); await new Promise(r => setTimeout(r, 2500)); cuCompareClose(); }}""")

        # Grade: open a photo, click every tool tab, crop screen + auto-straighten, camera-JPG check, masks list
        tab("Grade")
        page.wait_for_timeout(2500)
        page.evaluate("""async () => { if (!S.photos.length) return;
            await select(S.photos[0]); await new Promise(r => setTimeout(r, 2500));
            for (const k of ["adjust", "tone", "masks", "out", "crop", "look"]) { gradeTab(k); await new Promise(r => setTimeout(r, 400)); }
            await cropEnter(); await new Promise(r => setTimeout(r, 1500));
            $("crAuto").click(); await new Promise(r => setTimeout(r, 2500));
            $("crAutoV").click(); await new Promise(r => setTimeout(r, 3000));
            setPersp(30, -20); await new Promise(r => setTimeout(r, 1500)); cropExit(true);        // Transform, saved
            await new Promise(r => setTimeout(r, 2500));
            await api("/api/render?" + q({ name: S.photos[0], look: S.look, edge: 400, crop: "saved" }));
            await saveEdit(blankEdit());
            await (await api("/api/mask_info?" + q({ name: S.photos[0], kind: "sky" }))).json();
            const c = document.createElement("canvas"); c.width = c.height = 8;     // a painted mask + both gradients render
            c.getContext("2d").fillRect(0, 0, 4, 8);
            const blob = await new Promise(r => c.toBlob(r, "image/png"));
            const id = (await (await api("/api/brush", { method: "POST", headers: { "Content-Type": "image/png" }, body: blob })).json()).id;
            await api("/api/brush/" + id + ".png");
            await api("/api/brush/" + id + ".png?" + q({ name: S.photos[0], g: JSON.stringify({ crop: [0, 0, 0.5, 1] }) }));   // through another crop
            await api("/api/masks/refit", esJSON({ name: S.photos[0], old: { crop: [0.1, 0.1, 0.8, 0.8] },
              masks: [{ kind: "radial", cx: 0.5, cy: 0.5, rx: 0.2, ry: 0.2 }, { kind: "linear" }, { kind: "brush", id }] }));
            await api("/api/mark_file", esJSON({ name: S.photos[0], flag: "pick", rating: 3 }));   // marking from Grade
            gradeStep(1); gradeBW(); gradeBW();
            await api("/api/dragfile?" + q({ name: S.photos[0] }));                   // the JPG copy used to drag a photo out
            await api("/api/library/dragfile/" + (await (await api("/api/library/search?cats=&limit=1")).json()).results[0].id);
            await api("/api/render?" + q({ name: S.photos[0], look: S.look, edge: 400, adjust: JSON.stringify({ masks: [
              { kind: "brush", id, exposure: 0.5 }, { kind: "linear", exposure: -0.5 }, { kind: "radial", exposure: 0.5 }] }) })); }""")

        # Essays: the test essay and every panel (AI ones only show their buttons; nothing is sent to Claude)
        tab("Essays")
        page.wait_for_timeout(1500)
        page.evaluate(f"""async () => {{ await esLoad({app['essay']}); await new Promise(r => setTimeout(r, 2500));
            for (const p of ["check", "ai", "pairs", "bench", "fill", "export", "book", "contact", "publish", "brief"]) {{
                await esPanel(p); await new Promise(r => setTimeout(r, 1500)); }} }}""")

        # ⌘K: go anywhere lists tabs, shoots, essays and culls
        page.evaluate("async () => { await goOpen(); $('goQ').value = 'e2e'; goDraw(); goClose(); }")

        # Settings: every section renders
        page.evaluate("async () => { await openSettings(); await new Promise(r => setTimeout(r, 2500)); $('setClose').click(); }")
        browser.close()
    assert not bad, "Server said Not Found / error for: " + "; ".join(sorted(set(bad))[:10])
    assert not errors, "The page hit a script error: " + "; ".join(errors[:5])
