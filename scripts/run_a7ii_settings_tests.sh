#!/usr/bin/env bash
# Verify the A7 II full-settings-control stack in this checkout.
set -uo pipefail

cd "$(dirname "$0")/.."

if [[ -n "${PLAIN_PYTHON:-}" ]]; then
  plain="$PLAIN_PYTHON"
elif [[ -x ".venv/bin/python" ]]; then
  plain=".venv/bin/python"
else
  plain="python3"
fi
venv_python="${A7II_API_PYTHON:-$plain}"
pycache_prefix="${PYTHONPYCACHEPREFIX:-/tmp/a7ii-python-cache}"
generated_tmp="$(mktemp -d "${TMPDIR:-/tmp}/a7ii-settings-tests.XXXXXX")"
generated_reports="$generated_tmp/reports"
generated_sessions="$generated_tmp/live_sessions"
generated_marathons="$generated_tmp/live_marathons"
fail=0

cleanup_generated_tmp() {
  rm -rf "$generated_tmp"
}
trap cleanup_generated_tmp EXIT

run_step() {
  local label="$1"
  shift
  echo "== $label =="
  if "$@"; then
    echo "  ok   $label"
  else
    echo "  FAIL $label"
    fail=1
  fi
}

run_step "registry/control/precondition unit tests" "$plain" backend/test_settings_registry.py
run_step "photo-source inventory tests" "$plain" backend/test_settings_photo_inventory.py
run_step "settings evidence aggregation tests" "$plain" backend/test_settings_evidence.py
run_step "settings ownership audit tests" "$plain" backend/test_settings_ownership.py
run_step "settings promotion planning tests" "$plain" backend/test_settings_promotions.py
run_step "registry promotion apply CLI guard tests" "$plain" backend/test_registry_promotion_apply_cli.py
run_step "settings evidence closure report tests" "$plain" backend/test_evidence_closure_report.py
run_step "settings acceptance report tests" "$plain" backend/test_settings_acceptance.py
run_step "settings completion audit tests" "$plain" backend/test_settings_completion_audit.py
run_step "capability matrix contract tests" "$plain" backend/test_capability_matrix_contract.py
run_step "settings live evidence checklist tests" "$plain" backend/test_settings_live_checklist.py
run_step "settings live-readiness tests" "$plain" backend/test_settings_readiness.py
run_step "settings remaining-gates tests" "$plain" backend/test_settings_remaining_gates.py
run_step "settings live-session playbook tests" "$plain" backend/test_settings_sessions.py
run_step "settings live-session runner tests" "$plain" backend/test_settings_session_runs.py
run_step "settings live-session ledger tests" "$plain" backend/test_settings_session_ledger.py
run_step "settings live-evidence marathon tests" "$plain" backend/test_settings_live_marathon.py
run_step "physical gate proof CLI tests" "$plain" backend/test_physical_gate_proof_cli.py
run_step "Smart Remote client unit tests" "$plain" backend/test_wifi_a7ii.py
run_step "Smart Remote API-surface review tests" "$plain" backend/test_smart_remote_api_review.py
run_step "Smart Remote behavior proof tests" "$plain" backend/test_smart_remote_behavior_proof.py
run_step "Smart Remote set-observe proof tests" "$plain" backend/test_smart_remote_set_observe.py
run_step "Smart Remote phone preference proof tests" "$plain" backend/test_smart_remote_phone_preferences.py
run_step "USB plus Wi-Fi concurrency proof tests" "$plain" backend/test_a7ii_concurrency_matrix.py
run_step "OpenMemories proof-app scaffold tests" "$plain" backend/test_openmemories_route3.py
run_step "OpenMemories endpoint proof tests" "$plain" backend/test_openmemories_endpoint_proof.py
run_step "OpenMemories guarded write/revert tests" "$plain" backend/test_openmemories_param_set_read_revert.py
run_step "OpenMemories rollback port proof tests" "$plain" backend/test_openmemories_rollback_ports.py
run_step "OpenMemories artifact review tests" "$plain" backend/test_openmemories_review.py
run_step "USB bridge settings-route tests" "$plain" backend/test_usb_ptp_bridge_server.py
run_step "USB bridge security policy tests" "$plain" backend/test_usb_ptp_bridge_security.py
run_step "USB state-matrix parser tests" "$plain" backend/test_usb_state_matrix.py
run_step "USB set/read/revert planning tests" "$plain" backend/test_usb_set_read_revert.py
run_step "USB/gphoto artifact review tests" "$plain" backend/test_usb_gphoto_review.py
run_step "raw PTP artifact review tests" "$plain" backend/test_raw_ptp_review.py
run_step "raw PTP visual proof tests" "$plain" backend/test_raw_ptp_visual_proof.py
run_step "raw PTP probe CLI tests" "$plain" backend/test_raw_ptp_probe_cli.py
run_step "P3 privacy and tombstone tests" "$plain" backend/test_p3_security_tombstones.py
run_step "Camera Remote Command package-locator tests" "$plain" backend/test_camera_remote_command_package_locator.py
run_step "Camera Remote Command package-review tests" "$plain" backend/test_camera_remote_command_review.py
run_step "unresolved settings closure tests" "$plain" backend/test_unresolved_settings_closure.py

if [[ -x "$venv_python" ]]; then
  run_step "settings API contract tests" "$venv_python" tests/test_a7ii_settings_api.py
else
  echo "== settings API contract tests =="
  echo "  SKIP missing executable $venv_python (FastAPI lives in the project venv)"
fi

run_step "probe-plan dry run" "$plain" tools/a7ii_settings_probe_plan.py --limit 3
run_step "greyed precondition dry run" "$plain" tools/a7ii_precondition_solver.py --greyed-only --limit 3
run_step "USB set/read/revert dry run" "$plain" tools/a7ii_usb_set_read_revert.py --all-route-gaps --include-proven --max-settings 3 --json
run_step "USB bridge LAN script syntax" bash -n tools/falsify_usb_bridge_lan.sh
run_step "USB bridge secure-smoke harness tests" bash tests/test_usb_bridge_smoke_security.sh
run_step "secure launcher tests" bash tests/test_secure_launcher.sh
run_step "USB plus Wi-Fi concurrency dry run" "$plain" tools/a7ii_concurrency_matrix.py --json
run_step "remaining gates report generation" "$plain" tools/a7ii_remaining_gates_report.py --out "$generated_reports/a7ii_remaining_gates.md" --quiet
run_step "remaining gates JSON generation" "$plain" tools/a7ii_remaining_gates_report.py --json --out "$generated_reports/a7ii_remaining_gates.json" --quiet
run_step "evidence closure report generation" "$plain" tools/a7ii_evidence_closure_report.py --limit-settings 2 --limit-commands 1 --out "$generated_reports/a7ii_evidence_closure.md"
run_step "acceptance report generation" "$plain" tools/a7ii_acceptance_report.py --out "$generated_reports/a7ii_acceptance.md" --quiet
run_step "acceptance JSON generation" "$plain" tools/a7ii_acceptance_report.py --json --out "$generated_reports/a7ii_acceptance.json" --quiet
run_step "completion audit generation" "$plain" tools/a7ii_completion_audit.py --out "$generated_reports/a7ii_completion_audit.md" --quiet
run_step "completion audit JSON generation" "$plain" tools/a7ii_completion_audit.py --json --out "$generated_reports/a7ii_completion_audit.json" --quiet
run_step "live evidence checklist generation" "$plain" tools/a7ii_live_evidence_checklist.py --session-out-dir "$generated_sessions" --out "$generated_reports/a7ii_live_evidence_checklist.md" --quiet
run_step "live evidence checklist JSON generation" "$plain" tools/a7ii_live_evidence_checklist.py --session-out-dir "$generated_sessions" --json --out "$generated_reports/a7ii_live_evidence_checklist.json" --quiet
run_step "ownership report generation" "$plain" tools/a7ii_settings_ownership_report.py --out "$generated_reports/a7ii_settings_ownership.md" --limit-commands 1 --quiet
run_step "ownership JSON generation" "$plain" tools/a7ii_settings_ownership_report.py --json --out "$generated_reports/a7ii_settings_ownership.json" --limit-commands 1 --quiet
run_step "photo inventory report generation" "$plain" tools/a7ii_photo_inventory_report.py --out "$generated_reports/a7ii_photo_inventory.md" --quiet
run_step "photo inventory JSON generation" "$plain" tools/a7ii_photo_inventory_report.py --json --out "$generated_reports/a7ii_photo_inventory.json" --quiet
run_step "live readiness generation" "$plain" tools/a7ii_live_readiness.py --route usb_gphoto --out "$generated_reports/a7ii_live_readiness.md"
run_step "live readiness JSON generation" "$plain" tools/a7ii_live_readiness.py --route usb_gphoto --json --out "$generated_reports/a7ii_live_readiness.json"
run_step "registry promotion plan generation" "$plain" tools/a7ii_registry_promotion_plan.py --limit 20 --out "$generated_reports/a7ii_registry_promotion_plan.md"
run_step "registry promotion plan JSON generation" "$plain" tools/a7ii_registry_promotion_plan.py --limit 20 --json --out "$generated_reports/a7ii_registry_promotion_plan.json"
run_step "registry promotion apply dry run" "$plain" tools/a7ii_apply_registry_promotions.py --route usb_gphoto --json
run_step "live route playbook generation" "$plain" tools/a7ii_live_route_playbook.py --route usb_gphoto --limit-commands 2 --out "$generated_reports/a7ii_live_route_playbook.md"
run_step "live route playbook JSON generation" "$plain" tools/a7ii_live_route_playbook.py --route usb_gphoto --limit-commands 2 --json --out "$generated_reports/a7ii_live_route_playbook.json"
run_step "live session runner dry manifest" "$plain" tools/a7ii_live_session_runner.py --route usb_gphoto --phase review --phase refresh --out-dir "$generated_sessions"
run_step "live session ledger generation" "$plain" tools/a7ii_live_session_ledger.py --out-dir "$generated_sessions" --out "$generated_reports/a7ii_live_session_ledger.md" --quiet
run_step "live evidence marathon dry run" "$plain" tools/a7ii_live_evidence_marathon.py --route usb_gphoto --out-dir "$generated_marathons" --session-out-dir "$generated_sessions" --json
run_step "settings stack compile" env PYTHONPYCACHEPREFIX="$pycache_prefix" "$plain" -m py_compile \
  backend/settings_registry.py \
  backend/settings_smart_remote_values.py \
  backend/settings_acceptance.py \
  backend/settings_closure.py \
  backend/settings_completion_audit.py \
  backend/capability_matrix_contract.py \
  backend/settings_control.py \
  backend/settings_evidence.py \
  backend/settings_live_checklist.py \
  backend/settings_ownership.py \
  backend/settings_photo_inventory.py \
  backend/settings_promotions.py \
  backend/settings_sessions.py \
  backend/settings_session_runs.py \
  backend/settings_session_ledger.py \
  backend/settings_live_marathon.py \
  backend/settings_probe_plan.py \
  backend/settings_preconditions.py \
  backend/settings_readiness.py \
  backend/settings_remaining_gates.py \
  backend/camera_config.py \
  backend/wifi_a7ii.py \
  backend/app.py \
  tools/a7ii_acceptance_report.py \
  tools/a7ii_completion_audit.py \
  tools/a7ii_evidence_closure_report.py \
  tools/a7ii_live_evidence_checklist.py \
  tools/a7ii_settings_ownership_report.py \
  tools/a7ii_photo_inventory_report.py \
  tools/a7ii_live_route_playbook.py \
  tools/a7ii_live_readiness.py \
  tools/a7ii_live_session_runner.py \
  tools/a7ii_live_session_ledger.py \
  tools/a7ii_live_evidence_marathon.py \
  tools/a7ii_apply_registry_promotions.py \
  tools/a7ii_registry_promotion_plan.py \
  tools/a7ii_settings_gap_report.py \
  tools/a7ii_settings_probe_plan.py \
  tools/a7ii_usb_state_matrix.py \
  tools/a7ii_usb_set_read_revert.py \
  tools/review_usb_gphoto_artifacts.py \
  tools/review_raw_ptp_artifacts.py \
  tools/a7ii_raw_ptp_visual_proof.py \
  tools/a7ii_physical_gate_proof.py \
  tools/a7ii_remaining_gates_report.py \
  tools/a7ii_precondition_solver.py \
  tools/falsify_smart_remote_api_surface.py \
  tools/a7ii_concurrency_matrix.py \
  tools/review_smart_remote_api_surface.py \
  tools/a7ii_smart_remote_behavior_proof.py \
  tools/a7ii_smart_remote_set_observe.py \
  tools/a7ii_phone_preference_proof.py \
  tools/review_smart_remote_phone_preferences.py \
  tools/find_camera_remote_command_package.py \
  tools/review_camera_remote_command_package.py \
  tools/falsify_openmemories_route3.py \
  tools/prove_openmemories_endpoint_surface.py \
  tools/prove_openmemories_param_set_read_revert.py \
  tools/prove_openmemories_rollback_ports.py \
  tools/review_openmemories_artifacts.py \
  tools/usb_ptp_bridge_server.py \
  tests/probe_raw_control.py \
  backend/test_wifi_a7ii.py \
  backend/test_smart_remote_behavior_proof.py \
  backend/test_smart_remote_set_observe.py \
  backend/test_settings_evidence.py \
  backend/test_settings_ownership.py \
  backend/test_settings_photo_inventory.py \
  backend/test_settings_acceptance.py \
  backend/test_settings_completion_audit.py \
  backend/test_capability_matrix_contract.py \
  backend/test_settings_live_checklist.py \
  backend/test_settings_readiness.py \
  backend/test_settings_remaining_gates.py \
  backend/test_settings_promotions.py \
  backend/test_registry_promotion_apply_cli.py \
  backend/test_evidence_closure_report.py \
  backend/test_settings_sessions.py \
  backend/test_settings_session_runs.py \
  backend/test_settings_session_ledger.py \
  backend/test_settings_live_marathon.py \
  backend/test_physical_gate_proof_cli.py \
  backend/test_smart_remote_api_review.py \
  backend/test_smart_remote_phone_preferences.py \
  backend/test_a7ii_concurrency_matrix.py \
  backend/test_openmemories_route3.py \
  backend/test_openmemories_endpoint_proof.py \
  backend/test_openmemories_param_set_read_revert.py \
  backend/test_openmemories_rollback_ports.py \
  backend/test_openmemories_review.py \
  backend/test_usb_ptp_bridge_server.py \
  backend/test_usb_ptp_bridge_security.py \
  backend/test_usb_state_matrix.py \
  backend/test_usb_set_read_revert.py \
  backend/test_usb_gphoto_review.py \
  backend/test_raw_ptp_review.py \
  backend/test_raw_ptp_visual_proof.py \
  backend/test_raw_ptp_probe_cli.py \
  backend/test_camera_remote_command_package_locator.py \
  backend/test_camera_remote_command_review.py \
  backend/test_unresolved_settings_closure.py \
  tests/test_a7ii_settings_api.py

if [[ "$fail" -eq 0 ]]; then
  echo "A7 II SETTINGS TESTS PASS"
  exit 0
fi

echo "A7 II SETTINGS TESTS FAILED"
exit 1
