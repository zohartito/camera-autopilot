"""Seamless camera setup — the autopilot configures the camera itself.

Part of the "it just works" device vision: instead of you diving into menus, the autopilot pushes
the right settings for each shot/mode. Two tiers, because the A7 II's USB (PC Remote) PTP surface is
limited — the same wall that blocks remote focus-point also hides most deep-menu items:

  - REMOTE (settable over USB now): the per-shot shooting params gphoto2 exposes on the A7 II —
    focus mode, image quality, drive mode, white balance (ISO/aperture/shutter are driven per-scene
    by the exposure brain). `configure()` pushes these before a shoot.
  - ONE-TIME (the deep menu items the A7 II's PTP does NOT expose): Long Exposure NR, Face Detect,
    High ISO NR, SteadyShot, Color Space... These are set-and-forget (they don't change per shot),
    so the app walks you through them ONCE — `ONE_TIME_CHECKLIST`.

On a CRSDK body (A7 IV+) almost all of these become remote, so the device configures nearly
everything itself — see `wifi_engine.py` / `docs/FOCUS_HARDWARE.md`.
"""
from __future__ import annotations

try:
    import settings_registry
except Exception:  # pragma: no cover - keep this module usable in stripped-down test envs
    settings_registry = None

# A7 II PC-Remote gphoto2 widget nodes the body exposes over USB (friendly name -> gphoto node).
# LIVE FINDING (A7 II, 2026-06-21): image_quality + white_balance + ISO/aperture/shutter WRITE fine
# over USB; focus_mode READS but is LOCKED against writes ("stayed at Automatic") — so configure()
# best-effort-skips it on the A7 II and it lives in ONE_TIME_CHECKLIST instead. On a CRSDK body
# (A7 IV+) focus_mode writes remotely. Also: gphoto2 labels the A7 II focus modes
# Manual / Automatic(=AF-S) / AF-C / AF-A / DMF, and image quality RAW+JPEG is "RAW+JPEG (Fine)".
REMOTE_NODES = {
    "focus_mode": "focusmode",      # A7 II: read-only over USB (set in menu); CRSDK: settable
    "image_quality": "imagequality",
    "drive_mode": "drivemode",
    "white_balance": "whitebalance",
}

# Per-mode profiles: the remote-settable params the autopilot pushes before a shot. Values are
# fuzzy-matched to the live widget choices at apply time (the A7 II's exact strings — "AF-S" vs
# "Single-shot AF", "RAW+JPEG" vs "RAW & JPEG" — vary by firmware, so we don't hard-depend on them).
PROFILES = {
    "default": {"focus_mode": "AF-S", "image_quality": "RAW+JPEG", "drive_mode": "Single"},
    "action":  {"focus_mode": "AF-C", "image_quality": "RAW+JPEG", "drive_mode": "Continuous"},
    "stack":   {"focus_mode": "AF-S", "image_quality": "RAW",      "drive_mode": "Bracket"},
    "night":   {"focus_mode": "AF-S", "image_quality": "RAW",      "drive_mode": "Single"},
}

# Deep-menu items the A7 II does NOT expose over USB -> a one-time, set-and-forget wizard.
# (label, menu location, target, why)
_FALLBACK_ONE_TIME_CHECKLIST = [
    ("Focus Mode",       "Camera p3", "AF-S",         "autofocus master switch — locked over A7 II USB, set here"),
    ("Long Exposure NR", "Camera p6", "Off",          "doubles long exposures — breaks stacking/timelapse"),
    ("Face Detection",   "Camera p6", "On",           "native AF locks onto people — complements saliency"),
    ("High ISO NR",      "Camera p6", "Low",          "keep detail — the autopilot denoises in software"),
    ("SteadyShot",       "Camera p7", "Off on tripod", "IBIS can add micro-motion on a tripod"),
    ("Color Space",      "Camera p7", "sRGB",         "matches the metering pipeline (already set)"),
]


def one_time_checklist() -> list[tuple[str, str, str, str]]:
    """Deep-menu setup guidance, backed by the full settings registry when available."""
    if settings_registry is None:
        return list(_FALLBACK_ONE_TIME_CHECKLIST)
    try:
        return settings_registry.registry_setup_checklist()
    except Exception:
        return list(_FALLBACK_ONE_TIME_CHECKLIST)


ONE_TIME_CHECKLIST = one_time_checklist()


# Abbreviation <-> full-name aliases — gphoto2 may report either form on the A7 II (we don't yet
# know which, since the live widget choices couldn't be read while the camera was in its menus).
_ALIASES = [
    {"af-s", "single-shot af", "single shot af", "automatic"},  # gphoto2 reports AF-S as "Automatic" on the A7 II
    {"af-c", "continuous af"},
    {"dmf", "direct manual focus"},
    {"mf", "manual", "manual focus"},
    {"raw+jpeg", "raw & jpeg", "raw + jpeg", "raw jpeg"},
    {"single", "single shooting"},
    {"continuous", "cont. shooting", "continuous shooting"},
    {"bracket", "cont. bracket", "continuous bracket"},
]


def _aliases_of(value: str) -> set[str]:
    low = value.lower()
    out = {low}
    for group in _ALIASES:
        if low in group:
            out |= group
    return out


def _match(value: str, choices: list[str]) -> str | None:
    """Match a desired value to a real widget choice — exact/alias, then substring (either way),
    then token overlap (so 'AF-S'->'Single-shot AF', 'RAW+JPEG'->'RAW & JPEG' resolve)."""
    aliases = _aliases_of(value)
    for choice in choices:
        if choice.lower() in aliases:
            return choice
    for choice in choices:
        lower = choice.lower()
        if any(alias in lower for alias in aliases):  # choice CONTAINS an alias (not the reverse —
            return choice                             # else "RAW" wrongly matches "RAW+JPEG")
    want = set(value.lower().replace("+", " ").split())
    for choice in choices:
        if want & set(choice.lower().replace("+", " ").replace("&", " ").split()):
            return choice
    return None


def configure(profile: str, get_options, set_node) -> dict:
    """Push a profile's remote-settable params onto the camera. `get_options(node) -> [choices]` and
    `set_node(node, value)` are supplied by the caller (wrapping the camera link), so this stays
    testable + transport-agnostic. Best-effort: a setting that can't be matched or set is reported
    and skipped, never raised — the exposure brain still runs."""
    wanted = PROFILES.get(profile, PROFILES["default"])
    results = {}
    for key, value in wanted.items():
        node = REMOTE_NODES[key]
        try:
            choices = get_options(node)
            target = _match(value, choices) if choices else value
            if target is None:
                results[key] = f"no match for '{value}' in {choices}"
                continue
            set_node(node, target)
            results[key] = f"set -> {target}"
        except Exception as exc:  # noqa: BLE001 - best-effort; never block the shoot
            results[key] = f"skipped ({exc.__class__.__name__})"
    return results


if __name__ == "__main__":
    print("Remote-settable over A7 II USB:", ", ".join(REMOTE_NODES))
    print("\nOne-time menu checklist (not remotely settable on the A7 II):")
    for label, where, target, why in ONE_TIME_CHECKLIST:
        print(f"  [{where}] {label} -> {target}  ({why})")
