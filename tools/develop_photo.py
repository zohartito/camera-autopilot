#!/usr/bin/env python3
"""Develop an existing local photo without camera access or uploads."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from isolated_development import DevelopmentError, run_development


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--dng', action='store_true', help='Export the 16-bit output-referred DNG after development')
    for name, default in [('exposure-ev', 0), ('wb-red', 1), ('wb-blue', 1), ('highlight-compression', 0), ('tone-strength', 0)]:
        parser.add_argument('--' + name, type=float, default=default)
    args = parser.parse_args()
    options = {name: getattr(args, name) for name in ['exposure_ev', 'wb_red', 'wb_blue', 'highlight_compression', 'tone_strength']}
    try:
        result = run_development(args.source, args.output_root, options)
        if args.dng:
            from linear_dng import export_linear_dng
            job = args.output_root / result['job_id']
            dng = export_linear_dng(job / 'linear.tiff', job / 'receipt.json', args.output_root / 'dng')
            result['linear_dng'] = dng
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, DevelopmentError) as error:
        print(json.dumps({'status': 'failed', 'message': str(error), 'reason': getattr(error, 'reason', 'invalid_options'), 'retry_capture': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
