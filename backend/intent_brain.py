"""The intent brain — turn a plain-language photo intent into an exposure plan + a plain "what it did".

NO API billing. Uses a LOCAL Ollama model (free, private): your Mac's Ollama by default, or set
OLLAMA_HOST (e.g. http://localhost:11434) and OLLAMA_MODEL. Pure stdlib (urllib) — no new deps.
Falls back to a deterministic keyword map if Ollama is unreachable or replies badly, so the
autopilot never stalls on the intent step.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

from security_bounds import json_response

from intent import (
    feel_from_intent,
    long_exposure_subject_from_intent,
    mood_card_from_intent,
    mood_variants_from_intent,
    motion_from_intent,
    variant_recipe,
    target_from_intent,
    white_balance_from_intent,
)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct")

_SYSTEM = (
    "You translate a photographer's plain-language intent into autopilot settings for the scene "
    "in front of the camera. Reply with ONLY a JSON object, no prose:\n"
    '{"target_brightness": <number 60-180; lower = moodier/darker, ~112 = neutral, higher = '
    'brighter/airier>, "feel": "<short phrase of what you did, e.g. kept it moody>", '
    '"why": "<one plain sentence a beginner understands>", '
    '"white_balance": "warm" | "cool" | null}'
)


def _fallback_why(intent: str | None) -> str:
    if not intent or not intent.strip():
        return "Read the light and set a natural, balanced exposure for you."
    wb = white_balance_from_intent(intent)
    wb_note = f" and nudged white balance {wb}" if wb else ""
    return (
        f"You asked to {intent.strip()}, so I {feel_from_intent(intent)}{wb_note} by aiming the exposure "
        f"at a mean brightness of about {int(target_from_intent(intent))}."
    )


def _fallback(intent: str | None) -> dict:
    target = target_from_intent(intent)
    white_balance = white_balance_from_intent(intent)
    return {
        "target_brightness": target,
        "feel": feel_from_intent(intent),
        "why": _fallback_why(intent),
        "white_balance": white_balance,
        "mood": mood_card_from_intent(intent, target, white_balance),
        "source": "fallback",
        "variant_id": None,
        "requested_feel": feel_from_intent(intent),
        "outcome_verified": False,
        "motion_intent": motion_from_intent(intent),
        **_exposure_route(intent),
    }


def _exposure_route(intent: str | None) -> dict:
    """Light trails / silky water / long exposure resolve to ONE long exposure (engine-gated)."""
    subject = long_exposure_subject_from_intent(intent)
    return {"exposure_route": "long_single_exposure" if subject else "ordinary",
            "long_exposure_subject": subject}


def _coerce(plan: dict, intent: str | None) -> dict:
    """Validate the model's JSON, clamp brightness, and fill any missing field from the fallback."""
    out = _fallback(intent)
    out["source"] = "model"
    try:
        target = float(plan["target_brightness"])
        if math.isfinite(target):
            out["target_brightness"] = max(60.0, min(180.0, target))
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("feel", "why"):
        value = plan.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    out["white_balance"] = plan.get("white_balance") if plan.get("white_balance") in ("warm", "cool") else None
    # Generated prose cannot certify image sharpness, blur or an operation not yet run.
    for key in ("feel", "why"):
        if any(word in out[key].lower() for word in ("froze", "blurred", "perfectly sharp", "nailed")):
            out[key] = _fallback(intent)[key]
    out["requested_feel"] = out["feel"]
    out["mood"] = mood_card_from_intent(intent, out["target_brightness"], out["white_balance"])
    return out


def _ask_ollama(intent: str) -> dict:
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": intent}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.4},
    }
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as raw_response:
        response = json_response(raw_response)
    return json.loads(response["message"]["content"])


def interpret(intent: str | None, variant_id: str | None = None) -> dict:
    """Return the safe camera plan plus its visible mood card.

    Model failures fall back. Invalid explicit variants raise ValueError before capture.
    """
    if variant_id is not None:
        recipe = variant_recipe(variant_id)
        out = _fallback(recipe["intent"])
        out.update(target_brightness=float(recipe["target_brightness"]),
                   white_balance=recipe["white_balance"], variant_id=variant_id,
                   source="variant", motion_intent=motion_from_intent(intent), **_exposure_route(intent))
        out["mood"] = mood_card_from_intent(recipe["intent"], out["target_brightness"],
                                            out["white_balance"])
        out["why"] = (f"Requested {recipe['name']}: target brightness "
                      f"{out['target_brightness']:g}; white balance "
                      f"{recipe['white_balance'] or 'auto'}. Capture outcome is not yet verified.")
        return out
    if os.environ.get("CAMERA_INTENT_PROFILE", "full") == "compact":
        out = _fallback(intent)
        out["source"] = "compact_deterministic"
        out["runtime_profile"] = "compact"
        return out
    if not intent or not intent.strip():
        return _fallback(None)
    try:
        return _coerce(_ask_ollama(intent), intent)
    except Exception:
        return _fallback(intent)


def preview(intent: str | None) -> dict:
    """Fast deterministic preview for the UI before any camera-changing route is called."""
    plan = _fallback(intent)
    plan["source"] = "preview"
    return plan


def variants(intent: str | None) -> dict:
    """Return deterministic same-scene mood variants without touching the camera."""
    return mood_variants_from_intent(intent)


if __name__ == "__main__":
    for phrase in ["make it a moody noir film still", "bright and airy for instagram",
                   "freeze my kid running around", None]:
        print(repr(phrase), "->", json.dumps(interpret(phrase)))
