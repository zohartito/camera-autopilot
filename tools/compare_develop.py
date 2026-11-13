#!/usr/bin/env python3
"""Score a developed JPEG against a camera JPEG. Camera-free; no training."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from develop_compare import score_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('developed', type=Path, help='Developed JPEG')
    parser.add_argument('camera_jpeg', type=Path, help='Camera JPEG twin')
    parser.add_argument('--raw', type=Path, help='Optional local Sony ARW to develop first')
    parser.add_argument('--output-root', type=Path, help='Required with --raw')
    args = parser.parse_args()
    developed = args.developed
    if args.raw is not None:
        if args.output_root is None:
            parser.error('--output-root is required with --raw')
        from isolated_development import DevelopmentError, run_development
        try:
            result = run_development(args.raw, args.output_root)
        except (ValueError, DevelopmentError) as error:
            print(json.dumps({'status': 'failed', 'message': str(error),
                              'reason': getattr(error, 'reason', 'invalid_options'),
                              'retry_capture': False, 'real_file_develop': 'unproven'}),
                  file=sys.stderr)
            return 1
        developed = args.output_root / result['job_id'] / 'developed.jpg'
        print(json.dumps({'development': result, 'scoreboard': score_paths(developed, args.camera_jpeg)}, indent=2))
        return 0
    print(json.dumps(score_paths(developed, args.camera_jpeg), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
