"""Truthful mood previews are derived from the axes capture actually applies (backend/mood_preview.py).

A preview swatch must reflect the SAME two axes the engine drives — mean-brightness target and
white-balance direction — not an arbitrary CSS filter. These tests prove, on a synthetic gradient
JPEG (no camera, no network):

  * a variant whose target is brighter than the source raises the mean luma toward that target;
  * a warm variant renders warmer (higher R/B ratio) than a cool one;
  * output is bounded to max_width, carries no EXIF, and is byte-deterministic across runs;
  * the recipe string is generated from the numeric fields (so display can't drift from capture),
    for every MOOD_VARIANTS entry.

Run standalone:
    python3 tests/test_mood_preview.py
"""

from __future__ import annotations

import sys
import tempfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from PIL import Image, ImageStat

from intent import MOOD_VARIANTS
from mood_preview import (
    render_all_previews,
    render_variant_preview,
    variant_recipe_from_values,
)

VARIANTS_BY_ID = {variant["id"]: variant for variant in MOOD_VARIANTS}


# --- source fixture ------------------------------------------------------------------------------

def _make_dark_gradient_jpeg(path: Path) -> Path:
    """A wide horizontal gray gradient (0..120), mean ~60 — darker than every variant target, so a
    variant can only BRIGHTEN it. Wider than max_width to also exercise the bounded downscale."""
    width, height = 800, 400
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(width):
        value = round(120 * x / (width - 1))
        for y in range(height):
            pixels[x, y] = (value, value, value)
    image.save(path, format="JPEG", quality=92)
    return path


def _luma(data: bytes) -> float:
    return ImageStat.Stat(Image.open(BytesIO(data)).convert("L")).mean[0]


def _channel_means(data: bytes) -> tuple[float, float, float]:
    red, green, blue = ImageStat.Stat(Image.open(BytesIO(data)).convert("RGB")).mean
    return red, green, blue


# --- brightness axis -----------------------------------------------------------------------------

def test_brighter_variant_raises_mean_luma_toward_target(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    source_mean = _luma(source.read_bytes())

    natural = VARIANTS_BY_ID["natural"]  # target 112, well above the ~60 source mean
    out_mean = _luma(render_variant_preview(source, natural))

    target = float(natural["target_brightness"])
    assert out_mean > source_mean, "a brighter-target variant must raise the mean luma"
    assert abs(out_mean - target) < abs(source_mean - target), "mean must move TOWARD the target"
    # k == 1 lands the mean essentially on the target (small tolerance for 8-bit rounding/JPEG).
    assert abs(out_mean - target) < 4.0, f"mean should land near target {target}, got {out_mean:.1f}"


def test_darker_target_variant_lowers_mean_relative_to_brighter(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    noir_mean = _luma(render_variant_preview(source, VARIANTS_BY_ID["moody_noir"]))      # target 82
    natural_mean = _luma(render_variant_preview(source, VARIANTS_BY_ID["natural"]))       # target 112
    assert noir_mean < natural_mean, "the lower-target (noir) preview must be darker than natural"


# --- white-balance axis --------------------------------------------------------------------------

def test_warm_variant_raises_red_to_blue_ratio(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    warm_r, _, warm_b = _channel_means(render_variant_preview(source, VARIANTS_BY_ID["dreamy_golden_hour"]))
    cool_r, _, cool_b = _channel_means(render_variant_preview(source, VARIANTS_BY_ID["cool_blue"]))

    assert warm_r / warm_b > 1.0, "warm variant must render warmer than neutral gray (R/B > 1)"
    assert warm_r / warm_b > cool_r / cool_b, "warm variant must be warmer than the cool variant"


def test_neutral_variant_leaves_gray_neutral(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    red, _, blue = _channel_means(render_variant_preview(source, VARIANTS_BY_ID["natural"]))
    assert abs(red - blue) < 1.0, "a variant with no WB direction must not tint a gray source"


# --- bounds / hygiene / determinism --------------------------------------------------------------

def test_output_is_bounded_to_max_width(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    data = render_variant_preview(source, VARIANTS_BY_ID["natural"], max_width=320)
    image = Image.open(BytesIO(data))
    assert image.width <= 320 and image.height <= 320


def test_output_has_no_exif(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    data = render_variant_preview(source, VARIANTS_BY_ID["dreamy_golden_hour"])
    assert len(Image.open(BytesIO(data)).getexif()) == 0, "preview must strip EXIF"


def test_render_is_deterministic(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    first = render_variant_preview(source, VARIANTS_BY_ID["cool_blue"])
    second = render_variant_preview(source, VARIANTS_BY_ID["cool_blue"])
    assert first == second, "same source + variant must yield byte-identical output"


def test_render_all_matches_per_variant(tmp_path):
    source = _make_dark_gradient_jpeg(tmp_path / "src.jpg")
    everything = render_all_previews(source, MOOD_VARIANTS)
    assert set(everything) == {variant["id"] for variant in MOOD_VARIANTS}
    for variant in MOOD_VARIANTS:
        assert everything[variant["id"]] == render_variant_preview(source, variant), variant["id"]


# --- recipe is generated from values (display can't drift from capture) --------------------------

@pytest.mark.parametrize("variant", MOOD_VARIANTS, ids=[v["id"] for v in MOOD_VARIANTS])
def test_recipe_string_equals_values(variant):
    target = int(round(float(variant["target_brightness"])))
    expected_wb = variant["white_balance"] or "auto"
    expected = f"brightness target {target} / {expected_wb} WB"
    assert variant_recipe_from_values(variant) == expected
    # intent.MOOD_VARIANTS now COMPUTES its recipe from this function — they must agree.
    assert variant["recipe"] == expected


def main() -> int:
    tests = [
        test_brighter_variant_raises_mean_luma_toward_target,
        test_darker_target_variant_lowers_mean_relative_to_brighter,
        test_warm_variant_raises_red_to_blue_ratio,
        test_neutral_variant_leaves_gray_neutral,
        test_output_is_bounded_to_max_width,
        test_output_has_no_exif,
        test_render_is_deterministic,
        test_render_all_matches_per_variant,
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        for test in tests:
            try:
                test(tmp_path)
            except AssertionError as exc:
                failures.append(f"{test.__name__}: {exc}")
    for variant in MOOD_VARIANTS:
        try:
            test_recipe_string_equals_values(variant)
        except AssertionError as exc:
            failures.append(f"test_recipe_string_equals_values[{variant['id']}]: {exc}")

    if failures:
        print("MOOD PREVIEW TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1
    print("MOOD PREVIEW TESTS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
