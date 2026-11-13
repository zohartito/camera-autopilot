"""Camera-free proof of the full-resolution identity gate (escape the postview).

No camera, no network: candidate stills are REAL decodable JPEGs on disk, and the FakeCamera (modelled
on backend/test_wifi_lastwave.py's) writes them and records begin_session() resumes. Every image goes
through the same `image_safety.open_rgb` gate the production path uses.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from PIL import Image, ImageDraw, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wifi_full_res
from stack_session import frame_receipt


# --- synthetic scenes ---------------------------------------------------------------------------
def _scene(size: tuple[int, int], seed: int) -> Image.Image:
    """A deterministic, structured scene: a diagonal gradient plus a seeded grid of solid blocks.

    High local contrast at block edges makes both NCC and the difference-hash sensitive to framing,
    so a true downscale matches and a 5% pan clearly does not.
    """
    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 255) // width, (y * 255) // height, ((x + y) * 127) // (width + height))
    draw = ImageDraw.Draw(image)
    cols, rows = 8, 6
    state = seed
    for row in range(rows):
        for col in range(cols):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            color = (state & 0xFF, (state >> 8) & 0xFF, (state >> 16) & 0xFF)
            x0 = col * width // cols
            y0 = row * height // rows
            x1 = x0 + width // (cols * 2)
            y1 = y0 + height // (rows * 2)
            draw.rectangle([x0, y0, x1, y1], fill=color)
    return image


def _save(image: Image.Image, path: Path) -> Path:
    image.save(path, "JPEG", quality=92)
    return path


# --- identity_score / verify_identity ------------------------------------------------------------
def test_identity_passes_for_true_downscale(tmp_path):
    base = _scene((1200, 800), seed=7)
    full = _save(base, tmp_path / "full.jpg")
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    result = wifi_full_res.verify_identity(postview, full)
    assert result["verified"] is True, result
    assert result["ncc"] >= wifi_full_res.NCC_MIN, result
    assert result["phash_distance"] <= wifi_full_res.PHASH_MAX, result
    assert result["identity_basis"] == "content_only", result


def test_identity_fails_for_different_frame(tmp_path):
    postview = _save(_scene((600, 400), seed=1), tmp_path / "postview.jpg")
    other = _save(_scene((1200, 800), seed=999), tmp_path / "other.jpg")
    result = wifi_full_res.verify_identity(postview, other)
    assert result["verified"] is False, result
    assert result["ncc"] < wifi_full_res.NCC_MIN or result["phash_distance"] > wifi_full_res.PHASH_MAX, result


def test_identity_fails_for_five_percent_shift(tmp_path):
    base = _scene((1200, 800), seed=42)
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    shifted = _save(ImageChops.offset(base, int(round(0.05 * 1200)), 0), tmp_path / "shifted.jpg")
    result = wifi_full_res.verify_identity(postview, shifted)
    ncc_failed = result["ncc"] < wifi_full_res.NCC_MIN
    phash_failed = result["phash_distance"] > wifi_full_res.PHASH_MAX
    # A 5% pan of the same scene must break at least one content test — say which in the message.
    assert result["verified"] is False, result
    assert ncc_failed or phash_failed, (
        f"5% shift did not fail either metric: ncc={result['ncc']} phash={result['phash_distance']}")
    which = ("ncc" if ncc_failed else "") + ("+phash" if ncc_failed and phash_failed else
                                             ("phash" if phash_failed else ""))
    print(f"  5% shift failed via: {which}  (ncc={result['ncc']}, phash={result['phash_distance']})")


def test_aspect_mismatch_fails(tmp_path):
    base = _scene((1200, 800), seed=3)                       # 3:2
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    widescreen = _save(_scene((1280, 720), seed=3), tmp_path / "wide.jpg")   # 16:9, same seed content
    result = wifi_full_res.verify_identity(postview, widescreen)
    assert result["aspect_match"] is False, result
    assert result["verified"] is False, result
    assert "aspect" in result["reason"], result


def test_timestamp_outside_window_fails_even_with_perfect_content(tmp_path):
    base = _scene((1200, 800), seed=7)
    full = _save(base, tmp_path / "full.jpg")
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    # Perfect content, but the card file was created 60 s after the shutter — outside [-2, +30].
    result = wifi_full_res.verify_identity(postview, full, created_time=1_000_060.0, shutter_time=1_000_000.0)
    assert result["identity_basis"] == "content+time", result
    assert result["ncc"] >= wifi_full_res.NCC_MIN, result       # content still matches
    assert result["verified"] is False, result
    assert "window" in result["reason"], result


def test_timestamp_inside_window_passes(tmp_path):
    base = _scene((1200, 800), seed=7)
    full = _save(base, tmp_path / "full.jpg")
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    result = wifi_full_res.verify_identity(postview, full, created_time=1_000_004.0, shutter_time=1_000_000.0)
    assert result["identity_basis"] == "content+time", result
    assert result["verified"] is True, result
    assert result["time_delta_s"] == pytest.approx(4.0), result


# --- pull_full_res_for_capture -------------------------------------------------------------------
class FakeCam:
    """Duck-typed camera modelled on test_wifi_lastwave.FakeCamera: writes real candidate JPEGs and
    records that the live session was resumed via begin_session()."""

    def __init__(self, frames):
        self._frames = frames                # list[(name, PIL.Image)], newest-first
        self.begin_calls = 0
        self.raise_on_transfer = False

    def transfer_latest_full_res(self, save_dir, count=1, prefer_raw=False):
        assert prefer_raw is False
        if self.raise_on_transfer:
            raise OSError("contents transfer dropped mid-download")
        saved = []
        for name, image in self._frames[:count]:
            path = Path(save_dir) / name
            image.save(path, "JPEG", quality=92)
            saved.append(str(path))
        return saved

    def begin_session(self):
        self.begin_calls += 1
        return []


def test_pull_picks_verified_candidate_and_deletes_others(tmp_path):
    base = _scene((1200, 800), seed=7)
    postview = _save(base.resize((600, 400), Image.BILINEAR), tmp_path / "postview.jpg")
    work = tmp_path / "work"
    work.mkdir()
    # Newest-first: a non-match is newest, the true match is second, another non-match last.
    cam = FakeCam([
        ("DSC0003.JPG", _scene((1200, 800), seed=101)),
        ("DSC0002.JPG", base),
        ("DSC0001.JPG", _scene((1200, 800), seed=202)),
    ])
    result = wifi_full_res.pull_full_res_for_capture(cam, str(postview), str(work),
                                                     shutter_time=1_000_000.0, count=3)
    assert result["status"] == "verified", result
    assert result["path"].endswith("DSC0002.JPG"), result
    assert result["identity_basis"] == "content_only", result
    assert result["session_resumed"] is True and cam.begin_calls == 1
    receipt = result["receipt"]
    for key in ("bytes", "sha256", "width", "height", "path"):
        assert key in receipt, receipt
    assert receipt["width"] == 1200 and receipt["height"] == 800, receipt
    # The chosen file remains; the two non-matches were deleted.
    assert Path(result["path"]).is_file()
    assert not (work / "DSC0003.JPG").exists()
    assert not (work / "DSC0001.JPG").exists()
    # The receipt fingerprint matches an independent frame_receipt of the kept file.
    assert receipt["sha256"] == frame_receipt(result["path"])["sha256"]


def test_pull_resumes_session_in_finally_even_when_download_raises(tmp_path):
    base = _scene((600, 400), seed=7)
    postview = _save(base, tmp_path / "postview.jpg")
    work = tmp_path / "work"
    work.mkdir()
    cam = FakeCam([("DSC0002.JPG", _scene((1200, 800), seed=7))])
    cam.raise_on_transfer = True
    result = wifi_full_res.pull_full_res_for_capture(cam, str(postview), str(work),
                                                     shutter_time=1_000_000.0, count=3)
    assert result["status"] == "unverified", result
    assert "transfer failed" in result["reason"], result
    assert result["session_resumed"] is True and cam.begin_calls == 1


def test_pull_unverified_when_no_candidate_matches(tmp_path):
    postview = _save(_scene((600, 400), seed=7), tmp_path / "postview.jpg")
    work = tmp_path / "work"
    work.mkdir()
    cam = FakeCam([
        ("DSC0002.JPG", _scene((1200, 800), seed=555)),
        ("DSC0001.JPG", _scene((1200, 800), seed=666)),
    ])
    result = wifi_full_res.pull_full_res_for_capture(cam, str(postview), str(work),
                                                     shutter_time=1_000_000.0, count=3)
    assert result["status"] == "unverified", result
    assert result["session_resumed"] is True and cam.begin_calls == 1
    # No candidate was kept.
    assert not (work / "DSC0002.JPG").exists()
    assert not (work / "DSC0001.JPG").exists()
