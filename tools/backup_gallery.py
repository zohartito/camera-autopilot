#!/usr/bin/env python3
"""Copy a local gallery to an explicit directory, verifying every SHA256."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from photo_backup import backup_gallery, BackupError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--image', action='append', help='Exact root-level photo filename; repeat, or omit for all photos.')
    args = parser.parse_args()
    try:
        print(json.dumps(backup_gallery(args.source, args.destination, args.image), indent=2))
        return 0
    except (BackupError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'reason': getattr(error, 'reason', 'backup_failed'), 'message': str(error), 'original_deleted': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
