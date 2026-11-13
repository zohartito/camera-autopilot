"""The camera picker must be readable without lying about identity.

libgphoto2 names the two supported bodies inconsistently: one reports
"Sony Alpha-A7 III (PC Control)" and the other the bare code "Sony ILCE-7M2 (Control)",
which a beginner does not recognise as their A7 II. The picker therefore shows a friendly
name, but the exact driver string must remain the identity that is selected and sent back,
because a sleeping body can report the wrong model (.claude/lessons.md, Identification).
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "backend" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CLIENT = (WEB / "client.js").read_text(encoding="utf-8")

sys.path.insert(0, str(REPO / "backend"))


def _friendly_map() -> dict[str, str]:
    block = re.search(r"CAMERA_MODEL_NAMES:\s*\{(.*?)\}", CLIENT, re.S).group(1)
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))


def test_every_supported_model_has_a_friendly_name() -> None:
    from engine import USB_CONTROL_MODELS

    friendly = _friendly_map()
    assert set(USB_CONTROL_MODELS) <= set(friendly), (
        "a supported body would show only its driver code", set(USB_CONTROL_MODELS) - set(friendly))


def test_friendly_names_are_human_readable_and_distinct() -> None:
    friendly = _friendly_map()
    assert friendly["Sony ILCE-7M2 (Control)"] == "Sony A7 II"
    assert friendly["Sony Alpha-A7 III (PC Control)"] == "Sony A7 III"
    assert len(set(friendly.values())) == len(friendly), "two bodies must not share a display name"
    for name in friendly.values():
        assert "ILCE" not in name, f"{name} still shows the model code a beginner cannot read"


def test_the_friendly_label_keeps_the_exact_driver_string_visible() -> None:
    # The reading aid must not hide the identity: the driver string is shown in parentheses.
    helper = re.search(r"friendlyCameraName\(model\)\s*\{(.*?)\n  \}", CLIENT, re.S).group(1)
    assert "${friendly} (${model})" in helper
    # The ternary's else branch returns the driver string untouched for an unknown body.
    assert re.search(r":\s*model;", helper), "an unknown body must fall back to its exact driver string"


def test_selection_still_sends_the_exact_driver_model_not_the_friendly_name() -> None:
    # The option's value is the index into the server's own camera list, and the stored pair
    # compares camera.model, so nothing selects on the display name.
    assert "new Option(`${Autopilot.friendlyCameraName(camera.model)} · ${camera.port}${camera.serial ? \"\" : \" · no USB serial, capture disabled\"}`, String(i))" in INDEX
    assert "camera.model === savedPair.model" in INDEX
    # Used once, and only to build the visible label.
    assert INDEX.count("friendlyCameraName") == 1, "the friendly name must be used only for the label"
    assert "option.title = camera.model;" in INDEX, "the exact driver string stays inspectable"
