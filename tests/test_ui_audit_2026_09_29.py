"""2026-09-29 physical-iPhone UI audit: presentation fixes pinned without a browser.

Camera-free. The capture, identity and session request code is untouched; these tests pin the
layout contract (photo above its controls, explicit way back, settings apart from capture) and the
plain-language card rendering in client.js, executed in Node against a minimal fake DOM.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "backend" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
REVIEW = (WEB / "review.html").read_text(encoding="utf-8")
CLIENT = WEB / "client.js"
CSS, BODY = INDEX.split("</style>", 1)


def _node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    probe = subprocess.run([path, "--version"], check=False, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("node --version failed")
    return path


FAKE_DOM = """
const assert=require('node:assert/strict');
class El{constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.textContent='';this.hidden=false;}
  append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=[...nodes];}
  get childElementCount(){return this.children.length;}}
global.document={createElement:tag=>new El(tag)};
global.window={};
eval(require('fs').readFileSync(process.argv[1],'utf8'));
const Autopilot=window.Autopilot;
"""


def _run(code: str) -> str:
    return subprocess.run([_node(), "-e", FAKE_DOM + code, str(CLIENT)], check=True,
                          capture_output=True, text=True).stdout


def test_the_photo_has_its_own_box_and_every_control_sits_below_it() -> None:
    result = BODY.split('<section id="result"', 1)[1].split("</section>", 1)[0]
    photo = result.split('<div class="result-photo">', 1)[1].split("</div>", 1)[0]
    assert 'id="shot"' in photo and 'id="last-frame-canvas"' in photo
    after_photo = result.split('<div class="result-photo">', 1)[1].split("</div>", 1)[1]
    for element in ("last-frame-status", "last-frame-grid", "last-frame-zebra", "last-frame-histogram",
                    "constraint-cards", "result-mood", "auto-save-status", "feedback"):
        assert f'id="{element}"' in after_photo, element
    # The result is a scrolling sheet, not a fixed backdrop with a panel drawn over the photo.
    result_css = CSS.split("  .result {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in result_css
    overlay_css = CSS.split("  .result .overlay {", 1)[1].split("}", 1)[0]
    assert "position: absolute" not in overlay_css and "gradient" not in overlay_css
    assert "max-height:70dvh" not in CSS
    # The photo box takes the photo's shape before the aids are painted into it.
    render = INDEX.split("function renderLastFrameAids() {", 1)[1].split("\n  }\n", 1)[0]
    assert render.index("style.aspectRatio") < render.index("paintPreview")


def test_the_way_back_is_explicit_and_never_takes_a_photo() -> None:
    bar = BODY.split('<div class="result-bar">', 1)[1].split("</div>", 1)[0]
    assert re.search(r'<button id="again"[^>]*>.*New photo</button>', bar)
    assert "Again" not in bar
    handler = INDEX.split('$("again").addEventListener("click", () => {', 1)[1].split("});", 1)[0]
    assert "shoot(" not in handler and "captureRequest" not in handler
    assert '$("result").classList.remove("show");' in handler


def test_capture_comes_first_and_settings_follow_the_shooting_modes() -> None:
    order = [BODY.index(marker) for marker in (
        '<main id="home">', 'id="shutter"', '<details id="preview-panel"', '<details id="sessions-panel"',
        '<details id="handheld-panel"', '<details id="device-panel"')]
    assert order == sorted(order)
    shutter_bar = BODY.split('<div class="shutter-bar">', 1)[1].split("</div>", 1)[0]
    assert 'id="shutter"' in shutter_bar and 'id="caption"' in shutter_bar
    bar_css = CSS.split("  .shutter-bar {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in bar_css and "var(--safe-bottom)" in bar_css
    # No focus-dependent layout: a card that grows on blur would move the shutter mid-tap.
    assert ":focus-within" not in CSS


def test_auto_save_copy_is_short_and_the_forget_button_is_marked_destructive() -> None:
    panel = BODY.split('<details id="device-panel"', 1)[1].split("</details>", 1)[0]
    for jargon in ("AutopilotMedia", "saved=true", "Stage P", "plugin"):
        assert jargon not in panel, jargon
    assert re.search(r"#forget-device \{[^}]*color: var\(--warn\)", CSS)


def test_cards_lead_with_a_plain_status_and_keep_the_detail_behind_a_disclosure() -> None:
    out = _run("""
    const box=document.createElement('div');
    Autopilot.renderCards(box,[{axis:'SHUTTER',value:'1/10s',status:'limited',reason:'below the blur-safe floor',
      sub_factors:[{name:'Vibrations',readout:'1/35s modeled handheld floor',status:'limited'}]},
      {axis:'ISO',value:'ISO 400',status:'ok',reason:'clean',sub_factors:[]}]);
    const [shutter,iso]=box.children;
    const [heading,status,reason,details]=shutter.children;
    process.stdout.write(JSON.stringify({heading:heading.textContent,status:status.textContent,title:status.title,
      reason:reason.textContent,data:shutter.dataset.status,detailsTag:details.tagName,
      summary:details.children[0].textContent,line:details.children[1].textContent,
      isoChildren:iso.children.length,hidden:box.hidden}));
    """)
    card = json.loads(out)
    assert card["heading"] == "Shutter speed · 1/10s"
    assert card["status"] == "Limited" and card["title"] == "limited" and card["data"] == "limited"
    assert card["reason"] == "below the blur-safe floor"
    assert card["detailsTag"] == "DETAILS" and card["summary"] == "Details"
    assert card["line"] == "Vibrations: 1/35s modeled handheld floor (limited)"
    assert card["isoChildren"] == 3 and card["hidden"] is False


def test_the_picker_groups_non_sony_devices_without_changing_selection_values() -> None:
    out = _run("""
    process.stdout.write(JSON.stringify(['Sony Alpha-A7 III (PC Control)','Sony ILCE-7M2 (Control)',
      'Apple iPhone 5 (PTP mode)','',null].map(model=>Autopilot.isSupportedCameraModel(model))));
    """)
    assert json.loads(out) == [True, True, False, False, False]
    refresh = INDEX.split("async function refreshCameras() {", 1)[1].split("\n  }\n", 1)[0]
    assert "Autopilot.isSupportedCameraModel(camera.model) ? cameraGroup : otherGroup" in refresh
    assert "String(i))" in refresh  # the option value is still the index into the server's list
    assert refresh.count("isSupportedCameraModel") == 1


def test_review_keeps_delete_away_from_share_and_save() -> None:
    card = REVIEW.split("function makeCard(shot) {", 1)[1].split("card.innerHTML", 1)[1].split("`;", 1)[0]
    rows = re.findall(r'<div class="row[^"]*">(.*?)</div>', card)
    primary = next(row for row in rows if 'class="save"' in row)
    assert 'class="share"' in primary and "delete" not in primary
    danger = next(row for row in rows if 'class="delete danger"' in row)
    assert danger.count("<button") == 1


def test_a_resize_repaints_whenever_the_painted_box_changes() -> None:
    """Review of #117: comparing the two scale factors skipped proportional resizes (390x260 ->
    780x520 kept a 390x260 bitmap, stretching the photo and overlays)."""
    render = INDEX.split("function renderLastFrameAids() {", 1)[1].split("\n  }\n", 1)[0]
    key = "`${canvas.clientWidth}x${canvas.clientHeight}@${window.devicePixelRatio}`"
    assert f"canvas.dataset.paintedBox = {key};" in render
    assert render.index("paintPreview") < render.index("canvas.dataset.paintedBox")
    handler = re.search(r"  function repaintResultIfBoxChanged\(\) \{.*?\n  \}\n", INDEX, re.S).group()
    assert f"canvas.dataset.paintedBox !== {key}" in handler
    assert "renderLastFrameAids()" in handler
    assert 'window.addEventListener("resize", repaintResultIfBoxChanged);' in INDEX
    # Review of #117 round 3: a DPR-only change fires no resize event; a resolution query does.
    watcher = re.search(r"  function watchPixelRatio\(\) \{.*?\n  \}\n", INDEX, re.S).group()
    assert "(resolution: ${window.devicePixelRatio}dppx)" in watcher
    assert "repaintResultIfBoxChanged(); watchPixelRatio();" in watcher
    assert "{ once: true }" in watcher  # one live listener per ratio, re-armed after each change


def test_session_previews_are_on_by_default_and_the_timelapse_shows_its_expected_duration() -> None:
    """Owner's phone timelapse ran with preview_every 0 (Off was the default) and showed nothing."""
    timelapse = re.search(r'<select id="timelapse-preview-every".*?</select>', BODY, re.S).group()
    assert re.search(r'<option value="4" selected>', timelapse)
    assert '<option value="0">Off</option>' in timelapse  # still available
    panorama = re.search(r'<select id="panorama-preview-every".*?</select>', BODY, re.S).group()
    assert re.search(r'<option value="1" selected>', panorama)
    # The request shape is unchanged: the same fields feed the same payload keys.
    assert "preview_every:Number($('timelapse-preview-every').value)" in INDEX
    assert "preview_every:Number($('panorama-preview-every').value)" in INDEX
    assert 'id="timelapse-duration"' in BODY
    duration = re.search(r"  function showTimelapseDuration\(\) \{.*?\n  \}\n", INDEX, re.S).group()
    assert "apiFetch" not in duration and "sessionRequest" not in duration  # display only
    assert "The camera can make the real interval longer." in duration


def _timelapse_duration_text(values: dict) -> str:
    duration = re.search(r"  function showTimelapseDuration\(\) \{.*?\n  \}\n", INDEX, re.S).group()
    script = ("const values=" + json.dumps(values) + ";const target={textContent:''};"
              "const $=id=>id==='timelapse-duration'?target:(id==='smartlapse-enabled'?{checked:values[id]}:{value:values[id]});"
              + duration + "showTimelapseDuration();process.stdout.write(target.textContent);")
    return subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout


def test_the_expected_timelapse_duration_is_a_true_lower_bound() -> None:
    """Review of #117: rounding up made 4 frames every 30 s (90 s) read "at least 2 min"."""
    base = {"session-mode": "timelapse", "smartlapse-enabled": False, "timelapse-fps": "24"}
    text = _timelapse_duration_text({**base, "session-frames": "4", "session-interval": "30"})
    assert text.startswith("Expected: at least 1 min 30 s of shooting (4 frames, one every 30 s)"), text
    assert _timelapse_duration_text({**base, "session-frames": "240", "session-interval": "10"}).startswith(
        "Expected: at least 39 min 50 s of shooting")  # 239 intervals of 10 s
    assert "at least 2 h 59 min" in _timelapse_duration_text(
        {**base, "session-frames": "1080", "session-interval": "10"})  # 10790 s, rounded down
    assert "making a 10 s movie at 24 fps" in _timelapse_duration_text(
        {**base, "session-frames": "240", "session-interval": "10"})
    assert "as fast as the camera allows" in _timelapse_duration_text(
        {**base, "session-frames": "3", "session-interval": "0"})
    assert _timelapse_duration_text({**base, "session-mode": "hdr", "session-frames": "3", "session-interval": "5"}) == ""


def test_review_targets_are_44px_and_disclosures_keep_their_marker() -> None:
    css = REVIEW.split("</style>", 1)[0]
    assert "button {cursor:pointer;min-height:44px;min-width:44px}" in css
    assert "input, select {min-height:44px}" in css
    assert ".empty a, .error a {display:inline-block;min-height:44px;line-height:44px}" in css
    assert "summary {min-height:44px;line-height:44px;cursor:pointer}" in css
    # display:flex on a summary drops the native disclosure marker; no summary rule may set it.
    rules = re.findall(r"([^{}]*)\{([^}]*)\}", css)
    summary_rules = [body for selector, body in rules if re.search(r"(?:^|[\s,>])summary(?![-\w])", selector)]
    assert summary_rules
    for body in summary_rules:
        assert "display:flex" not in body.replace(" ", ""), body
