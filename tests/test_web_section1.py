"""Static and camera-free behavioural checks for the Section 1 web features.

Covers the server-rendered mood previews, the critique/refine trail + toggle, the guided
panorama session mode, and the handheld body-shutter panel. Mirrors the style of
tests/test_web_background_workflows.py: node runs the extracted helpers against fixtures, and
the rest are static assertions over the shipped HTML/JS.
"""
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

WEB = Path(__file__).resolve().parents[1] / "backend/web"
HTML = (WEB / "index.html").read_text()
WORKFLOWS = (WEB / "workflows.js").read_text()


def run_js(code: str) -> None:
    subprocess.run(["node", "-e", "const assert=require('node:assert/strict');\n" + code],
                   check=True, capture_output=True, text=True)


# --- new controls / ids exist in the HTML ----------------------------------------------------
def test_html_declares_the_new_control_ids() -> None:
    for element_id in [
        "refine", "refine-note", "critique-trail",
        "panorama-options", "panorama-projection", "panorama-hfov", "panorama-rotation",
        "panorama-autocrop", "panorama-preview-every", "panorama-budget",
        "panorama-preview-wrap", "panorama-preview-img", "panorama-preview-status",
        "handheld-panel", "handheld-max-frames", "handheld-max-duration",
        "handheld-start", "handheld-cancel", "handheld-status", "handheld-counters",
        "handheld-restore", "handheld-frames",
    ]:
        assert f'id="{element_id}"' in HTML, f"missing element id {element_id}"


def test_session_mode_offers_panorama_and_refine_is_relabelled() -> None:
    assert '<option value="panorama">Panorama (guided sweep)</option>' in HTML
    assert 'id="refine-budget"' in HTML and "refined.max_physical_exposures" in HTML
    assert "Set <strong>Still Img. Save Dest.</strong>" in HTML  # handheld instruction
    assert "exposure locked to current settings" in HTML  # panorama budget line


# --- workflows.js references the exact route paths -------------------------------------------
def test_workflows_references_the_new_route_paths() -> None:
    assert "'/moods/preview'" in WORKFLOWS or "/moods/preview" in WORKFLOWS
    assert "/handheld" in WORKFLOWS
    assert "panorama-preview" in WORKFLOWS
    assert "refine" in WORKFLOWS


def test_stack_refine_exclusivity_function_exists() -> None:
    # The exclusivity decision lives in one named, testable helper.
    assert "stackRefineExclusivity(" in WORKFLOWS
    assert "stackRefineExclusivity(" in HTML  # and the UI actually calls it


# --- no token ever travels in a URL ----------------------------------------------------------
def test_no_token_is_placed_in_any_url_string() -> None:
    # Any quoted literal that looks like a request URL must not carry a token; the token rides
    # the authenticated header helper (apiFetch) only.
    for source in (WORKFLOWS, HTML):
        literals = re.findall(r"""['"`]([^'"`]*)['"`]""", source)
        for literal in literals:
            if "/" in literal and ("moods" in literal or "handheld" in literal
                                   or "sessions" in literal or "shoot" in literal
                                   or "panorama" in literal or literal.startswith("/")):
                assert "token" not in literal.lower(), f"token found in URL literal: {literal}"
        assert re.search(r"[?&][a-z_]*token=", source, re.I) is None


# --- panorama projection list equals backend PROJECTIONS keys --------------------------------
def test_panorama_projection_options_match_backend_projections() -> None:
    from panorama import PROJECTIONS
    select = re.search(r'<select id="panorama-projection".*?</select>', HTML, re.S).group()
    option_values = set(re.findall(r'<option value="([^"]+)"', select))
    assert option_values == set(PROJECTIONS.keys()), (option_values, set(PROJECTIONS.keys()))
    # cylindrical is the default selection.
    assert re.search(r'<option value="cylindrical"[^>]*selected', select)


# --- behavioural checks over the extracted helpers -------------------------------------------
def test_stack_refine_exclusivity_disables_refine_when_a_stack_is_selected() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      const off=window.AutopilotWorkflows.stackRefineExclusivity('off', true);
      assert.equal(off.refine, true);
      assert.equal(off.refineDisabled, false);
      const fixed=window.AutopilotWorkflows.stackRefineExclusivity('fixed', true);
      assert.equal(fixed.refine, false);
      assert.equal(fixed.refineDisabled, true);
      assert.match(fixed.note, /cannot both stack and re-meter/);
    """)


def test_critique_trail_rows_render_off_line_and_attempts() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      const off=window.AutopilotWorkflows.critiqueTrailRows({refinement:{enabled:false}, critique_trail:[{attempt_index:0}]});
      assert.equal(off.length, 1);
      assert.equal(off[0].disabled, true);
      assert.equal(off[0].text, 'Refine was off (1 exposure)');
      const trail=window.AutopilotWorkflows.critiqueTrailRows({
        refinement:{enabled:true, attempts:2},
        critique_trail:[
          {attempt_index:0, target_brightness_requested:112,
           critique:{verdict:'dark', mean_brightness:70, issues:['shadows crushed']},
           correction_applied:{delta:1.0, reason:'raise exposure'}, stop_reason:null},
          {attempt_index:1, target_brightness_requested:112,
           critique:{verdict:'good', mean_brightness:110, issues:[]},
           correction_applied:null, stop_reason:'good_enough'}
        ]});
      assert.equal(trail.length, 2);
      assert.equal(trail[0].disabled, false);
      assert.match(trail[0].critique, /dark/);
      assert.match(trail[0].adjust, /raise exposure/);
      assert.equal(trail[1].stopReason, 'good_enough');
    """)


def test_panorama_preview_summary_reports_coverage_and_overlap() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      const text=window.AutopilotWorkflows.panoramaPreviewSummary({
        frame_count:4,
        receipt:{coverage:{valid_fraction:0.82},
                 alignment:[{estimated_overlap_fraction:0.3},{estimated_overlap_fraction:0.5}],
                 output:{width:4000, height:1200}}});
      assert.match(text, /4 frames/);
      assert.match(text, /82% coverage/);
      assert.match(text, /overlap 30–50%/);
      assert.match(text, /4000×1200/);
      assert.equal(window.AutopilotWorkflows.panoramaPreviewSummary(null), '');
    """)


def test_handheld_terminal_status_stops_polling() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      assert.equal(window.AutopilotWorkflows.handheldTerminal('running'), false);
      assert.equal(window.AutopilotWorkflows.handheldTerminal('created'), false);
      assert.equal(window.AutopilotWorkflows.handheldTerminal('stopped'), true);
      assert.equal(window.AutopilotWorkflows.handheldTerminal('error'), true);
    """)


# --- new mobile / deflicker / night-assist controls (2026-09-07) ------------------------------
def test_deflicker_and_night_assist_control_ids_exist() -> None:
    for element_id in [
        "timelapse-deflicker", "timelapse-deflicker-window", "timelapse-deflicker-gain",
        "night-assist-run", "night-assist-result",
    ]:
        assert f'id="{element_id}"' in HTML, f"missing element id {element_id}"


def test_deflicker_rides_inside_timelapse_options_only_when_enabled() -> None:
    # The object is spread into timelapse_options (not a sibling field), and gated on the checkbox
    # so non-timelapse sessions never carry an unknown option.
    assert "...deflickerOption()" in HTML
    assert "function deflickerOption()" in HTML
    assert "deflicker:{enabled:true, window:Number($('timelapse-deflicker-window').value)" in HTML
    assert "max_gain_stops:Number($('timelapse-deflicker-gain').value)" in HTML


def test_night_assist_posts_last_gallery_image_to_plan_route() -> None:
    assert "'/night-assist/plan'" in WORKFLOWS or "/night-assist/plan" in WORKFLOWS
    assert "nightAssistPlan(" in WORKFLOWS
    assert "nightAssistPlan(" in HTML  # the UI actually calls it
    assert "insufficient_light" in HTML  # the "insufficient light" message state


def test_new_controls_meet_44px_touch_target_and_safe_area_rules() -> None:
    # Shutter + both start buttons carry an explicit >=44px minimum touch target.
    assert re.search(r"\.shutter,\s*#session-start,\s*#handheld-start[^{]*\{[^}]*min-height:\s*44px",
                     HTML), "missing 44px min-height rule for shutter/start buttons"
    # Sticky start buttons respect the bottom safe-area inset.
    assert "env(safe-area-inset-bottom, 0px) + 12px" in HTML
    assert "position: sticky" in HTML
    # No sideways page scroll guard.
    assert "overflow-x: hidden" in HTML
    # Frame lists become horizontal scroll strips.
    assert 'id="handheld-frames" class="frames-strip"' in HTML
    assert re.search(r"\.frames-strip\s*\{[^}]*overflow-x:\s*auto", HTML)
