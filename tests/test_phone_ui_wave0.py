"""Wave 0E phone UI: last-frame grid/histogram/zebra, card history, auto-save.

Camera-free. Node and Python helpers are probed by executing them, not by PATH presence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from test_final_wave_product_api import product  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "backend" / "web"
CLIENT = WEB / "client.js"
INDEX = WEB / "index.html"
REVIEW = WEB / "review.html"
OVERLAY_NAME = "wave0e-last-frame-overlays.png"


def _usable(tool: str, version_flag: str) -> str:
    """Return a tool path only if it actually runs.

    Presence on PATH is not enough (macOS java stubs taught that). Node rejects
    `-version`; `--version` is the flag that executes.
    """
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} is not on PATH")
    try:
        probe = subprocess.run([path, version_flag], check=False, capture_output=True, text=True)
    except OSError:
        pytest.skip(f"{tool} did not execute")
    if probe.returncode != 0:
        pytest.skip(f"{tool} {version_flag} failed")
    return path


def run_js(code: str) -> str:
    node = _usable("node", "--version")
    script = "const assert=require('node:assert/strict');\n" + code
    return subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout


def test_node_toolchain_actually_runs() -> None:
    node = _usable("node", "--version")
    assert subprocess.run([node, "--version"], check=True, capture_output=True, text=True).stdout.startswith("v")


def test_last_frame_aids_are_labelled_and_not_live_view() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="last-frame-grid"' in html
    assert 'id="last-frame-zebra"' in html
    assert 'id="last-frame-histogram"' in html
    assert "Last captured frame — not a live view" in html
    assert "not a live view" in html
    assert "maxEdge: 1600" in html
    assert "maybeAutoSaveToPhotos" in html
    assert 'id="auto-save-photos"' in html
    assert '<input id="auto-save-photos" type="checkbox" checked' not in html
    assert "photosSaveStatusText" in html
    assert "/client.js?v=20260929a" in html
    assert "/workflows.js?v=20261003c" in html
    assert 'fit: "contain"' in html
    assert "boxWidth: canvas.clientWidth" in html
    assert "boxHeight: canvas.clientHeight" in html


def test_auto_save_checkbox_is_not_in_the_forget_row() -> None:
    html = INDEX.read_text(encoding="utf-8")
    css, body = html.split("</style>", 1)
    label_css = css[css.index("#auto-save-photos-label"):css.index("#auto-save-photos-label input")]
    assert "display: flex" in label_css
    assert "#forget-device { display: block; width: 100%; }" in css
    forget = body.index('id="forget-device"')
    label = body.index('id="auto-save-photos-label"')
    checkbox = body.index('id="auto-save-photos"')
    # 2026-09-29 phone audit: a tap near the auto-save box hit "Forget this device". The everyday
    # toggle now leads the settings panel and the destructive button sits below the refresh control.
    assert label < checkbox < forget
    between = body[checkbox:forget]
    assert "<div class=\"row\"" not in between
    assert 'id="device-refresh"' in between
    panel = body.split('<details id="device-panel"', 1)[1].split("</details>", 1)[0]
    assert panel.index('id="auto-save-photos-label"') < panel.index('id="device-refresh"') < panel.index('id="forget-device"')
    assert 'id="auto-save-photos-label"' in panel


def test_last_frame_grid_is_clipped_to_photo_rect() -> None:
    workflows = (WEB / "workflows.js").read_text(encoding="utf-8")
    assert "context.rect(dest.x, dest.y, dest.width, dest.height)" in workflows
    assert "context.clip()" in workflows
    out = run_js("""
    const ops=[];
    function context2d() {
      return {
        fillStyle:'', strokeStyle:'', lineWidth:1,
        fillRect(...a){ops.push(['fillRect',...a]);},
        drawImage(_img,x,y,w,h){ops.push(['drawImage',x,y,w,h]);},
        getImageData(x,y,w,h){
          ops.push(['getImageData',x,y,w,h]);
          const data=new Uint8ClampedArray(w*h*4);
          for (let i=0;i<data.length;i+=4){data[i]=data[i+1]=data[i+2]=40;data[i+3]=255;}
          for (let yy=0;yy<h;yy++) for (let xx=Math.floor(w*0.75);xx<w;xx++){
            const i=(yy*w+xx)*4; data[i]=data[i+1]=data[i+2]=255;
          }
          return {data};
        },
        createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},
        putImageData(){},
        save(){ops.push(['save']);}, restore(){ops.push(['restore']);},
        beginPath(){ops.push(['beginPath']);},
        rect(...a){ops.push(['rect',...a]);}, clip(){ops.push(['clip']);},
        moveTo(x,y){ops.push(['moveTo',x,y]);}, lineTo(x,y){ops.push(['lineTo',x,y]);},
        stroke(){ops.push(['stroke']);}
      };
    }
    const document={createElement(tag){assert.equal(tag,'canvas');return {width:0,height:0,getContext:context2d};}};
    const window={};
    """ + (WEB / "workflows.js").read_text(encoding="utf-8") + """
      const frame={naturalWidth:480,naturalHeight:320};
      const canvas={width:0,height:0,getContext:context2d};
      const histogram={width:0,height:0,getContext:context2d};
      const measured=window.AutopilotWorkflows.paintPreview(canvas,histogram,frame,{
        grid:true,zebra:true,fit:'contain',boxWidth:375,boxHeight:812
      });
      assert.equal(canvas.width,375); assert.equal(canvas.height,812);
      assert.deepEqual(measured.dest,{x:0,y:281,width:375,height:250,scale:375/480});
      const dest=measured.dest;
      const imageData=ops.find(op=>op[0]==='getImageData');
      assert.deepEqual(imageData,['getImageData', dest.x, dest.y, dest.width, dest.height]);
      const clip=ops.find(op=>op[0]==='rect');
      assert.deepEqual(clip,['rect', dest.x, dest.y, dest.width, dest.height]);
      assert.ok(ops.some(op=>op[0]==='clip'));
      for (const op of ops) {
        if (op[0]==='moveTo' || op[0]==='lineTo') {
          assert.ok(op[1] >= dest.x - 1e-9 && op[1] <= dest.x + dest.width + 1e-9, String(op));
          assert.ok(op[2] >= dest.y - 1e-9 && op[2] <= dest.y + dest.height + 1e-9, String(op));
        }
      }
      assert.equal(ops.some(op=>op[0]==='lineTo' && op[2]===812), false);
      process.stdout.write('ok');
    """)
    assert out == "ok"


def test_review_card_history_is_read_only() -> None:
    html = REVIEW.read_text(encoding="utf-8")
    block = html.split('<details id="card-history">', 1)[1].split("</details>", 1)[0]
    assert "this page never deletes from the card" in block
    assert "/api/camera/card/list" in html
    assert "/api/camera/card/import" in html
    assert "camera_serial:camera.serial" in html
    assert "card-history-delete" not in html
    assert "class=\"danger\"" not in block
    assert "AutopilotWorkflows.cardFileViewable" in html


def test_auto_save_is_off_by_default_and_never_claims_unconfirmed_save() -> None:
    out = run_js("""
    function memoryStore() {
      const data = new Map();
      return { getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)),
               removeItem: k => data.delete(k) };
    }
    const window = { localStorage: memoryStore(), sessionStorage: memoryStore() };
    """ + CLIENT.read_text(encoding="utf-8") + """
      const A=window.Autopilot;
      assert.equal(A.autoSavePhotosEnabled(), false);
      assert.equal(A.shouldAttemptPhotosSave(true, null), false);
      assert.equal(A.shouldAttemptPhotosSave(false, {savePhoto(){}}), false);
      assert.equal(A.shouldAttemptPhotosSave(true, {savePhoto(){}}), true);
      assert.equal(A.photosSaveConfirmed({saved:true}), true);
      assert.equal(A.photosSaveConfirmed({saved:false}), false);
      assert.equal(A.photosSaveConfirmed({}), false);
      assert.equal(A.photosSaveConfirmed({shared:true}), false);
      assert.match(A.photosSaveStatusText({}), /not confirmed/);
      assert.match(A.photosSaveStatusText({saved:true}), /plugin confirmed/);
      A.setAutoSavePhotos(true);
      assert.equal(A.autoSavePhotosEnabled(), true);
      A.setAutoSavePhotos(false);
      assert.equal(A.autoSavePhotosEnabled(), false);
      process.stdout.write('ok');
    """)
    assert out == "ok"


def test_card_list_and_import_auth_and_no_gallery_enroll(product, monkeypatch) -> None:
    model = "Sony ILCE-7M2 (Control)"
    port = "usb:000,003"
    serial = "A7II00SERIAL"
    camera = {"camera_model": model, "camera_port": port, "camera_serial": serial}
    anonymous = product.client.post("/api/camera/card/list", json={"camera_model": model, "camera_port": port})
    assert anonymous.status_code == 401
    # Mock engine refuses USB selection — never opens a body.
    blocked = product.client.post("/api/camera/card/list", headers=product.headers,
                                  json={"camera_model": model, "camera_port": port})
    assert blocked.status_code == 400

    product.fake.name = "real"
    monkeypatch.setattr(product.engine, "RealEngine", lambda **kw: product.fake)
    launched = []

    def refuse_popen(*_a, **_k):
        launched.append(True)
        raise AssertionError("must not open a camera")

    monkeypatch.setattr("card_history.subprocess.Popen", refuse_popen)

    missing = product.client.post("/api/camera/card/import", headers=product.headers, json={
        "camera_model": model, "camera_port": port, "folder": "/DCIM", "name": "DSC0001.JPG"})
    assert missing.status_code == 400, missing.text
    assert missing.json()["reason"] == "serial_required"
    assert launched == []
    imports = product.root / "card-imports"
    assert not imports.exists() or not any(imports.rglob("*"))

    def fake_list(model_, port_, *, serial=None, cancel_event=None):
        assert product.app._camera_lock.locked()
        assert model_ == model and port_ == port and serial == camera["camera_serial"]
        return {"status": "completed", "folders": ["/", "/DCIM"], "files": [
            {"folder": "/DCIM", "name": "DSC0001.JPG", "bytes": 12, "downloadable": True},
            {"folder": "/DCIM", "name": "DSC0001.ARW", "bytes": 99, "downloadable": True},
        ], "file_count": 2, "read_only": True, "retry_capture": False}

    monkeypatch.setattr("card_history.list_card", fake_list)
    listing = product.client.post("/api/camera/card/list", headers=product.headers, json=camera)
    assert listing.status_code == 200, listing.text
    assert listing.json()["read_only"] is True and launched == []

    jpeg = Image.new("RGB", (32, 24), (200, 10, 10))
    payload = BytesIO()
    jpeg.save(payload, format="JPEG")
    jpeg_bytes = payload.getvalue()

    def fake_download(model_, port_, folder, name, destination_root, *, serial=None, cancel_event=None):
        assert product.app._camera_lock.locked()
        assert launched == [] and serial == camera["camera_serial"]
        job = Path(destination_root) / ("card_" + hashlib.sha256(name.encode()).hexdigest()[:32])
        job.mkdir(parents=True, exist_ok=True)
        path = job / name
        path.write_bytes(jpeg_bytes if name.endswith(".JPG") else b"ARW-FIXTURE")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = {"status": "completed", "folder": folder, "name": name, "bytes": path.stat().st_size,
                   "sha256": digest, "read_only": True, "card_deleted": False, "retry_capture": False,
                   "job_id": job.name, "image": name}
        (job / "receipt.json").write_text(json.dumps(receipt))
        return receipt

    monkeypatch.setattr("card_history.download_card_file", fake_download)
    imported = product.client.post("/api/camera/card/import", headers=product.headers, json={
        **camera, "folder": "/DCIM", "name": "DSC0001.JPG"})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["gallery_enrolled"] is False and body["card_deleted"] is False
    assert body["viewable"] is True and body["url"].endswith("/DSC0001.JPG")
    served = product.client.get(body["url"], headers=product.headers)
    assert served.status_code == 200 and served.content == jpeg_bytes
    gallery = product.client.get("/gallery", headers=product.headers)
    assert gallery.status_code == 200 and gallery.json() == []

    raw = product.client.post("/api/camera/card/import", headers=product.headers, json={
        **camera, "folder": "/DCIM", "name": "DSC0001.ARW"})
    assert raw.status_code == 200
    assert raw.json()["viewable"] is False and raw.json()["url"] is None

    traversal = product.client.get("/api/camera/card/imports/card_../escape/x.jpg", headers=product.headers)
    assert traversal.status_code == 404
    assert launched == []


def test_card_import_refuses_when_port_holds_a_different_body(product, monkeypatch) -> None:
    """USB ports re-enumerate: a different body at the selected port is refused before any card read."""
    model = "Sony ILCE-7M2 (Control)"
    port = "usb:000,003"
    expected = "A7II00SERIAL"
    observed = "AAAA1111BBBB"
    product.fake.name = "real"
    monkeypatch.setattr(product.engine, "RealEngine", lambda **kw: product.fake)
    launched = []
    monkeypatch.setattr("card_history.subprocess.Popen",
                        lambda *_a, **_k: launched.append(True) or pytest.fail("must not open a camera"))
    import sonyremote.camera as camera_mod
    monkeypatch.setattr(camera_mod, "_os_identity",
                        lambda: ("ioreg", [{"model": model, "port": port, "serial": observed}]))
    monkeypatch.setattr(camera_mod, "_probe_camera_answers",
                        lambda *_a, **_k: pytest.fail("probe opened a camera"))
    response = product.client.post("/api/camera/card/import", headers=product.headers, json={
        "camera_model": model, "camera_port": port, "camera_serial": expected,
        "folder": "/DCIM", "name": "DSC0001.JPG"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "serial_mismatch"
    assert (body["expected_serial"], body["observed_serial"]) == (expected, observed)
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert launched == []
    imports = product.root / "card-imports"
    assert not imports.exists() or not any(imports.rglob("*"))


def _paint_synthetic_last_frame(path: Path) -> Path:
    width, height = 480, 320
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    for x in range(width):
        tone = int(40 + 140 * x / (width - 1))
        pixels[:, x] = (tone, tone + 8, min(255, tone + 20))
    pixels[:, 360:] = (255, 255, 255)
    overlay = pixels.copy()
    for y in range(height):
        for x in range(width):
            r, g, b = (int(v) for v in pixels[y, x])
            if max(r, g, b) >= 250 and ((x + y) % 12) < 6:
                overlay[y, x] = (255, 80, 80)
    image = Image.fromarray(overlay, "RGB")
    draw = ImageDraw.Draw(image)
    for fraction in (1 / 3, 2 / 3):
        draw.line([(width * fraction, 0), (width * fraction, height)], fill=(255, 255, 255), width=2)
        draw.line([(0, height * fraction), (width, height * fraction)], fill=(255, 255, 255), width=2)
    gray = (0.2126 * pixels[:, :, 0] + 0.7152 * pixels[:, :, 1] + 0.0722 * pixels[:, :, 2]).round().astype(np.int32)
    histogram = np.bincount(gray.ravel(), minlength=256)
    hist_h = 80
    chart = Image.new("RGB", (256, hist_h), (21, 21, 26))
    chart_draw = ImageDraw.Draw(chart)
    peak = max(int(histogram.max()), 1)
    for x, count in enumerate(histogram.tolist()):
        if not count:
            continue
        bar = max(1, int(hist_h * count / peak))
        chart_draw.line([(x, hist_h - 1), (x, hist_h - bar)], fill=(232, 184, 75))
    clipped = float(np.mean(pixels.max(axis=2) >= 250) * 100)
    caption = (f"Last captured frame - not a live view. {width} x {height}. "
               f"{clipped:.1f}% near-clipped JPEG pixels. Synthetic fixture.")
    banner = Image.new("RGB", (width, 36), (11, 11, 13))
    ImageDraw.Draw(banner).text((8, 12), caption, fill=(243, 243, 245))
    hist_row = Image.new("RGB", (width, hist_h), (21, 21, 26))
    hist_row.paste(chart.resize((width, hist_h)), (0, 0))
    composed = Image.new("RGB", (width, height + hist_h + 36), (11, 11, 13))
    composed.paste(image, (0, 0))
    composed.paste(hist_row, (0, height))
    composed.paste(banner, (0, height + hist_h))
    path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(path)
    return path


def test_synthetic_last_frame_overlay_renders_grid_zebra_and_histogram(tmp_path) -> None:
    dest = tmp_path / OVERLAY_NAME
    _paint_synthetic_last_frame(dest)
    with Image.open(dest) as rendered:
        assert rendered.size == (480, 436)
        # Right side is clipped: zebra stripes must appear, not a solid white field.
        sample = np.array(rendered.crop((400, 40, 460, 120)))
        reds = (sample[:, :, 0] > 200) & (sample[:, :, 1] < 120)
        whites = (sample.min(axis=2) > 240)
        assert reds.any() and whites.any()
        # A thirds line is near x=160.
        column = np.array(rendered)[:320, 160]
        assert (column[:, 0] > 240).mean() > 0.2
        # Histogram strip is below the frame.
        hist = np.array(rendered)[320:400]
        gold = (hist[:, :, 0] > 180) & (hist[:, :, 1] > 140) & (hist[:, :, 2] < 120)
        assert gold.any()
    artifact_dir = os.environ.get("CURSOR_ARTIFACT_DIR")
    if artifact_dir:
        public = Path(artifact_dir) / OVERLAY_NAME
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_bytes(dest.read_bytes())
