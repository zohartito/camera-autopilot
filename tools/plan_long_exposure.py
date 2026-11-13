#!/usr/bin/env python3
"""Plan an independent-frame long-exposure average without camera or network access."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from camera_caps import _shutter_seconds
from long_exposure_plan import plan_long_exposure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("duration_s", type=float)
    parser.add_argument("--shutter", required=True, help="confirmed current shutter, such as1/60 or2")
    parser.add_argument("--max-frames", type=int, default=16)
    args = parser.parse_args(argv)
    try:
        result = plan_long_exposure(args.duration_s, _shutter_seconds(args.shutter), frame_budget=args.max_frames)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
