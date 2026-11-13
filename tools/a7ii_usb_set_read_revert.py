#!/usr/bin/env python3
"""Set/read/revert proof runner for A7 II USB/gphoto registry nodes.

Default mode is dry-run. With --execute, each selected setting is probed by:

1. reading the live gphoto node value and choices,
2. writing a target value,
3. reading back the value,
4. reverting to the original value,
5. writing per-setting JSON artifacts.

Use this only in a controlled USB PC Remote state. Destructive/admin registry rows are always
skipped, and rows without an explicit or auto-selectable target are reported instead of guessed.

--execute requires the exact --model, --port and the body's USB --serial
(tools/inspect_usb_camera.py). The serial is proven at the port on connect, before any write; a
body that cannot be proven is a terminal identity refusal and no setting is written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

DEFAULT_ROUTE_STATUSES = {"available_not_wrapped"}
OPT_IN_ROUTE_STATUSES = {"proven_set", "proven_read"}


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "setting"


def _parse_value(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"--value must be setting_id=value, got {item!r}")
        setting_id, value = item.split("=", 1)
        parsed[setting_id.strip()] = value.strip()
    return parsed


def _selected_rows(
    registry: dict[str, Any],
    *,
    setting_ids: list[str],
    include_proven: bool,
    all_route_gaps: bool,
) -> list[dict[str, Any]]:
    if setting_ids:
        rows = [settings_registry.get_setting(setting_id, registry) for setting_id in setting_ids]
    else:
        route_statuses = set(DEFAULT_ROUTE_STATUSES)
        if include_proven:
            route_statuses |= OPT_IN_ROUTE_STATUSES
        rows = []
        for row in registry["settings"]:
            usb = row.get("routes", {}).get("usb_gphoto")
            if not usb or not usb.get("node"):
                continue
            if all_route_gaps and usb.get("status") != "unsupported_on_a7ii" or usb.get("status") in route_statuses:
                rows.append(row)
    return sorted(rows, key=lambda row: row["setting_id"])


def _dry_row(row: dict[str, Any], explicit_values: dict[str, str]) -> dict[str, Any]:
    usb = row.get("routes", {}).get("usb_gphoto", {})
    guarded = row["status"] == "dangerous_admin" or row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS
    return {
        "setting_id": row["setting_id"],
        "label": row["label"],
        "status": row["status"],
        "node": usb.get("node"),
        "route_status": usb.get("status"),
        "guarded": guarded,
        "target": explicit_values.get(row["setting_id"], "<auto alternate choice>"),
        "next_action": row["next_action"],
    }


def _widget_snapshot(camera: Any, node: str) -> dict[str, Any]:
    gp_camera = camera._require()
    config = gp_camera.get_config()
    widget = config.get_child_by_name(node)
    choices = []
    try:
        choices = [widget.get_choice(index) for index in range(widget.count_choices())]
    except Exception:
        choices = []
    try:
        readonly = bool(widget.get_readonly())
    except Exception:
        readonly = None
    return {
        "node": node,
        "value": str(widget.get_value()),
        "readonly": readonly,
        "choices": [str(choice) for choice in choices],
    }


def _auto_targets(snapshot: dict[str, Any]) -> list[str]:
    current = str(snapshot["value"])
    choices = [str(choice) for choice in snapshot.get("choices") or []]
    if choices:
        targets: list[str] = []

        def add(candidate: str) -> None:
            if candidate != current and candidate not in targets:
                targets.append(candidate)

        try:
            index = choices.index(current)
        except ValueError:
            index = -1
        if index >= 0:
            for candidate_index in (index + 1, index - 1):
                if 0 <= candidate_index < len(choices):
                    add(choices[candidate_index])
        for choice in choices:
            add(choice)
        return targets
    if current in {"0", "1"}:
        return ["1" if current == "0" else "0"]
    lower = current.lower()
    if lower in {"on", "off"}:
        return ["Off" if lower == "on" else "On"]
    return []


def _auto_target(snapshot: dict[str, Any]) -> str | None:
    targets = _auto_targets(snapshot)
    return targets[0] if targets else None


def _values_match(actual: Any, expected: Any, *, choices: list[str] | None = None) -> bool:
    if str(actual) == str(expected):
        return True
    if not choices:
        try:
            return abs(float(actual) - float(expected)) <= 0.01
        except (TypeError, ValueError):
            return False
    return False


def _write_then_snapshot(
    camera: Any,
    node: str,
    value: str,
    *,
    settle_seconds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Write a value, wait for delayed Sony/libgphoto updates, then read the live widget.

    Some Sony nodes, notably still capture mode, can apply after libgphoto's immediate verification
    has already reported failure. The proof runner cares about observed state, so it always takes a
    post-write snapshot and lets the caller decide whether the visible value matches.
    """
    error: str | None = None
    try:
        camera.set_setting(node, str(value))
    except Exception as exc:
        if getattr(exc, "identity_refused", False):
            raise  # terminal: not a delayed write to wait out
        error = f"{exc.__class__.__name__}: {exc}"
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    try:
        return _widget_snapshot(camera, node), error
    except Exception as exc:
        snapshot_error = f"{exc.__class__.__name__}: {exc}"
        return None, f"{error}; post-write read failed: {snapshot_error}" if error else snapshot_error


def _probe_one(
    camera: Any,
    row: dict[str, Any],
    *,
    explicit_values: dict[str, str],
    out_dir: Path,
    settle_seconds: float,
) -> dict[str, Any]:
    setting_id = row["setting_id"]
    usb = row.get("routes", {}).get("usb_gphoto", {})
    node = str(usb.get("node") or "")
    result: dict[str, Any] = {
        "setting_id": setting_id,
        "label": row["label"],
        "node": node,
        "status": row["status"],
        "route_status": usb.get("status"),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "ok": False,
        "skipped": False,
        "error": "",
    }

    if not node:
        result.update({"skipped": True, "error": "no usb_gphoto node registered"})
        return result
    if row["status"] == "dangerous_admin" or setting_id in settings_registry.DESTRUCTIVE_SETTING_IDS:
        result.update({"skipped": True, "error": "destructive/admin setting is guarded"})
        return result

    try:
        before = _widget_snapshot(camera, node)
        result["before"] = before
        if before["readonly"] is True:
            result.update({"skipped": True, "error": "live widget is read-only in this camera state"})
            return result
        explicit_target = explicit_values.get(setting_id)
        targets = [explicit_target] if explicit_target is not None else _auto_targets(before)
        targets = [str(target) for target in targets if target is not None]
        result["candidate_targets"] = targets
        if not targets:
            result.update({"skipped": True, "error": "no alternate choice; pass --value setting_id=value"})
            return result

        attempts: list[dict[str, Any]] = []
        result["attempts"] = attempts
        for target in targets:
            attempt: dict[str, Any] = {"target": str(target), "ok": False}
            attempts.append(attempt)
            after_set, set_error = _write_then_snapshot(
                camera,
                node,
                str(target),
                settle_seconds=settle_seconds,
            )
            if after_set is not None:
                attempt["after_set"] = after_set
                set_ok = _values_match(after_set["value"], target, choices=after_set.get("choices"))
                attempt["set_ok"] = set_ok
                if set_error:
                    attempt["set_warning"] = set_error
            else:
                attempt["set_ok"] = False
                attempt["set_error"] = set_error or "post-write snapshot unavailable"
                set_ok = False

            after_revert, revert_error = _write_then_snapshot(
                camera,
                node,
                str(before["value"]),
                settle_seconds=settle_seconds,
            )
            if after_revert is not None:
                attempt["after_revert"] = after_revert
                revert_ok = _values_match(
                    after_revert["value"],
                    before["value"],
                    choices=after_revert.get("choices"),
                )
                attempt["revert_ok"] = revert_ok
                if revert_error:
                    attempt["revert_warning"] = revert_error
            else:
                attempt["revert_ok"] = False
                attempt["revert_error"] = revert_error or "post-revert snapshot unavailable"
                revert_ok = False

            attempt["ok"] = bool(set_ok and revert_ok)
            if attempt["ok"]:
                result["target"] = str(target)
                result["after_set"] = attempt.get("after_set")
                result["after_revert"] = attempt.get("after_revert")
                result["ok"] = True
                result["set_ok"] = True
                result["revert_ok"] = True
                return result

            if not revert_ok:
                result["target"] = str(target)
                result["after_set"] = attempt.get("after_set")
                result["after_revert"] = attempt.get("after_revert")
                result["set_ok"] = set_ok
                result["revert_ok"] = False
                result["error"] = "stopping after failed revert; camera state may need manual inspection"
                return result

        result["target"] = targets[-1]
        result["after_set"] = attempts[-1].get("after_set")
        result["after_revert"] = attempts[-1].get("after_revert")
        result["set_ok"] = False
        result["revert_ok"] = all(attempt.get("revert_ok") for attempt in attempts)
        result["error"] = "no candidate target produced a set/read/revert match"
        return result
    except Exception as exc:  # proof tool: preserve unexpected failure as artifact
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        if getattr(exc, "identity_refused", False):
            raise  # terminal: never probe another row on a body this run cannot prove
        return result
    finally:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{_slug(setting_id)}.json").write_text(
            json.dumps(result, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", action="append", default=[], help="setting_id to probe; repeatable")
    parser.add_argument("--value", action="append", default=[], help="target as setting_id=value; repeatable")
    parser.add_argument("--out-dir", default="artifacts/falsification/r2_usb_set_read_revert")
    parser.add_argument("--include-proven", action="store_true", help="also regression-test proven/read rows")
    parser.add_argument("--all-route-gaps", action="store_true", help="try every registered USB node gap")
    parser.add_argument("--max-settings", type=int, default=0)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=1.0,
        help="seconds to wait after each write before the verification read",
    )
    parser.add_argument("--execute", action="store_true", help="touch the camera; otherwise dry-run only")
    parser.add_argument("--model", help="exact gphoto2 model, e.g. 'Sony ILCE-7M2 (Control)'; required with --execute")
    parser.add_argument("--port", help="exact USB port, e.g. usb:000,004 (re-detect first); required with --execute")
    parser.add_argument("--serial", help="the body's USB serial (tools/inspect_usb_camera.py); required with "
                                         "--execute, and proven at the port before any setting write")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    registry = settings_registry.load_registry()
    explicit_values = _parse_value(args.value)
    rows = _selected_rows(
        registry,
        setting_ids=args.setting,
        include_proven=args.include_proven,
        all_route_gaps=args.all_route_gaps,
    )
    if args.max_settings > 0:
        rows = rows[: args.max_settings]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (REPO_ROOT / args.out_dir / f"{stamp}-usb-set-read-revert").resolve()
    dry_rows = [_dry_row(row, explicit_values) for row in rows]
    plan = {
        "timestamp": stamp,
        "execute": args.execute,
        "camera_model": args.model,
        "camera_port": args.port,
        "camera_serial": args.serial,
        "out_dir": str(out_dir),
        "settle_seconds": args.settle_seconds,
        "selected_settings": len(rows),
        "rows": dry_rows,
    }

    if not args.execute:
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(f"USB set/read/revert dry-run: {len(rows)} settings")
            print(f"artifact run dir would be: {out_dir}")
            for row in dry_rows:
                guard = " guarded" if row["guarded"] else ""
                print(f"- {row['setting_id']} node={row['node']} route={row['route_status']}{guard}")
                print(f"  target: {row['target']}")
        return 0

    if not (args.model and args.port):
        print(json.dumps({"status": "invalid", "execute": True, "setting_writes": 0,
                          "error": "--model and --port are required with --execute "
                                   "(the exact gphoto2 model and USB port)"}, indent=2, sort_keys=True))
        return 2
    from sonyremote import Camera, CameraIdentityError  # lazy import: dry-runs should not require gphoto2

    if not args.serial:
        # USB ports re-enumerate; a same-model body could hold this port. Write only to a proven body.
        refusal = CameraIdentityError(
            "Pass --serial <USB serial> (tools/inspect_usb_camera.py); the port alone is not an identity.",
            reason="serial_required", model=args.model, port=args.port)
        print(json.dumps({"status": "identity_refused", "execute": True, "setting_writes": 0,
                          "identity": refusal.as_dict()}, indent=2, sort_keys=True))
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    results = []
    identity = None
    verified_serial = None
    try:
        with Camera(model=args.model, port=args.port, serial=args.serial) as camera:
            verified_serial = camera.verified_serial
            for row in rows:
                results.append(
                    _probe_one(
                        camera,
                        row,
                        explicit_values=explicit_values,
                        out_dir=out_dir,
                        settle_seconds=args.settle_seconds,
                    )
                )
    except CameraIdentityError as exc:
        # Terminal: nothing is retried and no other body is chosen; the operator re-selects the camera.
        identity = exc.as_dict()

    summary = {
        "timestamp": stamp,
        "status": "identity_refused" if identity else "complete",
        "out_dir": str(out_dir),
        "camera_model": args.model,
        "camera_port": args.port,
        "verified_serial": verified_serial,
        "selected_settings": len(rows),
        "ok": sum(1 for result in results if result.get("ok")),
        "skipped": sum(1 for result in results if result.get("skipped")),
        "failed": sum(1 for result in results if not result.get("ok") and not result.get("skipped")),
        "results": results,
    }
    if identity:
        summary["identity"] = identity
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(out_dir)
        if identity:
            print(f"identity_refused reason={identity['reason']}: {identity['detail']}")
        print(f"ok={summary['ok']} skipped={summary['skipped']} failed={summary['failed']}")
    if identity:
        return 2
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
