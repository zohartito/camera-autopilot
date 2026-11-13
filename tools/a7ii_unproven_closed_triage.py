#!/usr/bin/env python3
"""Triage settings whose route gaps closed without behavior proof and sit in no queue.

The closure report tracks `unproven_closed_setting_ids`: rows every route stopped queueing
(candidate sightings, failed attempts, proof-app not-exposed) that still have no behavior proof
anywhere. This report gives each one an explicit owner-decision bucket so none of them stays
closed by silence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_closure
import settings_evidence
import settings_registry

BUCKET_RECOMMENDATIONS = {
    "deliberate_deferral_documented": (
        "The registry next_action already scopes this to product need. Record an explicit "
        "deferred/wont_do decision if it should stop appearing here."
    ),
    "state_gated_retest_live": (
        "Set-control was refused in the recorded camera state. Retest live in the required state "
        "(flash attached, JPEG-only, power-zoom lens, ...) or accept the read-only evidence."
    ),
    "read_only_exposure_candidate": (
        "Read behavior is proven but no route has a proven setter. Decide whether set-control is "
        "required; if read-only is acceptable, expose it in the settings API and record the decision."
    ),
    "needs_route_decision": (
        "No current route reaches this setting with proof. Scope it into the Camera Remote Command "
        "package review once the package lands, or record an explicit deferral."
    ),
}

BUCKET_ORDER = (
    "state_gated_retest_live",
    "read_only_exposure_candidate",
    "needs_route_decision",
    "deliberate_deferral_documented",
)


def _bucket(row: dict) -> str:
    next_action = str(row.get("next_action") or "").lower()
    if "only if" in next_action or "if needed" in next_action:
        return "deliberate_deferral_documented"
    if any(info.get("status") == "state_gated" for info in row.get("routes", {}).values()):
        return "state_gated_retest_live"
    if row["status"] == "proven_read":
        return "read_only_exposure_candidate"
    return "needs_route_decision"


def build_report(*, registry: dict | None = None) -> dict:
    registry = registry or settings_registry.load_registry()
    closure = settings_closure.build_report(registry=registry, sample_limit=1)
    evidence = settings_evidence.build_evidence_report(registry=registry)
    reviews_by_id = {item["setting_id"]: item["reviews"] for item in evidence["settings"]}

    items = []
    for setting_id in closure["summary"]["unproven_closed_setting_ids"]:
        row = settings_registry.get_setting(setting_id, registry)
        bucket = _bucket(row)
        items.append(
            {
                "setting_id": setting_id,
                "label": row["label"],
                "status": row["status"],
                "bucket": bucket,
                "recommendation": BUCKET_RECOMMENDATIONS[bucket],
                "routes": {name: info.get("status") for name, info in row.get("routes", {}).items()},
                "closing_verdicts": sorted(
                    {
                        f"{review.get('route')}:{review.get('verdict')}"
                        for review in reviews_by_id.get(setting_id, [])
                        if review.get("verdict")
                    }
                ),
                "next_action": row.get("next_action"),
            }
        )

    bucket_counts: dict[str, int] = {}
    for item in items:
        bucket_counts[item["bucket"]] = bucket_counts.get(item["bucket"], 0) + 1

    return {
        "summary": {
            "unproven_closed_settings": len(items),
            "bucket_counts": dict(sorted(bucket_counts.items())),
            "closed_without_proof_rows": closure["summary"]["closed_without_proof_rows"],
            "closed_without_proof_notes": closure["closed_without_proof_notes"],
        },
        "items": items,
    }


def to_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Unproven-Closed Settings Triage",
        "",
        f"- unproven_closed_settings: `{summary['unproven_closed_settings']}`",
        f"- closed_without_proof_rows: `{summary['closed_without_proof_rows']}`",
        "",
        "## Bucket Counts",
        "",
    ]
    for bucket, count in summary["bucket_counts"].items():
        lines.append(f"- `{bucket}`: {count}")
    lines.extend(["", "## Route Closure Notes", ""])
    for note in summary["closed_without_proof_notes"]:
        lines.append(f"- {note}")
    for bucket in BUCKET_ORDER:
        bucket_items = [item for item in report["items"] if item["bucket"] == bucket]
        if not bucket_items:
            continue
        lines.extend(["", f"## {bucket}", "", f"_{BUCKET_RECOMMENDATIONS[bucket]}_", ""])
        for item in bucket_items:
            routes = ", ".join(f"{name}={status}" for name, status in sorted(item["routes"].items()))
            verdicts = ", ".join(item["closing_verdicts"]) or "none"
            lines.append(f"### {item['setting_id']}")
            lines.append("")
            lines.append(f"- label: {item['label']}")
            lines.append(f"- status: `{item['status']}`")
            lines.append(f"- routes: `{routes}`")
            lines.append(f"- closing_verdicts: `{verdicts}`")
            lines.append(f"- next_action: {item['next_action']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, write the file without echoing")
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    report = build_report()
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else to_markdown(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True)
            if out.suffix.lower() == ".json"
            else to_markdown(report),
            encoding="utf-8",
        )
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
