#!/usr/bin/env python3
"""List a selected Sony card, or import one explicitly selected photo; never shoot."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))
from card_history import CardHistoryError, list_card, download_card_file
from sonyremote.camera import CameraIdentityError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--port', required=True)
    parser.add_argument('--serial', required=True, help="USB serial of the selected body; required before any card read")
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    download = sub.add_parser('download')
    download.add_argument('--folder', required=True)
    download.add_argument('--name', required=True)
    download.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = list_card(args.model, args.port, serial=args.serial) if args.command == 'list' else download_card_file(args.model, args.port, args.folder, args.name, args.destination, serial=args.serial)
        print(json.dumps(result, indent=2))
        return 0
    except CameraIdentityError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 1
    except (CardHistoryError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'reason': getattr(error, 'reason', 'card_read_failed'), 'message': str(error), 'retry_capture': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
