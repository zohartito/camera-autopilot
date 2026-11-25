"""The exposure a Fixed stack or computational session starts from is visible before capture.

/shoot leaves its final settings on the camera. A computational session starts from them without
metering; a Fixed stack meters first unless the owner keeps the body's current exposure. The capture
page must say which, and show the last values it knows. These are static checks over the shipped
HTML plus node runs of the pure text helpers.
"""
import json
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "backend/web"
HTML = (WEB / "index.html").read_text()
WORKFLOWS = (WEB / "workflows.js").read_text()


def run_js(code: str) -> str:
    return subprocess.run(["node", "-e", "const assert=require('node:assert/strict');\nconst window={};\n"
                           + WORKFLOWS + "\n" + code], check=True, capture_output=True, text=True).stdout


def test_baseline_elements_sit_in_the_fixed_stack_and_sessions_panels() -> None:
    stack_block = HTML[HTML.index('id="stack-settings"'):HTML.index('id="device-panel"')]
    assert 'id="stack-baseline"' in stack_block
    assert 'id="stack-baseline-source"' in stack_block
    assert 'id="stack-baseline-row"' in stack_block
    assert '<option value="metered">' in stack_block and '<option value="current">' in stack_block
    assert stack_block.index('<option value="metered">') < stack_block.index('<option value="current">')
    sessions_block = HTML[HTML.index('id="sessions-panel"'):HTML.index('id="session-start"')]
    assert 'id="session-baseline"' in sessions_block


def test_page_renders_on_load_and_records_after_an_ordinary_photo() -> None:
    assert "renderBodyBaseline();" in HTML
    assert 'if (captureMode === "photo") recordBodyBaseline(shot);' in HTML
    # A stack session restores its starting settings, so it must not overwrite the baseline.
    assert "shot.evidence?.stack_policy?.session_id) return;" in HTML
    # Sessions meter first unless the owner keeps the body's exposure (Session meter-first).
    assert 'AutopilotWorkflows.sessionBaselineText(bodyBaseline, sessionBaselineSource(), nightPlan)' in HTML
    assert "AutopilotWorkflows.inheritedBaselineText(bodyBaseline, stackBaselineSource())" in HTML
    assert "AutopilotWorkflows.fixedStackBudgetText(frames, stackBaselineSource(), captureBudgets['fixed_'+stackBaselineSource()])" in HTML
    assert "AutopilotWorkflows.stackBaselineOutcomeText(stack.baseline)" in HTML


def test_text_is_honest_when_nothing_is_known() -> None:
    out = run_js("process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText(null));")
    assert out.startswith("A computational session starts from whatever exposure is on the body now")
    assert "does not meter first" in out
    assert "unknown here" in out and "check the camera before starting" in out


def test_fixed_stack_text_says_it_meters_first_and_shows_the_exposure_it_will_not_use() -> None:
    baseline = {"settings": {"iso": "3200", "aperture": "f/2.8", "shutter": "1/15"},
                "ts": 1_758_000_000_000, "readback": "verified"}
    out = run_js("process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText("
                 f"{json.dumps(baseline)}, 'metered'));")
    assert out.startswith("A Fixed stack meters first")
    assert "does not meter first" not in out
    assert "ISO 3200 · aperture f/2.8 · shutter 1/15" in out
    assert "not used" in out and "restores it afterwards" in out
    unknown = run_js("process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText(null, 'metered'));")
    assert unknown.startswith("A Fixed stack meters first") and "unknown here" in unknown


def test_keep_current_text_says_the_stack_does_not_meter() -> None:
    out = run_js("process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText(null, 'current'));")
    assert out.startswith("Keep current exposure: the Fixed stack starts from whatever exposure is on the body now")
    assert "does not meter first" in out and "check the camera before starting" in out


def test_budget_text_counts_the_meter_frame_unless_current_is_kept() -> None:
    metered = run_js("process.stdout.write(window.AutopilotWorkflows.fixedStackBudgetText(3, 'metered', {baseline_meter_exposures:1, extra_meter_exposures:2}));")
    assert metered.startswith("Fixed stack: up to 6 physical photos")
    assert "1 meter photo" in metered and "up to 2 more" in metered
    current = run_js("process.stdout.write(window.AutopilotWorkflows.fixedStackBudgetText(3, 'current', {baseline_meter_exposures:0, extra_meter_exposures:0}));")
    assert current.startswith("Fixed stack: 3 physical photos") and "No meter photo" in current


def test_outcome_text_names_both_the_metered_and_the_inherited_exposure() -> None:
    metered = {"source": "metered", "metered": {"iso": "100", "aperture": "8", "shutter": "1/500"},
               "inherited": {"iso": "3200", "aperture": "8", "shutter": "1/15"}}
    out = run_js(f"process.stdout.write(window.AutopilotWorkflows.stackBaselineOutcomeText({json.dumps(metered)}));")
    assert "Baseline metered: ISO 100 · 8 · 1/500" in out and "ISO 3200 · 8 · 1/15" in out
    kept = {"source": "inherited", "metered": None, "inherited": {"iso": "3200", "aperture": "8", "shutter": "1/15"}}
    out = run_js(f"process.stdout.write(window.AutopilotWorkflows.stackBaselineOutcomeText({json.dumps(kept)}));")
    assert "Baseline kept from the body: ISO 3200 · 8 · 1/15" in out and "No meter photo" in out
    assert run_js("process.stdout.write(window.AutopilotWorkflows.stackBaselineOutcomeText(null));") == ""


def test_text_shows_the_inherited_settings_from_the_last_shot() -> None:
    baseline = {"settings": {"iso": "400", "aperture": "f/2.8", "shutter": "1/30", "white_balance": "Auto"},
                "image": "shot_1.jpg", "ts": 1_758_000_000_000, "readback": "verified"}
    out = run_js(f"process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText({json.dumps(baseline)}));")
    assert "ISO 400 · aperture f/2.8 · shutter 1/30" in out
    assert "readback verified" in out
    assert "The last photo from this app at " in out
    assert "Anything changed on the camera since then is the real starting point." in out
    assert "white_balance" not in out and "Auto" not in out


def test_unverified_readback_and_missing_values_are_not_dressed_up() -> None:
    baseline = {"settings": {"iso": "200"}, "ts": None, "readback": "unavailable"}
    out = run_js(f"process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText({json.dumps(baseline)}));")
    assert "ISO 200 (readback not verified)" in out
    assert " at " not in out.split("The last photo")[1].split("left")[0]
    empty = run_js("process.stdout.write(window.AutopilotWorkflows.inheritedBaselineText({settings:{}}));")
    assert "no exposure values were reported" in empty
