#!/usr/bin/env python3
"""Measure a body's JPEG-domain noise-vs-ISO curve from an ISO-ladder of the SAME static scene.

Shoot one static, evenly-lit scene on a tripod at each ISO on the ladder, letting the shutter
compensate so the resulting LIGHTNESS is held constant (same metered exposure, only ISO/shutter
change). Point this tool at the folder of JPEGs and it writes data/sensor_profiles/<body>.json:
a per-ISO luma/chroma noise sigma (8-bit) plus provenance.

What this is and is NOT:
  - It is the standard deviation of 8-bit luma in automatically-chosen flat, mid-tone patches,
    after subtracting a 5x5 median to strip scene texture — an honest *relative* JPEG-domain
    noise curve and a usable-ISO ceiling.
  - It is NOT RAW read noise, dynamic range, or a lens MTF. The numbers fold in the camera's
    JPEG tone curve and any in-camera noise reduction, so they are only comparable within one
    body/JPEG-setting. The rig is labelled `measured_jpeg_noise`, never fully calibrated.

Usage:
  .venv/bin/python tools/calibrate_sensor_noise.py <jpeg_dir> \
      --body ILCE-7M3 --lens "FE 24-105mm F4 G OSS" --out data/sensor_profiles/ilce-7m3.json
  .venv/bin/python tools/calibrate_sensor_noise.py <jpeg_dir> --report      # print table, no write

Deterministic: same inputs -> identical noise table (fixed cell grid, fixed selection order); only
the provenance date differs between runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from math import log2
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

CELL = 64                    # flat-patch cell size in pixels
DEFAULT_CELLS = 24           # number of flat cells to measure over
MIDTONE_LO, MIDTONE_HI = 60, 190
DEFAULT_TOLERANCE_STOPS = 0.15
DEFAULT_THRESHOLD_RATIO = 2.5

# Manufacturer spec geometry, recorded as provenance (spec-derived, not measured here).
_BODY_GEOMETRY = {
    "ILCE-7M3": {"width_mm": 35.6, "height_mm": 23.8, "width_px": 6000, "height_px": 4000},
    "ILCE-7M2": {"width_mm": 35.8, "height_mm": 23.9, "width_px": 6000, "height_px": 4000},
}


def _rational(value) -> float:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        num, den = value
        if den == 0:
            raise ValueError(f"zero-denominator rational EXIF value: {value!r}")
        return float(num) / float(den)
    try:
        return float(value)                              # int, float, or PIL IFDRational
    except (TypeError, ValueError):
        raise ValueError(f"unreadable rational EXIF value: {value!r}")


def read_exposure(path: Path) -> dict:
    """Pull ISO / shutter seconds / f-number off a JPEG's EXIF (ExifIFD or base)."""
    with Image.open(path) as image:
        exif = image.getexif()
    sub = exif.get_ifd(0x8769) if 0x8769 in exif else exif
    iso = sub.get(0x8827, exif.get(0x8827))
    exposure_time = sub.get(0x829A, exif.get(0x829A))
    f_number = sub.get(0x829D, exif.get(0x829D))
    if iso is None or exposure_time is None or f_number is None:
        raise ValueError(f"{path.name}: EXIF is missing ISO, ExposureTime or FNumber")
    iso = int(iso[0]) if isinstance(iso, (tuple, list)) else int(iso)
    shutter_s = _rational(exposure_time)
    f = _rational(f_number)
    if not (iso > 0 and shutter_s > 0 and f > 0):
        raise ValueError(f"{path.name}: non-positive ISO/shutter/aperture in EXIF")
    return {"iso": iso, "shutter_s": shutter_s, "f_number": f,
            "achieved_ev100": log2(f * f / shutter_s) - log2(iso / 100.0)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _luma(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype=np.float64)


def _ycbcr(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with Image.open(path) as image:
        arr = np.asarray(image.convert("YCbCr"), dtype=np.float64)
    return arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]


def _cell_boxes(shape: tuple[int, int]) -> list[tuple[int, int]]:
    rows, cols = shape[0] // CELL, shape[1] // CELL
    return [(r * CELL, c * CELL) for r in range(rows) for c in range(cols)]


def _residual_std(patch: np.ndarray) -> float:
    """Std of a patch after subtracting its 5x5 median (removes scene texture, keeps noise)."""
    median = np.asarray(
        Image.fromarray(patch.astype(np.uint8), mode="L").filter(ImageFilter.MedianFilter(5)),
        dtype=np.float64)
    return float((patch - median).std())


def select_flat_cells(reference: Path, count: int) -> list[tuple[int, int]]:
    """Deterministically pick the `count` flattest mid-tone cells of the reference (base-ISO) frame."""
    luma = _luma(reference)
    scored = []
    for (y, x) in _cell_boxes(luma.shape):
        cell = luma[y:y + CELL, x:x + CELL]
        mean = float(cell.mean())
        if not (MIDTONE_LO <= mean <= MIDTONE_HI):
            continue
        gradient = float(np.abs(np.diff(cell, axis=0)).mean() + np.abs(np.diff(cell, axis=1)).mean())
        scored.append((gradient, y, x))
    if len(scored) < count:
        raise ValueError(f"only {len(scored)} mid-tone cells found in {reference.name}; "
                         f"need {count}. Use a flatter, evenly-lit target.")
    scored.sort(key=lambda item: (item[0], item[1], item[2]))  # flattest first, ties by position
    return [(y, x) for _, y, x in scored[:count]]


def measure_frame(path: Path, cells: list[tuple[int, int]]) -> tuple[float, float]:
    """Median (over cells) luma and chroma residual sigma for one frame, in 8-bit units."""
    luma = _luma(path)
    _, cb, cr = _ycbcr(path)
    luma_sigmas, chroma_sigmas = [], []
    for (y, x) in cells:
        luma_sigmas.append(_residual_std(luma[y:y + CELL, x:x + CELL]))
        cb_sigma = _residual_std(cb[y:y + CELL, x:x + CELL])
        cr_sigma = _residual_std(cr[y:y + CELL, x:x + CELL])
        chroma_sigmas.append((cb_sigma + cr_sigma) / 2.0)
    return float(np.median(luma_sigmas)), float(np.median(chroma_sigmas))


def build_profile(jpeg_dir: Path, *, body: str, lens: str, cells: int,
                  tolerance_stops: float, threshold_ratio: float) -> dict:
    paths = sorted(p for p in jpeg_dir.iterdir()
                   if p.suffix.lower() in {".jpg", ".jpeg"} and p.is_file())
    if len(paths) < 3:
        raise ValueError(f"need at least 3 ISO-ladder JPEGs in {jpeg_dir}, found {len(paths)}")
    exposures = {p: read_exposure(p) for p in paths}

    evs = [e["achieved_ev100"] for e in exposures.values()]
    spread = max(evs) - min(evs)
    if spread > 2 * tolerance_stops:
        detail = ", ".join(f"{p.name}:EV{exposures[p]['achieved_ev100']:.2f}" for p in paths)
        raise ValueError(
            f"metered exposure is not held constant across the ladder (spread {spread:.2f} stops > "
            f"{2 * tolerance_stops:.2f}). Hold lightness constant by compensating shutter for ISO. "
            f"[{detail}]")

    # One ISO may not repeat: keep the lowest-ISO frame as the flatness reference.
    by_iso = sorted(paths, key=lambda p: exposures[p]["iso"])
    isos_seen = [exposures[p]["iso"] for p in by_iso]
    if len(set(isos_seen)) != len(isos_seen):
        raise ValueError(f"duplicate ISO in the ladder: {isos_seen}. One frame per ISO.")
    reference = by_iso[0]
    flat_cells = select_flat_cells(reference, cells)

    table = []
    for p in by_iso:
        luma_sigma, chroma_sigma = measure_frame(p, flat_cells)
        table.append({"iso": exposures[p]["iso"],
                      "sigma_luma_8bit": round(luma_sigma, 4),
                      "sigma_chroma_8bit": round(chroma_sigma, 4),
                      "shutter_s": round(exposures[p]["shutter_s"], 8),
                      "f_number": round(exposures[p]["f_number"], 3),
                      "file": p.name})
    lumas = [row["sigma_luma_8bit"] for row in table]
    if not all(b >= a - 1e-6 for a, b in zip(lumas, lumas[1:])):
        # Not fatal, but suspicious: noise should not fall as ISO rises. Surface it.
        print(f"WARNING: luma sigma is not monotonic with ISO: {lumas}", file=sys.stderr)

    base_iso = isos_seen[0]

    def ceiling() -> int:
        base = lumas[0]
        result = isos_seen[0]
        for iso, sigma in zip(isos_seen, lumas):
            if sigma > threshold_ratio * base + 1e-9:
                break  # first ISO over the bar ends the usable range
            result = iso
        return result

    profile = {
        "body": body,
        "lens": lens,
        "sensor": _BODY_GEOMETRY.get(body),
        "base_iso": base_iso,
        "ceiling_threshold_ratio": threshold_ratio,
        "max_usable_iso": ceiling(),
        "noise_table": [{"iso": r["iso"], "sigma_luma_8bit": r["sigma_luma_8bit"],
                         "sigma_chroma_8bit": r["sigma_chroma_8bit"]} for r in table],
        "provenance": {
            "date": date.today().isoformat(),
            "method": "jpeg_flat_patch_residual",
            "lens": lens,
            "cells": cells,
            "cell_size_px": CELL,
            "midtone_window_8bit": [MIDTONE_LO, MIDTONE_HI],
            "exposure_tolerance_stops": tolerance_stops,
            "frames": [{"file": r["file"], "iso": r["iso"], "shutter_s": r["shutter_s"],
                        "f_number": r["f_number"],
                        "sha256": _sha256(jpeg_dir / r["file"])} for r in table],
            "limitations": ("JPEG tone-curve and in-camera noise-reduction dependent; not RAW read "
                            "noise, dynamic range, or lens MTF. Comparable only within this body "
                            "and JPEG setting."),
        },
    }
    return profile


def _print_report(profile: dict) -> None:
    print(f"body {profile['body']}  lens {profile['lens']}  base ISO {profile['base_iso']}")
    print(f"  usable-ISO ceiling {profile['max_usable_iso']} "
          f"(<= {profile['ceiling_threshold_ratio']}x base luma sigma)")
    print("  ISO     sigma_luma  sigma_chroma")
    for row in profile["noise_table"]:
        print(f"  {row['iso']:<7} {row['sigma_luma_8bit']:<11} {row['sigma_chroma_8bit']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jpeg_dir", type=Path, help="folder of ISO-ladder JPEGs of one static scene")
    parser.add_argument("--body", default="", help="canonical body id, e.g. ILCE-7M3")
    parser.add_argument("--lens", default="", help="mounted lens description for provenance")
    parser.add_argument("--out", type=Path, help="write the profile JSON here")
    parser.add_argument("--cells", type=int, default=DEFAULT_CELLS)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_STOPS,
                        help="max +/- stops the metered exposure may vary across the ladder")
    parser.add_argument("--threshold-ratio", type=float, default=DEFAULT_THRESHOLD_RATIO,
                        help="usable-ISO ceiling = highest ISO with luma sigma <= ratio x base")
    parser.add_argument("--report", action="store_true", help="print the table (no write)")
    args = parser.parse_args(argv)

    if not args.out and not args.report:
        parser.error("pass --out to write the profile, or --report to only print it")
    if args.out and not args.body:
        parser.error("--body is required when writing a profile (it names the output file's rig)")

    profile = build_profile(args.jpeg_dir, body=args.body, lens=args.lens, cells=args.cells,
                            tolerance_stops=args.tolerance, threshold_ratio=args.threshold_ratio)
    _print_report(profile)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
