"""Static and node checks for the Crowd Control live preview, share and Stop when clean button."""
from pathlib import Path
import subprocess

WEB = Path(__file__).resolve().parents[1] / "backend/web"
HTML = (WEB / "index.html").read_text()
WORKFLOWS = (WEB / "workflows.js").read_text()


def run_js(code: str) -> None:
    subprocess.run(["node", "-e", "const assert=require('node:assert/strict');\n" + code],
                   check=True, capture_output=True, text=True)


def test_sessions_panel_declares_crowd_controls() -> None:
    for element_id in ["crowd-options", "crowd-stop-at", "crowd-preview-wrap", "crowd-preview-img",
                       "crowd-preview-status", "crowd-stop"]:
        assert f'id="{element_id}"' in HTML, f"missing element id {element_id}"
    assert "Stop when clean" in HTML
    assert "25% of the frames" in HTML
    assert "needs a budget of 6 to 8 frames" in HTML  # the server refuses auto-stop above 8


def test_ui_wires_stop_route_preview_stream_and_threshold() -> None:
    assert "'/sessions/'+activeSession+'/stop'" in HTML
    assert "stop_when_cleared:crowdStopAt" in HTML
    assert "renderCrowdPreview(state)" in HTML
    assert "'/crowd-preview'" in WORKFLOWS
    # The Stop button is shown only while a crowd session runs.
    assert "$('crowd-stop').hidden=!(crowd && state.status==='running')" in HTML


def test_crowd_summaries_report_share_and_stop() -> None:
    run_js("const window={};\n" + WORKFLOWS + """
      const flows=window.AutopilotWorkflows;
      const text=flows.crowdPreviewSummary({accepted_frames:5, cleared_share:0.914, not_cleared_share:0.086,
                                            visibility_floor_frames:2});
      assert.match(text, /from 5 frames/);
      assert.match(text, /91\\.4% clear/);
      assert.match(text, /8\\.6% not yet cleared/);
      assert.match(text, /2\\+ frames \\(25% rule\\)/);
      assert.match(text, /never moves/);
      assert.doesNotMatch(text, /automatic stop waits/);  // older previews carry no regions
      const waiting=flows.crowdPreviewSummary({accepted_frames:6, cleared_share:0.95, visibility_floor_frames:2,
                                               regions:{all_seen_clear:false, pending_share:0.071}});
      assert.match(waiting, /automatic stop waits: 7\\.1% of the frame/);
      assert.match(flows.crowdPreviewSummary({accepted_frames:5, cleared_share:0.97,
                                              regions:{all_seen_clear:true, pending_share:0}}),
                   /Every spot people crossed has been seen clear again/);
      assert.equal(flows.crowdPreviewSummary(null), '');
      assert.equal(flows.crowdPreviewSummary({accepted_frames:2}), '');
      assert.match(flows.crowdStopSummary({reason:'user', accepted_frames:4, requested_frames:8}),
                   /when you pressed Stop after 4 of 8 frames/);
      assert.match(flows.crowdStopSummary({reason:'cleared_share_threshold', threshold:0.95,
                                           accepted_frames:6, requested_frames:8}), /automatically at 95% clear/);
      assert.equal(flows.crowdStopSummary(null), '');
      // The shown image is keyed by session and snapshot hash, never by frame count alone.
      const a=flows.crowdPreviewKey('s1',{accepted_frames:3, sha256:'aa'});
      assert.notEqual(a, flows.crowdPreviewKey('s2',{accepted_frames:3, sha256:'aa'}));
      assert.notEqual(a, flows.crowdPreviewKey('s1',{accepted_frames:3, sha256:'bb'}));
      assert.equal(flows.crowdPreviewKey('s1',null), '');
    """)
