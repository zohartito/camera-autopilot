#!/usr/bin/env python3
"""Exercise fixture external-event/file ingress; no native trigger or shutter control."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from external_trigger import ExternalTriggerIngest, ExternalTriggerError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--frames', type=int, required=True)
    create.add_argument('--settle-seconds', type=float, default=.25)
    create.add_argument('--adapter', default='fixture')
    event = sub.add_parser('event')
    event.add_argument('--sequence', type=int, required=True)
    event.add_argument('--filename', required=True)
    event.add_argument('--sha256', required=True)
    supply = sub.add_parser('supply')
    supply.add_argument('--source', type=Path, required=True)
    settle = sub.add_parser('settle')
    inspect = sub.add_parser('inspect')
    export = sub.add_parser('export')
    for command in [event, supply, settle, inspect, export]:
        command.add_argument('--session', required=True)
    for command in [event, supply, settle]:
        command.add_argument('--event-id', required=True)
    for command in [create, event, supply, settle]:
        command.add_argument('--model', required=True)
        command.add_argument('--port', required=True)
    for command in [supply, settle]:
        command.add_argument('--iso', required=True)
        command.add_argument('--aperture', required=True)
        command.add_argument('--shutter', required=True)
    args = parser.parse_args()
    try:
        runner = ExternalTriggerIngest(args.root)
        if args.command == 'create':
            result = runner.create(args.model, args.port, args.frames, adapter=args.adapter, settle_seconds=args.settle_seconds)
        elif args.command == 'inspect':
            result = runner.read(args.session)
        elif args.command == 'export':
            result = {'frames': runner.render_inputs(args.session), 'adapter': 'fixture', 'native_route_verified': False, 'retry_capture': False}
        else:
            camera = {'model': args.model, 'port': args.port}
            if args.command == 'event':
                result = runner.observe_event(args.session, args.event_id, args.sequence, args.filename, args.sha256, camera)
            else:
                exposure = {'iso': args.iso, 'aperture': args.aperture, 'shutter': args.shutter}
                if args.command == 'supply':
                    result = runner.supply_file(args.session, args.event_id, args.source, camera, exposure)
                else:
                    result = runner.observe_exposure(args.session, args.event_id, camera, exposure)
        print(json.dumps(result, indent=2))
        return 0
    except (ExternalTriggerError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'reason': getattr(error, 'reason', 'ingress_failed'),
                          'message': str(error), 'retry_capture': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
