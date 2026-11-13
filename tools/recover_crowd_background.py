#!/usr/bin/env python3
"""Recover background and export mandatory uncertainty maps.

Explicit-mask mode (default): masks are caller-asserted binary grayscale PNGs
where white asserts visible background and black means unknown/occluded.

    python tools/recover_crowd_background.py --registered \
      --frame image1.jpg background1.png --frame image2.jpg background2.png \
      --output new-inspection-bundle

Automatic mode (--auto): no masks are supplied. A per-pixel temporal estimator
labels background automatically and feeds the same recovery; the bundle carries
the estimator's honesty statistics and a status of "ok" or
"insufficient_background_visibility".

    python tools/recover_crowd_background.py --auto --registered \
      --image image1.jpg --image image2.jpg --image image3.jpg \
      --output new-inspection-bundle

Inputs must already share a registered pixel grid. The bundle includes validity,
never-observed and support maps plus provenance. Existing output directories are
never overwritten. No camera or network access.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def _auto_main(argv):
    parser = argparse.ArgumentParser(description="automatic crowd background recovery")
    parser.add_argument("--auto", action="store_true", required=True)
    parser.add_argument("--image", action="append", required=True, metavar="IMAGE",
                        help="registered source image; repeat for every frame")
    parser.add_argument("--registered", action="store_true",
                        help="assert all source images share a registered pixel grid")
    parser.add_argument("--minimum-observations", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True,
                        help="new private bundle directory; never overwritten")
    args = parser.parse_args(argv)
    if not args.registered:
        parser.error("automatic recovery requires --registered: sources must already share a registered pixel grid")
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; choose a new directory")

    import crowd_auto_mask
    try:
        outcome = crowd_auto_mask.recover_background_auto(
            args.image, args.output / "work", minimum_observations=args.minimum_observations)
        root = outcome["result"].save_bundle(args.output / "bundle")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "bundle": str(root.resolve()),
        "evidence": str((root / "evidence.json").resolve()),
        "status": outcome["status"],
        "low_confidence_fraction": outcome["low_confidence_fraction"],
        "masks": outcome["mask_paths"],
    }))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--auto" in argv:
        return _auto_main(argv)
    from crowd_recovery import main as explicit_main
    return explicit_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
