#!/usr/bin/env python3
"""Process existing overlapping JPEGs; no camera access or automatic capture."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from panorama import PROJECTIONS, PanoramaError, _layout, build_panorama, capture_plan, guided_plan


def _csv_floats(text, expected, name):
    try:
        values = [float(part) for part in text.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'{name} needs comma-separated numbers') from exc
    if len(values) != expected:
        raise argparse.ArgumentTypeError(f'{name} needs {expected} comma-separated numbers')
    return values


def _csv_ints(text, expected, name):
    try:
        values = [int(part) for part in text.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'{name} needs comma-separated integers') from exc
    if len(values) != expected:
        raise argparse.ArgumentTypeError(f'{name} needs {expected} comma-separated integers')
    return values


def _layout_arg(text):
    """The session API's {"rows": R, "columns": C} object, or the older "R,C" pair."""
    if text.lstrip().startswith('{'):
        try:
            layout = json.loads(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('--layout JSON must be {"rows": R, "columns": C}') from exc
        if not isinstance(layout, dict) or set(layout) - {'rows', 'columns', 'order'} or layout.get('order', 'serpentine') != 'serpentine':
            raise argparse.ArgumentTypeError('--layout JSON must be {"rows": R, "columns": C}')
        layout = {'rows': layout.get('rows'), 'columns': layout.get('columns')}
    else:
        layout = dict(zip(('rows', 'columns'), _csv_ints(text, 2, '--layout')))
    try:
        _layout(layout)  # positive integers describing 2..12 frames
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'--layout: {exc}') from exc
    return layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='*', help='2..12 existing overlapping JPEG files')
    parser.add_argument('--output-root', type=Path, default=Path('artifacts/panoramas'))
    parser.add_argument('--projection', choices=PROJECTIONS, default='spherical')
    parser.add_argument('--horizontal-fov', type=float, default=90,
                        help='degrees; the virtual view of a planar mosaic, and the lens field of view a '
                             'panned sweep falls back on when the JPEGs carry no 35 mm focal length')
    parser.add_argument('--rotation', type=float, default=0)
    parser.add_argument('--no-autocrop', action='store_true')
    parser.add_argument('--no-level-horizon', action='store_true')
    parser.add_argument('--crop', help='normalized left,top,right,bottom of the auto-cropped result')
    parser.add_argument('--layout', type=_layout_arg,
                        help='multi-row grid in serpentine capture order, as the session API\'s '
                             '\'{"rows": 2, "columns": 3}\' or the older "2,3"')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--plan', nargs=2, type=int, metavar=('ROWS', 'COLUMNS'))
    parser.add_argument('--overlap', type=float, default=.35)
    parser.add_argument('--focal-length', type=float, help='lens focal length in millimetres')
    parser.add_argument('--sensor-width', type=float, help='sensor width in millimetres')
    parser.add_argument('--sensor-height', type=float, help='sensor height in millimetres')
    parser.add_argument('--yaw-span', type=float, help='scene yaw coverage in degrees')
    parser.add_argument('--pitch-span', type=float, help='scene pitch coverage in degrees')
    args = parser.parse_args()
    try:
        optics = (args.focal_length, args.sensor_width, args.sensor_height, args.yaw_span, args.pitch_span)
        if args.plan and any(value is not None for value in optics):
            parser.error('--plan is the explicit grid; use --focal-length and sensor/span instead of mixing')
        if args.plan:
            if args.sources:
                parser.error('--plan does not accept image sources')
            receipt = guided_plan(*args.plan, overlap=args.overlap)
        elif any(value is not None for value in optics):
            if args.sources:
                parser.error('an optics plan does not accept image sources')
            if any(value is None for value in optics):
                parser.error('optics plan needs --focal-length --sensor-width --sensor-height --yaw-span --pitch-span')
            receipt = capture_plan(focal_length_mm=args.focal_length, sensor_width_mm=args.sensor_width,
                                   sensor_height_mm=args.sensor_height, overlap=args.overlap,
                                   yaw_span_deg=args.yaw_span, pitch_span_deg=args.pitch_span)
        else:
            layout = args.layout
            crop = None if not args.crop else _csv_floats(args.crop, 4, '--crop')
            receipt = build_panorama(args.sources, args.output_root, projection=args.projection,
                                     horizontal_fov_deg=args.horizontal_fov, rotation_deg=args.rotation,
                                     autocrop=not args.no_autocrop, timeout_seconds=args.timeout,
                                     level_horizon=not args.no_level_horizon, crop=crop, layout=layout)
        print(json.dumps(receipt, indent=2))
        return 0
    except (ValueError, PanoramaError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc), 'reason': getattr(exc, 'reason', 'invalid_request'),
                          'job_id': getattr(exc, 'job_id', None)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
