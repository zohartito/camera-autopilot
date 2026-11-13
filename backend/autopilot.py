"""Unified autopilot CLI for the A7 II — one entry point over the six all-WiFi flows.

Every subcommand DISPATCHES to an existing, already-tested callable; none of the capture/merge logic
is reimplemented here. The submodules are imported LAZILY inside each handler so this CLI stays
importable (and `modes` / `--list` keep working) under the plain interpreter, where the heavy ML/CV
stack (brain -> open_clip, cv2) that wifi_stack/wifi_pano/wifi_night pull in is absent.

    backend/.venv-ml/bin/python backend/autopilot.py modes        # list every mode, NO camera needed
    backend/.venv-ml/bin/python backend/autopilot.py shot
    backend/.venv-ml/bin/python backend/autopilot.py hdr --frames 3 --ev-step 2.0
    backend/.venv-ml/bin/python backend/autopilot.py longexp --frames 5
    backend/.venv-ml/bin/python backend/autopilot.py pano --frames 6
    backend/.venv-ml/bin/python backend/autopilot.py night --frames 8 --shutter 15 --iso 3200
    backend/.venv-ml/bin/python backend/autopilot.py timelapse --frames 30 --interval 5
    backend/.venv-ml/bin/python backend/autopilot.py track --seconds 30 --fps 5
    backend/.venv-ml/bin/python backend/autopilot.py demo [--track]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# Every mode in plain English — the single source of truth for both `modes`/`--list` (camera-free)
# and the subcommand help. (subcommand, one-line description).
MODES: list[tuple[str, str]] = [
    ("shot", "One full autopilot still: see the subject, meter the light, set ISO/aperture/shutter, "
             "focus, and fire — hands-off."),
    ("hdr", "Exposure-bracket HDR: shoot a shutter bracket, then Mertens-fuse the frames into one "
            "high-dynamic-range image."),
    ("longexp", "Digital long exposure: average a same-settings burst to smooth motion (water, "
                "clouds) and cut noise."),
    ("focus", "Focus stack: merge a burst for front-to-back sharpness."),
    ("crowd", "Crowd removal: median-merge a burst to erase moving people from a static scene."),
    ("pano", "Panorama: sweep frames at locked focus + exposure, then stitch them into one wide "
             "panorama."),
    ("night", "Clean night image: stack many short (<=30s, no bulb over WiFi) frames and average them "
              "into one low-noise night still."),
    ("timelapse", "Intervalometer: fire N shots spaced --interval seconds apart (wall-clock cadence)."),
    ("track", "Real-time focus tracking: keep native AF locked on the auto-chosen subject as it moves "
              "(~5 fps)."),
    ("demo", "Narrated live demo: the laptop talks through every decision as it shoots (add --track to "
             "show it follow a moving subject)."),
]


def _connect_or_exit():
    """One shared connect — return a live camera or print the clear no-camera message and exit.

    Centralizes the message every old __main__ printed: open Smart Remote Control on the A7 II and
    join its WiFi AP. Imported lazily so `modes` / `--list` never need wifi_a7ii (or a camera)."""
    import wifi_a7ii

    cam = wifi_a7ii.connect()
    if not cam:
        print("No camera. Open Smart Remote Control on the A7 II and join its WiFi AP first.\n"
              "  1. On the A7 II: MENU -> Application -> Smart Remote Embedded (camera becomes a WiFi AP)\n"
              "  2. Join that AP (DIRECT-xxxx:ILCE-7M2) from THIS host\n"
              "  3. Re-run. (This host needs the camera's subnet, not internet.)")
        raise SystemExit(1)
    return cam


def _print_result(result) -> None:
    """Dump a handler's return value as pretty JSON (matches every old __main__'s output)."""
    import json
    print(json.dumps(result, indent=2, default=str))


def print_modes() -> None:
    """List every mode in plain English — works with NO camera and NO ML stack."""
    print("Camera autopilot — available modes:\n")
    width = max(len(name) for name, _ in MODES)
    for name, description in MODES:
        print(f"  {name.ljust(width)}  {description}")


# --- per-subcommand handlers: each imports its submodule lazily + calls the existing callable -------
# Stacking modes share one handler; the CLI verb maps onto wifi_stack.live_stack's `mode` argument.
_STACK_MODE = {"hdr": "hdr", "longexp": "long_exposure",
               "focus": "focus_stack", "crowd": "crowd_remove"}


def _cmd_shot(args) -> None:
    import wifi_autopilot
    _print_result(wifi_autopilot.shoot(_connect_or_exit(), full_res_dir=args.full_res))


def _cmd_stack(args) -> None:
    import wifi_stack
    _print_result(wifi_stack.live_stack(
        _connect_or_exit(), mode=_STACK_MODE[args.command],
        frames=args.frames, ev_step=args.ev_step, out_path=args.out,
        save_dir=args.save_dir))


def _cmd_pano(args) -> None:
    import wifi_pano
    _print_result(wifi_pano.live_pano(_connect_or_exit(), frames=args.frames, out_path=args.out))


def _cmd_night(args) -> None:
    import wifi_night
    _print_result(wifi_night.night_stack(
        _connect_or_exit(), frames=args.frames, shutter=args.shutter, iso=args.iso))


def _cmd_timelapse(args) -> None:
    import wifi_night
    _print_result(wifi_night.intervalometer(
        _connect_or_exit(), frames=args.frames, interval_s=args.interval))


def _cmd_track(args) -> None:
    import wifi_tracking
    state = wifi_tracking.track(_connect_or_exit(), fps=args.fps, seconds=args.seconds)
    print(f"tracked {state.frames} frames @ {state.mean_fps:.1f} fps  "
          f"(AF commands {state.af_commands}, locks {state.af_locks}, vision misses {state.misses})")


def _cmd_demo(args) -> None:
    import wifi_demo
    cam = _connect_or_exit()
    if args.track:
        wifi_demo.narrated_track(cam)
    else:
        wifi_demo.narrated_shot(cam)


def _cmd_modes(_args) -> None:
    print_modes()


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree: one subcommand per mode, each wired to its handler."""
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Unified all-WiFi autopilot for the Sony A7 II — one CLI over every capture mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run `autopilot modes` (or any subcommand with --help) for details. "
               "Every mode needs the A7 II in Smart Remote Control with this host on its WiFi AP, "
               "except `modes`, which needs no camera.")
    parser.add_argument("--list", action="store_true",
                        help="list every mode in plain English (no camera needed) and exit")
    sub = parser.add_subparsers(dest="command", metavar="<mode>")

    p = sub.add_parser("shot", help="one full autopilot still (meter, set, focus, fire)")
    p.add_argument("--full-res", metavar="DIR", default=None,
                   help="also pull the full-res JPEG off the card into DIR")
    p.set_defaults(func=_cmd_shot)

    # the four stacking modes share argparse options + one handler; only the verb (-> live_stack mode) differs
    for verb, blurb in (("hdr", "exposure-bracket HDR (Mertens fusion)"),
                        ("longexp", "digital long exposure (average a burst)"),
                        ("focus", "focus stack (front-to-back sharpness)"),
                        ("crowd", "crowd removal (median-merge a burst)")):
        sp = sub.add_parser(verb, help=blurb)
        sp.add_argument("--frames", type=int, default=3, help="frames to capture (default 3)")
        sp.add_argument("--ev-step", type=float, default=2.0,
                        help="EV between bracket frames (hdr only; default 2.0)")
        sp.add_argument("--out", default="/tmp/wifi_stack/merged.jpg", help="output image path")
        sp.add_argument("--full-res", "--save-dir", dest="save_dir", metavar="DIR",
                        default="/tmp/wifi_stack",
                        help="save transferred full-res stack frames into DIR (default /tmp/wifi_stack)")
        sp.set_defaults(func=_cmd_stack)

    p = sub.add_parser("pano", help="panorama sweep + stitch")
    p.add_argument("--frames", type=int, default=6, help="frames in the sweep (default 6)")
    p.add_argument("--out", default="/tmp/pano.jpg", help="output panorama path")
    p.set_defaults(func=_cmd_pano)

    p = sub.add_parser("night", help="clean night image (stack short frames)")
    p.add_argument("--frames", type=int, default=8, help="frames to stack (default 8)")
    p.add_argument("--shutter", default="15", help="seconds or Sony label; clamped to <=30s (no bulb)")
    p.add_argument("--iso", default="3200", help="ISO for the night stack")
    p.set_defaults(func=_cmd_night)

    p = sub.add_parser("timelapse", help="intervalometer (N shots, --interval apart)")
    p.add_argument("--frames", type=int, default=30, help="number of shots (default 30)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="wall-clock seconds between shots (default 5)")
    p.set_defaults(func=_cmd_timelapse)

    p = sub.add_parser("track", help="real-time focus tracking (~5 fps)")
    p.add_argument("--seconds", type=float, default=30.0, help="how long to track (default 30)")
    p.add_argument("--fps", type=float, default=5.0, help="target frame rate (default 5)")
    p.set_defaults(func=_cmd_track)

    p = sub.add_parser("demo", help="narrated live demo of the autopilot")
    p.add_argument("--track", action="store_true", help="demo focus tracking instead of a single shot")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("modes", help="list every mode in plain English (no camera needed)")
    p.set_defaults(func=_cmd_modes)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    # --list short-circuits before any camera/ML import — list the modes and stop.
    if getattr(args, "list", False):
        print_modes()
        return
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
