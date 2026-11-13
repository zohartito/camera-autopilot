#!/usr/bin/env python3
"""Export our completed development TIFF as an output-referred linear DNG."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from linear_dng import DNGError, export_linear_dng, validate_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tiff', type=Path)
    parser.add_argument('development_receipt', type=Path)
    parser.add_argument('--output-root', type=Path, default=Path('artifacts/linear-dng'))
    parser.add_argument('--verification-python', type=Path)
    parser.add_argument('--validate-input-only', action='store_true')
    args = parser.parse_args()
    try:
        result = validate_input(args.tiff, args.development_receipt) if args.validate_input_only else export_linear_dng(args.tiff, args.development_receipt, args.output_root, verification_python=args.verification_python)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'failed', 'reason': getattr(exc, 'reason', 'invalid_input'), 'job_id': getattr(exc, 'job_id', None), 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
