"""Review the actual JPEG with a local vision model after capture.

The configured 8B model runs on this Mac through Ollama. VISION_HOST can select
another explicitly configured host. A critique is model evidence, not a guarantee
of photographic quality; the live verification missed clipped window highlights.

    VISION_HOST=http://127.0.0.1:11434 python vision_critique.py [image.jpg] ["the intent"]
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

from image_safety import open_rgb
from security_bounds import json_response, read_bounded

# Vision defaults to the local model service; VISION_HOST can explicitly select a remote host.
# The recovered Mac runtime verified the configured 8B vision model on actual camera JPEGs.
VISION_HOST = os.getenv("VISION_HOST", "http://127.0.0.1:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:8b-instruct-q4_K_M")

_SYSTEM = (
    "You are a sharp photography director reviewing a photo a beginner's camera just took for a "
    "stated intent. LOOK at the image and judge it honestly. Reply with ONLY JSON, no prose:\n"
    '{"matches_intent": true|false, '
    '"exposure": "too dark" | "good" | "too bright", '
    '"brightness_delta": <int from -60 to 60; how far to shift the exposure target to fix it, '
    '0 if good>, '
    '"issues": ["short concrete problems, e.g. blown highlights, crooked horizon, busy background"], '
    '"suggestion": "<one plain sentence telling the camera what to change next shot>"}'
)


def critique(image_path: str | Path, intent: str | None) -> dict:
    """Ask the vision model to look at the shot and say how to improve it for the intent."""
    # Downscale before sending — the camera JPEG is 24MP; a vision model only needs ~1024px, and
    # the full-size base64 payload makes Ollama reject the request (HTTP 400).
    im = open_rgb(image_path)
    im.thumbnail((1024, 1024))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=85)
    image_b64 = base64.b64encode(buf.getvalue()).decode()
    prompt = f"Intent: {intent or 'a clean, well-balanced photo'}. Review this photo."
    body = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt, "images": [image_b64]},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2},
    }
    request = urllib.request.Request(
        f"{VISION_HOST}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(os.getenv("VISION_TIMEOUT_SECONDS", "45"))) as raw_response:
            response = json_response(raw_response)
    except urllib.error.HTTPError as exc:
        error = read_bounded(exc, limit=8 * 1024).decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Ollama HTTP {exc.code}: {error}") from exc
    return json.loads(response["message"]["content"])


if __name__ == "__main__":
    default_img = Path.home() / "Code/sony-camera-mcp/captures/20260616-190254_capt_DSC00512.JPG"
    img = sys.argv[1] if len(sys.argv) > 1 else str(default_img)
    intent = sys.argv[2] if len(sys.argv) > 2 else "moody golden hour, warm and intimate"
    print(f"  looking at: {Path(img).name}")
    print(f"  shot for  : {intent}\n")
    print(json.dumps(critique(img, intent), indent=2))
