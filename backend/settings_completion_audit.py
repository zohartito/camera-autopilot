"""Requirement-level completion audit for A7 II full settings control.

The acceptance report answers "what should we run next?" This module answers the stricter
question: "which explicit plan requirements are proven complete by the current worktree and
artifacts, and which still need live/package evidence?"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import camera_config
import settings_acceptance
import settings_closure
import settings_photo_inventory
import settings_probe_plan
import settings_promotions
import settings_readiness
import settings_registry
import settings_remaining_gates
import settings_session_ledger
import settings_sessions

ACTIVE_ROUTE_ORDER = tuple(
    route for route in settings_closure.ROUTE_ORDER if route not in settings_registry.DISABLED_ROUTES
)

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_ROW_FIELDS = (
    "setting_id",
    "label",
    "menu_group",
    "menu_page",
    "photo_sources",
    "current_photo_value",
    "greyed_in_photo",
    "preconditions",
    "status",
    "routes",
    "proof_artifacts",
    "next_action",
)

EXPECTED_API_ROUTE_SNIPPETS = (
    '"/api/camera/settings"',
    '"/api/camera/settings/{setting_id}"',
    '"/api/camera/setting-probes"',
    '"/api/camera/setting-preconditions"',
    '"/api/camera/setting-evidence"',
    '"/api/camera/settings-ownership"',
    '"/api/camera/setting-promotions"',
    '"/api/camera/settings-sessions"',
    '"/api/camera/settings-readiness"',
    '"/api/camera/settings-sessions/ledger"',
    '"/api/camera/settings-sessions/run"',
    '"/api/camera/settings-live-marathon/run"',
    '"/api/camera/settings-closure"',
    '"/api/camera/settings-gates"',
    '"/api/camera/settings-acceptance"',
    '"/api/camera/settings-completion-audit"',
    '"/api/camera/settings-photo-inventory"',
    '"/api/camera/settings-live-checklist"',
    '"/api/camera/setup-checklist"',
)

EXPECTED_PROOF_TOOLS = (
    "tools/a7ii_settings_probe_plan.py",
    "tools/a7ii_live_evidence_marathon.py",
    "tools/falsify_smart_remote_api_surface.py",
    "tools/a7ii_smart_remote_set_observe.py",
    "tools/a7ii_concurrency_matrix.py",
    "tools/a7ii_usb_state_matrix.py",
    "tools/a7ii_usb_set_read_revert.py",
    "tools/falsify_usb_bridge_lan.sh",
    "tools/a7ii_raw_ptp_visual_proof.py",
    "tools/find_camera_remote_command_package.py",
    "tools/review_camera_remote_command_package.py",
    "tools/a7ii_physical_gate_proof.py",
)

EXPECTED_VERIFIER_FILES = (
    "scripts/run_a7ii_settings_tests.sh",
    "backend/test_settings_registry.py",
    "backend/test_settings_promotions.py",
    "backend/test_settings_acceptance.py",
    "backend/test_settings_photo_inventory.py",
    "backend/test_settings_remaining_gates.py",
    "backend/test_settings_session_runs.py",
    "backend/test_settings_session_ledger.py",
    "backend/test_settings_live_marathon.py",
    "tests/test_a7ii_settings_api.py",
)

ROUTE_REQUIREMENT_IDS = {
    "smart_remote_wifi": "smart_remote_wifi_behavior_evidence",
    "usb_gphoto": "usb_gphoto_live_set_read_revert",
    "smart_remote_phone": "smart_remote_phone_preference_evidence",
    "raw_ptp": "raw_ptp_visible_focus_behavior",
    "camera_remote_command": "camera_remote_command_package_review",
    "openmemories": "openmemories_reversible_route_proof",
    "guided_physical": "guided_physical_admin_observations",
}


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _existing(paths: tuple[str, ...]) -> tuple[list[str], list[str]]:
    present = []
    missing = []
    for path in paths:
        target = REPO_ROOT / path
        if target.exists():
            present.append(path)
        else:
            missing.append(path)
    return present, missing


def _dedupe_commands(commands: list[str | None]) -> list[str]:
    out = []
    seen = set()
    for command in commands:
        if not command or command in seen:
            continue
        seen.add(command)
        out.append(command)
    return out


def _concrete_next_commands(commands: list[str | None]) -> list[str]:
    """Keep audit next commands actionable, not broad route templates."""
    route_template_tokens = (
        "<setting_id>",
        "<Smart Remote setter method>",
        "<value sent>",
    )
    return [
        command
        for command in _dedupe_commands(commands)
        if not any(token in command for token in route_template_tokens)
    ]


def _item(
    requirement: str,
    *,
    title: str,
    status: str,
    evidence: str,
    artifacts: list[str] | None = None,
    exact_next_commands: list[str] | None = None,
    missing: list[str] | None = None,
    route: str | None = None,
    adjudication_status: str | None = None,
    behavior_control_status: str | None = None,
) -> dict[str, Any]:
    return {
        "requirement": requirement,
        "title": title,
        "status": status,
        "route": route,
        "evidence": evidence,
        "artifacts": artifacts or [],
        "exact_next_commands": exact_next_commands or [],
        "missing": missing or [],
        "adjudication_status": adjudication_status,
        "behavior_control_status": behavior_control_status,
    }


def _missing_row_fields(registry: dict[str, Any]) -> list[dict[str, Any]]:
    missing = []
    for row in registry["settings"]:
        row_missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
        if row_missing:
            missing.append({"setting_id": row.get("setting_id", "<missing>"), "missing_fields": row_missing})
    return missing


def _api_missing_routes() -> list[str]:
    app_path = REPO_ROOT / "backend" / "app.py"
    try:
        app_text = app_path.read_text(encoding="utf-8")
    except OSError:
        return list(EXPECTED_API_ROUTE_SNIPPETS)
    return [snippet for snippet in EXPECTED_API_ROUTE_SNIPPETS if snippet not in app_text]


def _route_sections_by_route(closure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {section["route"]: section for section in closure["routes"]}


def _queue_by_route(acceptance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in acceptance["next_action_queue"]}


def _route_requirement(
    route: str,
    *,
    closure_section: dict[str, Any],
    queue_item: dict[str, Any] | None,
) -> dict[str, Any]:
    if route == "openmemories":
        return _item(
            ROUTE_REQUIREMENT_IDS[route],
            title=f"{closure_section['label']} route review and behavior evidence",
            status="unavailable",
            route=route,
            evidence="Route 3 is permanently disabled; historical private evidence is unavailable and cannot establish control.",
            adjudication_status="permanently_disabled",
            behavior_control_status="unverified",
        )
    source = closure_section.get("source_status") or {}
    source_status = source.get("status")
    gap_count = closure_section["gap_setting_count"]
    reviewed = closure_section["reviewed_settings"]
    missing_review = closure_section["missing_review_settings"]
    closed_without_proof = closure_section.get("closed_without_proof_count", 0)
    commands = closure_section.get("commands") or []
    exact = []
    if queue_item:
        sequence_command = f"python3 tools/a7ii_live_session_runner.py --route {route} --recommended-sequence"
        exact = _concrete_next_commands(
            [
                sequence_command,
                *(queue_item.get("readiness_next_commands") or []),
                queue_item.get("readiness_next_action"),
                queue_item.get("next_concrete_command"),
                queue_item.get("first_one_row_direct_command"),
                queue_item.get("audited_session_command"),
                queue_item.get("next_command"),
                queue_item.get("first_direct_command"),
                queue_item.get("review_and_refresh_command"),
            ]
        )
    if not exact and commands:
        exact = [commands[0]["command"]]

    artifacts = []
    if source.get("artifact"):
        artifacts.append(source["artifact"])

    adjudication_status = "complete"
    behavior_control_status = "behavior_evidence_complete"
    status = "behavior_evidence_complete"
    if route == "camera_remote_command" and source_status != "loaded":
        status = "waiting_external_package"
        adjudication_status = "waiting_external_package"
        behavior_control_status = "not_reviewed"
    elif gap_count or source_status not in {None, "loaded"}:
        status = "needs_route_adjudication"
        adjudication_status = "incomplete"
        behavior_control_status = "unproven"
    elif closed_without_proof or missing_review:
        status = "adjudicated_with_unproven_settings"
        behavior_control_status = "partial"
    elif route == "raw_ptp" and closure_section.get("verdict_counts", {}).get(
        "behavior_rejected"
    ):
        status = "negative_result_recorded"
        behavior_control_status = "behavior_rejected"
    elif route == "smart_remote_phone":
        status = "observation_complete"
        behavior_control_status = "phone_preference_observed_not_camera_control"
    elif route == "guided_physical":
        status = "observation_complete"
        behavior_control_status = "physical_or_admin_observation"

    if status == "adjudicated_with_unproven_settings":
        exact = [
            "python3 tools/a7ii_unproven_closed_triage.py "
            "--out artifacts/falsification/a7ii_unproven_closed_triage.md --quiet"
        ]
    elif status == "waiting_external_package":
        exact = [
            command
            for command in exact
            if "find_camera_remote_command_package.py" in command
            or "review_camera_remote_command_package.py" in command
        ]
    elif status in {
        "behavior_evidence_complete",
        "negative_result_recorded",
        "observation_complete",
    }:
        exact = []

    label = closure_section["label"]
    evidence = (
        f"{label}: source={source_status or 'not_applicable'}, gaps={gap_count}, "
        f"closed_without_proof={closed_without_proof}, "
        f"reviewed={reviewed}, missing_review_settings={missing_review}, commands={len(commands)}"
    )
    return _item(
        ROUTE_REQUIREMENT_IDS[route],
        title=f"{label} route review and behavior evidence",
        status=status,
        route=route,
        evidence=evidence,
        artifacts=artifacts,
        exact_next_commands=exact,
        missing=(
            closure_section.get("closed_without_proof_setting_ids", [])
            if status == "adjudicated_with_unproven_settings"
            else [route]
            if status in {"needs_route_adjudication", "waiting_external_package"}
            else []
        ),
        adjudication_status=adjudication_status,
        behavior_control_status=behavior_control_status,
    )


def _guardrail_item(registry: dict[str, Any]) -> dict[str, Any]:
    rows_by_id = {row["setting_id"]: row for row in registry["settings"]}
    missing = [
        setting_id
        for setting_id in sorted(settings_registry.DESTRUCTIVE_SETTING_IDS)
        if setting_id not in rows_by_id
    ]
    unguarded = [
        setting_id
        for setting_id in sorted(settings_registry.DESTRUCTIVE_SETTING_IDS)
        if setting_id in rows_by_id
        and rows_by_id[setting_id]["status"] != "dangerous_admin"
        and "guided_physical" not in rows_by_id[setting_id].get("routes", {})
    ]
    problems = missing + unguarded
    return _item(
        "destructive_admin_guardrails",
        title="Destructive/admin settings are guarded",
        status="complete" if not problems else "incomplete",
        evidence=(
            f"{len(settings_registry.DESTRUCTIVE_SETTING_IDS) - len(missing)} destructive/admin ids "
            f"present; {len(problems)} missing or unguarded"
        ),
        exact_next_commands=["python3 backend/test_settings_registry.py"],
        missing=problems,
    )


def _acceptance_ledger_context_item(
    acceptance: dict[str, Any],
    session_ledger: dict[str, Any],
) -> dict[str, Any]:
    queue_routes = {item["route"]: item for item in acceptance.get("next_action_queue", [])}
    session_routes = {item["route"]: item for item in acceptance.get("next_live_sessions", [])}
    expected_routes = set(session_routes)
    actionable_routes = {
        item["route"]
        for item in acceptance.get("next_live_sessions", [])
        if item.get("next_evidence_session_command")
    }
    queue_missing = sorted(
        route
        for route in actionable_routes
        if route not in queue_routes or not queue_routes[route].get("ledger_next_command")
    )
    sessions_missing = sorted(
        route
        for route in expected_routes
        if route not in session_routes or not session_routes[route].get("ledger_next_command")
    )
    summary = acceptance.get("summary", {})
    unreadable = summary.get("session_ledger_unreadable_manifests")
    if unreadable is None:
        unreadable = session_ledger["summary"]["unreadable_manifests"]
    missing = queue_missing + [f"next_live_session:{route}" for route in sessions_missing]
    if unreadable:
        missing.append("unreadable_session_manifests")
    status = "complete" if not missing else "incomplete"
    return _item(
        "acceptance_ledger_context",
        title="Acceptance queue includes latest live-session ledger context",
        status=status,
        evidence=(
            f"{summary.get('session_ledger_sessions', session_ledger['summary']['sessions'])} session "
            f"manifest(s) reflected; {summary.get('routes_with_ledger_next_command', 0)}/"
            f"{len(expected_routes)} active route ledgers with next commands; "
            f"{len(actionable_routes) - len(queue_missing)}/"
            f"{len(actionable_routes)} actionable queue routes with ledger next commands; "
            f"{len(expected_routes) - len(sessions_missing)}/"
            f"{len(expected_routes)} active live-session sections with ledger next commands; "
            f"{unreadable} unreadable"
        ),
        artifacts=[
            "backend/settings_acceptance.py",
            "backend/settings_session_ledger.py",
            "tools/a7ii_acceptance_report.py",
            "artifacts/falsification/a7ii_acceptance.md",
        ],
        exact_next_commands=[
            "python3 tools/a7ii_acceptance_report.py --out artifacts/falsification/a7ii_acceptance.md --quiet"
        ],
        missing=missing,
    )


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    package_path: str | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    closure = settings_closure.build_report(registry=registry, sample_limit=3)
    acceptance = settings_acceptance.build_report(registry=registry, package_path=package_path)
    gates = settings_remaining_gates.build_report(registry=registry)
    promotions = settings_promotions.build_promotion_plan(registry=registry)
    readiness = settings_readiness.build_readiness_report(registry=registry, package_path=package_path)
    session_playbook = settings_sessions.build_session_playbook(registry=registry)
    session_ledger = settings_session_ledger.build_ledger()
    probe_report = settings_probe_plan.build_probe_report(registry)
    photo_inventory = settings_photo_inventory.build_report(registry=registry)
    photo_inventory_is_privacy_tombstoned = (
        photo_inventory["summary"]["status"] == "source_dir_missing"
        and photo_inventory["summary"]["non_registry_sources"] == 0
    )

    missing_fields = _missing_row_fields(registry)
    api_missing = _api_missing_routes()
    proof_present, proof_missing = _existing(EXPECTED_PROOF_TOOLS)
    verifier_present, verifier_missing = _existing(EXPECTED_VERIFIER_FILES)
    checklist = camera_config.one_time_checklist()
    route_sections = _route_sections_by_route(closure)
    queue = _queue_by_route(acceptance)

    items = [
        _item(
            "canonical_settings_registry",
            title="Canonical photographed-settings registry",
            status="complete" if len(registry["settings"]) >= 150 else "incomplete",
            evidence=f"{len(registry['settings'])} settings loaded from data/a7ii_settings_registry.yaml",
            artifacts=["data/a7ii_settings_registry.yaml"],
            exact_next_commands=["python3 backend/test_settings_registry.py"],
        ),
        _item(
            "registry_required_fields",
            title="Every registry row has the required control fields",
            status="complete" if not missing_fields else "incomplete",
            evidence=f"{len(missing_fields)} registry rows missing required fields",
            artifacts=["data/a7ii_settings_registry.yaml"],
            exact_next_commands=["python3 backend/test_settings_registry.py"],
            missing=[item["setting_id"] for item in missing_fields],
        ),
        _item(
            "photo_source_inventory",
            title="Every source photo is registry-owned or explicit route context",
            status=(
                "complete"
                if photo_inventory["summary"]["coverage_complete"] or photo_inventory_is_privacy_tombstoned
                else "incomplete"
            ),
            evidence=(
                (
                    "private source inventory intentionally unconfigured; "
                    if photo_inventory_is_privacy_tombstoned
                    else f"{photo_inventory['summary']['source_files']} source files; "
                )
                + f"{photo_inventory['summary']['registry_photo_sources']} registry photo source(s); "
                + f"{photo_inventory['summary']['non_registry_sources']} non-registry context source(s); "
                + f"{len(photo_inventory['summary']['unaccounted_source_files'])} unaccounted"
            ),
            artifacts=[
                "data/a7ii_photo_inventory.json",
                "backend/settings_photo_inventory.py",
                "tools/a7ii_photo_inventory_report.py",
            ],
            exact_next_commands=[
                "python3 backend/test_settings_photo_inventory.py",
                "python3 tools/a7ii_photo_inventory_report.py --out artifacts/falsification/a7ii_photo_inventory.md",
            ],
            missing=(
                []
                if photo_inventory_is_privacy_tombstoned
                else (
                    photo_inventory["summary"]["unaccounted_source_files"]
                    + photo_inventory["summary"]["missing_registry_photo_files"]
                    + photo_inventory["summary"]["missing_non_registry_files"]
                    + photo_inventory["summary"]["inventory_overlaps_registry"]
                    + photo_inventory["summary"]["invalid_setting_links"]
                )
            ),
        ),
        _item(
            "validation_test_harness",
            title="Validation tests and verifier script exist",
            status="complete" if not verifier_missing else "incomplete",
            evidence=f"{len(verifier_present)} verifier/test files present; {len(verifier_missing)} missing",
            artifacts=verifier_present,
            exact_next_commands=["scripts/run_a7ii_settings_tests.sh"],
            missing=verifier_missing,
        ),
        _item(
            "unified_settings_api",
            title="Unified settings API surface",
            status="complete" if not api_missing else "incomplete",
            evidence=f"{len(EXPECTED_API_ROUTE_SNIPPETS) - len(api_missing)}/{len(EXPECTED_API_ROUTE_SNIPPETS)} expected endpoint snippets present",
            artifacts=["backend/app.py", "tests/test_a7ii_settings_api.py"],
            exact_next_commands=[".venv/bin/python tests/test_a7ii_settings_api.py"],
            missing=api_missing,
        ),
        _guardrail_item(registry),
        _item(
            "registry_backed_setup_guidance",
            title="Setup guidance is registry-backed",
            status="complete" if checklist else "incomplete",
            evidence=f"{len(checklist)} setup guidance items returned by camera_config.one_time_checklist()",
            artifacts=["backend/camera_config.py", "backend/settings_registry.py"],
            exact_next_commands=["python3 backend/test_settings_registry.py"],
        ),
        _item(
            "route_probe_hooks_and_playbooks",
            title="Runnable route proof hooks and session playbooks",
            status="complete" if not proof_missing and not probe_report["summary"]["missing_gap_rows"] else "incomplete",
            evidence=(
                f"{len(proof_present)} proof tools present; {probe_report['summary']['probe_commands']} "
                f"proof commands cover {probe_report['summary']['covered_gap_rows']}/"
                f"{probe_report['summary']['selected_gap_rows']} gap rows"
            ),
            artifacts=proof_present,
            exact_next_commands=[
                "python3 tools/a7ii_settings_probe_plan.py",
                "python3 tools/a7ii_live_evidence_marathon.py",
            ],
            missing=proof_missing + probe_report["summary"]["missing_gap_rows"],
        ),
        _item(
            "remaining_gate_report",
            title="Remaining physical/state/admin gates have exact commands",
            status="complete" if not gates["summary"]["missing_probe_command_rows"] else "incomplete",
            evidence=(
                f"{gates['summary']['remaining_gate_rows']} gate rows; "
                f"{gates['summary']['probe_commands']} proof commands; "
                f"{len(gates['summary']['missing_probe_command_rows'])} missing command rows"
            ),
            artifacts=[
                "backend/settings_remaining_gates.py",
                "tools/a7ii_remaining_gates_report.py",
                "artifacts/falsification/a7ii_remaining_gates.md",
            ],
            exact_next_commands=[
                "python3 tools/a7ii_remaining_gates_report.py --out artifacts/falsification/a7ii_remaining_gates.md"
            ],
            missing=gates["summary"]["missing_probe_command_rows"],
        ),
        _item(
            "registry_promotion_pipeline",
            title="Review artifacts can be promoted only after behavior proof",
            status="waiting_for_evidence" if promotions["summary"]["applicable_promotions"] == 0 else "complete",
            evidence=(
                f"{promotions['summary']['reviewed_candidates']} reviewed candidates; "
                f"{promotions['summary']['applicable_promotions']} currently applicable promotions"
            ),
            artifacts=["backend/settings_promotions.py", "tools/a7ii_registry_promotion_plan.py"],
            exact_next_commands=[
                "python3 tools/a7ii_registry_promotion_plan.py --out artifacts/falsification/a7ii_registry_promotion_plan.md"
            ],
        ),
        _item(
            "live_session_ledger",
            title="Latest live-session outcomes are route-owned and actionable",
            status=(
                "complete"
                if session_ledger["summary"]["unreadable_manifests"] == 0
                and session_ledger["summary"]["routes_with_next_command"] >= len(ACTIVE_ROUTE_ORDER)
                else "incomplete"
            ),
            evidence=(
                f"{session_ledger['summary']['sessions']} session manifest(s); "
                f"{session_ledger['summary']['unreadable_manifests']} unreadable; "
                f"{session_ledger['summary']['routes_with_next_command']}/"
                f"{len(ACTIVE_ROUTE_ORDER)} active routes with next commands"
            ),
            artifacts=[
                "backend/settings_session_ledger.py",
                "tools/a7ii_live_session_ledger.py",
                "artifacts/falsification/a7ii_live_session_ledger.md",
            ],
            exact_next_commands=[
                "python3 tools/a7ii_live_session_ledger.py --out artifacts/falsification/a7ii_live_session_ledger.md"
            ],
            missing=session_ledger["summary"]["routes_without_sessions"]
            if session_ledger["summary"]["routes_with_next_command"] < len(ACTIVE_ROUTE_ORDER)
            else [],
        ),
        _acceptance_ledger_context_item(acceptance, session_ledger),
    ]

    for route in settings_closure.ROUTE_ORDER:
        items.append(
            _route_requirement(
                route,
                closure_section=route_sections[route],
                queue_item=queue.get(route),
            )
        )

    status_counts: dict[str, int] = {}
    for item in items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    incomplete_items = [item["requirement"] for item in items if item["status"] == "incomplete"]
    live_items = [
        item["requirement"]
        for item in items
        if item["status"]
        in {"needs_route_adjudication", "waiting_external_package", "waiting_for_evidence"}
    ]
    unproven_control_items = [
        item["requirement"]
        for item in items
        if item["status"]
        in {"adjudicated_with_unproven_settings", "negative_result_recorded"}
    ]
    full_behavior_control_complete = closure["summary"]["full_behavior_control_complete"]
    goal_complete = (
        not incomplete_items
        and not live_items
        and not unproven_control_items
        and full_behavior_control_complete
    )
    completion_state = "complete" if goal_complete else "needs_behavior_evidence"
    if incomplete_items:
        completion_state = "implementation_incomplete"
    registry_counts = closure["summary"]["registry_status_counts"]
    acceptance_summary = dict(acceptance["summary"])
    acceptance_summary["legacy_queue_completion_state"] = acceptance_summary.get(
        "completion_state"
    )
    acceptance_summary["completion_state"] = completion_state

    return {
        "summary": {
            "completion_state": completion_state,
            "goal_complete": goal_complete,
            "total_settings": len(registry["settings"]),
            "inventory_complete": closure["summary"]["inventory_complete"],
            "route_adjudication_complete": closure["summary"][
                "route_adjudication_complete"
            ],
            "full_behavior_control_complete": full_behavior_control_complete,
            "registry_status_counts": registry_counts,
            "proven_set_settings": registry_counts["proven_set"],
            "proven_read_settings": registry_counts["proven_read"],
            "needs_probe_settings": registry_counts["needs_probe"],
            "closed_without_behavior_proof_route_rows": closure["summary"][
                "closed_without_proof_rows"
            ],
            "unique_unproven_closed_settings": closure["summary"][
                "unproven_closed_setting_count"
            ],
            "audit_items": len(items),
            "status_counts": dict(sorted(status_counts.items())),
            "incomplete_items": incomplete_items,
            "live_or_external_items": live_items,
            "unproven_control_items": unproven_control_items,
            "missing_review_sources": closure["summary"]["missing_review_sources"],
            "ready_to_attempt_routes": readiness["summary"]["ready_to_attempt"],
            "missing_readiness_requirements": readiness["summary"]["missing_requirements"],
            "ordered_session_commands": session_playbook["summary"]["ordered_commands"],
        },
        "items": items,
        "acceptance_summary": acceptance_summary,
        "closure_summary": closure["summary"],
        "gate_summary": gates["summary"],
        "readiness_summary": readiness["summary"],
    }


def to_markdown(report: dict[str, Any], *, limit_commands: int = 3) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Full Settings Control Completion Audit",
        "",
        "This report separates inventory and route-review completion from behavior-proven "
        "setting control. `complete` never means all 162 settings are remotely controlled.",
        "",
        f"- completion_state: `{summary['completion_state']}`",
        f"- goal_complete: `{summary['goal_complete']}`",
        f"- total_settings: `{summary['total_settings']}`",
        f"- inventory_complete: `{summary['inventory_complete']}`",
        f"- route_adjudication_complete: `{summary['route_adjudication_complete']}`",
        f"- full_behavior_control_complete: `{summary['full_behavior_control_complete']}`",
        f"- proven_set settings: `{summary['proven_set_settings']}`",
        f"- proven_read settings: `{summary['proven_read_settings']}`",
        f"- needs_probe settings: `{summary['needs_probe_settings']}`",
        (
            "- closed_without_behavior_proof route rows: "
            f"`{summary['closed_without_behavior_proof_route_rows']}`"
        ),
        (
            "- unique_unproven_closed settings: "
            f"`{summary['unique_unproven_closed_settings']}`"
        ),
        f"- audit_items: `{summary['audit_items']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        f"- ready_to_attempt_routes: `{summary['ready_to_attempt_routes']}`",
        f"- missing_readiness_requirements: `{summary['missing_readiness_requirements']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in summary["status_counts"].items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "## Requirements", ""])
    for item in report["items"]:
        lines.append(f"### {item['requirement']}")
        lines.append("")
        lines.append(f"- title: {item['title']}")
        lines.append(f"- status: `{item['status']}`")
        if item.get("route"):
            lines.append(f"- route: `{item['route']}`")
        if item.get("adjudication_status"):
            lines.append(f"- adjudication_status: `{item['adjudication_status']}`")
        if item.get("behavior_control_status"):
            lines.append(f"- behavior_control_status: `{item['behavior_control_status']}`")
        lines.append(f"- evidence: {item['evidence']}")
        if item["artifacts"]:
            lines.append(f"- artifacts: `{', '.join(item['artifacts'])}`")
        if item["missing"]:
            lines.append(f"- missing: `{', '.join(item['missing'][:20])}`")
        commands = item["exact_next_commands"]
        if commands:
            lines.extend(["", "Next command(s):", ""])
            visible = commands if limit_commands <= 0 else commands[:limit_commands]
            for command in visible:
                lines.extend(["```bash", command, "```"])
            hidden = len(commands) - len(visible)
            if hidden > 0:
                lines.append(f"_Plus {hidden} more command(s); rerun with --limit-commands 0 for all._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
