"""M6 review/share surface — camera-free test (MockEngine).

Verifies that taking a shot persists a reviewable record and that the /gallery surface lists it
with its record fields + thumbs verdict, and that /review serves HTML.

Runs with no camera and no network: SHOTS_DIR is redirected to a temp dir. If FastAPI is
installed we exercise the real routes via TestClient; otherwise (the backend/.venv-ml case, which
has no fastapi) we call the underlying engine/feedback handlers directly against the temp dir.
Either way the same assertions hold.

Run:  backend/.venv-ml/bin/python backend/test_app_gallery.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# A camera-free test must never touch real hardware, whatever CAMERA_ENGINE is set to ambiently.
os.environ["CAMERA_ENGINE"] = "mock"
os.environ["CAMERA_AUTOPILOT_API_TOKEN"] = "test-token"

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import engine
import feedback

WEB_DIR = BACKEND / "web"


def _point_shots_dir(tmp: Path) -> None:
    """Redirect every persistence path at the temp dir (engine + app's imported reference)."""
    engine.SHOTS_DIR = tmp
    try:
        import app  # only present when fastapi is installed
        app.SHOTS_DIR = tmp
    except ModuleNotFoundError:
        pass


def _has_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401  (TestClient needs httpx)
        return True
    except ModuleNotFoundError:
        return False


def test_with_testclient(tmp: Path) -> None:
    import app
    from fastapi.testclient import TestClient

    _point_shots_dir(tmp)
    # Re-point feedback to a temp log so verdicts don't read the real feedback.jsonl.
    feedback.FEEDBACK_FILE = tmp / "feedback.jsonl"

    client = TestClient(app.app)
    auth = {"X-Camera-Autopilot-Token": "test-token"}

    assert client.post("/shoot", json={"intent": "warm cozy portrait"}).status_code == 401
    res = client.post("/shoot", json={"intent": "warm cozy portrait"}, headers=auth)
    assert res.status_code == 200, res.status_code
    shot = res.json()
    assert set(shot) == {
        "ok",
        "image",
        "summary",
        "why",
        "mood",
        "settings",
        "cards",
        "applied",
        "evidence",
    }, "shoot response shape changed"
    assert isinstance(shot["cards"], list)
    assert isinstance(shot["applied"], dict)
    assert shot["evidence"]["physical_capture"] is False
    image = shot["image"]

    # Sidecar persisted next to the image.
    sidecar = tmp / f"{Path(image).stem}.json"
    assert sidecar.exists(), "shot record sidecar was not written"
    record = json.loads(sidecar.read_text())
    assert record["image"] == image
    assert record["intent"] == "warm cozy portrait"
    assert record["summary"] and record["settings"]

    # A thumbs verdict should surface on the gallery card.
    client.post(
        "/feedback",
        json={"image": image, "verdict": "up", "intent": "warm cozy portrait"},
        headers=auth,
    )

    assert client.get("/gallery").status_code == 401
    gal = client.get("/gallery", headers=auth)
    assert gal.status_code == 200
    rows = gal.json()
    assert len(rows) == 1, f"expected 1 shot, got {len(rows)}"
    row = rows[0]
    assert row["image"] == f"/shots/{image}"
    assert row["summary"] == shot["summary"]
    assert row["why"] == shot["why"]
    assert row["settings"] == shot["settings"]
    assert row["intent"] == "warm cozy portrait"
    assert row["verdict"] == "up"
    assert "ts" in row

    assert client.get(f"/shots/{image}").status_code == 401
    assert client.get(f"/shots/{image}", headers=auth).status_code == 200

    rev = client.get("/review")
    assert rev.status_code == 200
    assert "text/html" in rev.headers.get("content-type", "")
    assert "<html" in rev.text.lower()
    assert client.get(f"/shots/{sidecar.name}").status_code == 401
    assert (
        client.get(f"/shots/{sidecar.name}", headers=auth).status_code == 404
    ), "shot sidecar JSON must not be served, even to an authenticated gallery client"
    print("PASS (TestClient): shoot persists record, /gallery lists it, /review serves HTML")


def test_direct(tmp: Path) -> None:
    """No-fastapi path: drive engine + feedback helpers directly against the temp dir."""
    _point_shots_dir(tmp)
    feedback.FEEDBACK_FILE = tmp / "feedback.jsonl"

    eng = engine.get_engine()
    assert eng.name == "mock", f"expected MockEngine, got {eng.name}"

    intent = "warm cozy portrait"
    shot = eng.shoot(intent)
    # /shoot response shape is unchanged (dataclass fields).
    assert {"ok", "image", "summary", "why", "settings"}.issubset(shot.__dict__), "Shot shape changed"

    sidecar = engine.save_shot_record(shot, intent)
    assert sidecar.exists(), "shot record sidecar was not written"
    record = json.loads(sidecar.read_text())
    assert record["image"] == shot.image
    assert record["intent"] == intent
    assert record["summary"] == shot.summary
    assert record["settings"] == shot.settings
    assert "ts" in record

    feedback.log_feedback(intent=intent, verdict="up", image=shot.image)

    rows = engine.gallery_records(verdict_for=feedback.verdict_for)
    assert len(rows) == 1, f"expected 1 shot, got {len(rows)}"
    row = rows[0]
    assert row["image"] == f"/shots/{shot.image}"
    assert row["summary"] == shot.summary
    assert row["why"] == shot.why
    assert row["settings"] == shot.settings
    assert row["intent"] == intent
    assert row["verdict"] == "up"
    assert "ts" in row

    # Tolerate an image with no sidecar: a stray .jpg must not crash enumeration or be listed.
    (tmp / "shot_0000000000.jpg").write_bytes(b"not-a-real-jpeg")
    rows_again = engine.gallery_records(verdict_for=feedback.verdict_for)
    assert len(rows_again) == 1, "stray image without a sidecar must be ignored, not crash"

    # A corrupt/hand-edited sidecar cannot turn /gallery into a path-traversal URL.
    bad = tmp / "shot_9999999999.json"
    bad.write_text(json.dumps({"image": "../secret.jpg", "ts": 9999999999}), encoding="utf-8")
    names = [row["image"] for row in engine.gallery_records()]
    assert "/shots/secret.jpg" in names and "/shots/../secret.jpg" not in names, names

    # /review serves HTML — the route is a plain FileResponse(review.html), so assert the file.
    review_html = (WEB_DIR / "review.html").read_text(encoding="utf-8")
    assert "<html" in review_html.lower() and "/gallery" in review_html
    print("PASS (direct): shoot persists record, gallery_records lists it, review.html is HTML")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        if _has_fastapi():
            test_with_testclient(tmp)
        else:
            test_direct(tmp)
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
