"""M3 panorama tests — the camera-free logic for the WiFi pano sweep + stitch, plus a DETERMINISTIC
real-stitch test. No live camera:

  * shoot_sweep is exercised against a tiny fake-cam stub (mirroring test_wifi_lastwave's FakeCamera);
  * stitch() SUCCESS is proven by slicing ONE large textured image into 6 horizontally-overlapping
    tiles (~40% overlap) and asserting it stitches back (stitched=True);
  * stitch() ERROR + NO-FLOOD is proven by feeding 2 identical / non-overlapping frames and asserting
    a clean error dict comes back with NO exception AND NOTHING written to fd 2 (the LAPACK/DLASCL
    flood the hardening suppresses).

    backend/.venv-ml/bin/python backend/test_wifi_pano.py    (needs numpy/cv2/PIL)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the actual modules: process-wide dependency stubs contaminate aggregate tests.

import wifi_pano


# --- a fake camera that records calls and scripts shoot_sweep's responses -----------------
class FakeSweepCamera:
    """Minimal stub for shoot_sweep — it only touches begin_session / set_focus_mode /
    grab_liveview_frame / call(get*) / take_picture. We record the take_picture count and the pan
    hints printed so the frame/hint-count contract is checked without a camera. focus=False is passed
    by the test so the vision_banana/focus_subject path is skipped (those need the ML stack)."""

    def __init__(self):
        self.shots = 0
        self.calls: list[str] = []

    def begin_session(self):
        self.calls.append("begin_session")

    def set_focus_mode(self, mode):
        self.calls.append(f"set_focus_mode:{mode}")

    def grab_liveview_frame(self, save_path=None):
        # write a tiny real JPEG so shoot_sweep's PIL.Image.open(save_path).size works
        from PIL import Image as _Img
        _Img.new("RGB", (640, 480)).save(save_path)

    def call(self, method, params=None, version="1.0"):
        # shoot_sweep reads ISO / f / shutter once; return plausible Sony-shaped results
        canned = {"getIsoSpeedRate": {"result": ["800"]},
                  "getFNumber": {"result": ["5.6"]},
                  "getShutterSpeed": {"result": ["1/60"]},
                  "getExposureMode": {"result": ["Manual"]},
                  "getFocusMode": {"result": ["MF"]},
                  "getWhiteBalance": {"result": ["Daylight"]}}
        return canned.get(method, {"result": [], "id": 1})

    def take_picture(self, timeout=40.0):
        self.shots += 1
        return [f"http://cam/pano{self.shots}.jpg"]


# --- tests --------------------------------------------------------------------------------
def test_cv2_readable_filter():
    # the suffix gate must keep decodable stills and drop RAW (.ARW) siblings cv2 can't read
    paths = ["/card/DSC1.JPG", "/card/DSC1.ARW", "/card/DSC2.jpeg", "/card/DSC3.png",
             "/card/DSC4.tif", "/card/DSC5.tiff", "/card/DSC6.arw", "/card/DSC7.raw"]
    kept = [p for p in paths if Path(p).suffix.lower() in wifi_pano._CV2_OK]
    assert kept == ["/card/DSC1.JPG", "/card/DSC2.jpeg", "/card/DSC3.png",
                    "/card/DSC4.tif", "/card/DSC5.tiff"], kept
    # case-insensitive: an upper-case .JPG passes, .ARW/.RAW are dropped
    assert ".arw" not in {Path(p).suffix.lower() for p in kept}
    print("  ok  cv2-readable filter (keeps jpg/jpeg/png/tif/tiff, drops RAW)")


def _textured_source(width=1200, height=600):
    """Return one large STRONGLY-TEXTURED BGR image to slice for the stitch test: synthesized
    texture (random noise lightly blurred) so the stitcher finds plenty of features. The repository
    ships no photographs."""
    import cv2
    import numpy as np
    # blurred RGB noise — high-frequency, feature-rich, fully deterministic via the seed
    rng = np.random.default_rng(1234)
    noise = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    return cv2.GaussianBlur(noise, (0, 0), sigmaX=1.4)


def _capture_c_output(callable_no_args):
    """Run callable_no_args() while capturing EVERYTHING written to the real fds 1 AND 2 (C-level
    stdout + stderr). Returns (return_value, captured_text). This is how we prove the DLASCL/LAPACK
    flood is actually suppressed: cv2 emits those lines from native code straight to an OS fd, so a
    Python-level capture wouldn't see them — and on this OpenCV build the flood lands on fd 1, not
    fd 2, so we redirect BOTH to one temp file and read it back."""
    saved = {fd: os.dup(fd) for fd in (1, 2)}
    tmp = tempfile.TemporaryFile(mode="w+b")
    try:
        sys.stdout.flush(); sys.stderr.flush()
        for fd in saved:
            os.dup2(tmp.fileno(), fd)
        try:
            value = callable_no_args()
        finally:
            sys.stdout.flush(); sys.stderr.flush()
            for fd, original in saved.items():
                os.dup2(original, fd)
        tmp.seek(0)
        return value, tmp.read().decode("utf-8", "replace")
    finally:
        for original in saved.values():
            os.close(original)
        tmp.close()


def test_stitch_slices_back_together():
    # DETERMINISTIC success path: take ONE big textured image, cut it into 6 vertical tiles that each
    # overlap its neighbour by ~40%, and assert stitch() puts them back into one panorama.
    # TemporaryDirectory, not tmp_path: a concurrent session sharing pytest's temp root can delete an
    # older basetemp mid-stitch.
    import cv2
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        src = _textured_source(width=1200, height=600)
        full_w = src.shape[1]

        n_tiles = 6
        overlap = 0.40                                         # ~40% neighbour overlap, the sweet spot
        step = int(full_w * (1 - overlap) / (n_tiles - 1))     # left edge advance per tile
        tile_w = full_w - step * (n_tiles - 1)                 # width that makes the last tile hit the edge
        tile_paths = []
        for i in range(n_tiles):
            x0 = min(i * step, full_w - tile_w)
            tile = src[:, x0:x0 + tile_w]
            path = str(out_dir / f"tile{i}.png")
            cv2.imwrite(path, tile)
            tile_paths.append(path)

        out_path = str(out_dir / "pano_ok.jpg")
        result = wifi_pano.stitch(tile_paths, out_path)
        assert result["stitched"] is True, result             # the tiles must stitch back together
        assert result["mode_used"] in ("panorama", "scans"), result
        assert result["status"] == cv2.Stitcher_OK, result
        assert result["size"] is not None and result["size"][0] > 0, result
        assert Path(out_path).exists(), "a successful stitch must write the pano"
    print(f"  ok  stitch slices back together (6 tiles ~40% overlap -> "
          f"{result['mode_used']} {result['size']})")


def test_stitch_error_dict_no_stderr_flood():
    # ERROR path WITHOUT the flood: two NON-overlapping featureless frames give the bundle adjuster
    # nothing to match (no correspondences -> non-OK in BOTH panorama and scans). This is the input
    # that, unsuppressed, floods stderr with hundreds of DLASCL lines from the PANORAMA attempt.
    # stitch() must return a clean dict, NOT raise, and write NOTHING to fd 2.
    import cv2
    import numpy as np
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        red = np.zeros((128, 128, 3), dtype=np.uint8); red[:] = (0, 0, 255)
        blue = np.zeros((128, 128, 3), dtype=np.uint8); blue[:] = (255, 0, 0)
        p_red, p_blue = str(out_dir / "red.png"), str(out_dir / "blue.png")
        cv2.imwrite(p_red, red); cv2.imwrite(p_blue, blue)    # no features anywhere -> degenerate solve
        out_path = str(out_dir / "pano_fail.jpg")

        result, flood_text = _capture_c_output(lambda: wifi_pano.stitch([p_red, p_blue], out_path))
        assert result["stitched"] is False, result
        assert result["status"] != cv2.Stitcher_OK, result    # a real non-OK status code came back
        assert result["mode_used"] is None, result
        assert "~40% overlap" in result["hint"], result       # the capture-guiding hint is present
        assert not Path(out_path).exists(), "no pano should be written on a failed stitch"
        # the whole point of the hardening: the LAPACK/DLASCL flood never reaches the terminal (fd 1+2)
        assert "DLASCL" not in flood_text, f"flood leaked: {flood_text[:200]!r}"
        assert flood_text.strip() == "", f"unexpected C-level output: {flood_text[:200]!r}"

        # two byte-IDENTICAL frames: a different degenerate shape (zero baseline). Whether this build
        # solves it (identity) or rejects it, stitch() must NOT raise and must NOT flood fd 2.
        frame = _textured_source(width=400, height=300)
        p_a, p_b = str(out_dir / "same_a.png"), str(out_dir / "same_b.png")
        cv2.imwrite(p_a, frame); cv2.imwrite(p_b, frame)
        same, same_flood = _capture_c_output(
            lambda: wifi_pano.stitch([p_a, p_b], str(out_dir / "same.jpg")))
        assert same["stitched"] in (True, False), same        # either outcome is fine; just no crash
        assert "DLASCL" not in same_flood, same_flood[:200]

        # fewer than 2 readable frames -> its own clean error dict (no Stitcher call, no raise)
        one = wifi_pano.stitch([p_red], out_path)
        assert one["stitched"] is False and one["status"] is None and one["mode_used"] is None, one
    print("  ok  stitch error dict + NO stderr flood (no-feature/identical/<2 frames all clean)")


def test_shoot_sweep_frame_and_hint_counts(capsys=None):
    # frames=4, focus off (skip the ML path) -> exactly 4 shots fired, and a pan hint printed before
    # every frame EXCEPT the first (so frames-1 = 3 hints). settle_s=0 keeps the test instant.
    import contextlib
    import io
    cam = FakeSweepCamera()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        shot = wifi_pano.shoot_sweep(cam, frames=4, settle_s=0.0,
                                     frame_prefix="/tmp/test_pano_sweep", focus=False)
    out = buf.getvalue()

    assert shot["frames"] == 4, shot
    assert cam.shots == 4, cam.shots                       # exactly `frames` shutter fires
    assert len(shot["postviews"]) == 4, shot["postviews"]
    hint_lines = [ln for ln in out.splitlines() if "pan" in ln and "hold" in ln]
    assert len(hint_lines) == 3, hint_lines                # frames - 1 pan prompts
    # the locked exposure was read once from the canned getters and carried into the summary
    assert shot["locked_iso"] == 800.0 and shot["locked_f"] == 5.6, shot
    assert "begin_session" in cam.calls and "set_focus_mode:AF-S" not in cam.calls, cam.calls
    print("  ok  shoot_sweep frame/hint counts (N shots, N-1 pan prompts, exposure locked)")


if __name__ == "__main__":
    print("wifi pano tests:")
    test_cv2_readable_filter()
    test_stitch_slices_back_together()
    test_stitch_error_dict_no_stderr_flood()
    test_shoot_sweep_frame_and_hint_counts()
    print("ALL wifi pano tests PASS")
