"""Escape the postview: pull the full-resolution JPEG for THIS Wi-Fi capture — and publish it only
when its identity as this capture is proved by *content* (and, when available, *time*), never by
"newest file on the card".

Why identity has to be proved
-----------------------------
`wifi_a7ii.transfer_latest_full_res` walks the card newest-first and downloads the latest still. On a
tripod shooting a static scene, "newest" is usually the shot we just fired — but "usually" is not
proof. A stale frame from moments ago, or someone else's camera writing to the card, would sail
through a newest-file heuristic. So before a full-res file is allowed to REPLACE the verified
postview as the published image, it must match the postview by content, and — when the card exposes a
per-file createdTime — fall inside a tight window after the shutter.

The content test (`identity_score` / `verify_identity`)
-------------------------------------------------------
Both images are decoded through `image_safety.open_rgb` (bounded, never a raw `Image.open`),
downscaled to the SAME small working grid (160 px wide, height from the postview's aspect), converted
to luma, and compared two ways:

  * **NCC** — normalized cross-correlation of the two luma grids. 1.0 is identical framing; it falls
    as the scene's *structure* diverges. Threshold: **>= 0.97**.
  * **pHash Hamming** — 64-bit difference-hash distance. 0 is identical; small distances tolerate
    JPEG re-compression and the postview-vs-full-res resolution gap. Threshold: **<= 6**.
  * **aspect agreement** — the postview and the still must share an aspect ratio within **2%** (a
    16:9 movie grab can never be the identity match for a 3:2 still).

A caveat this is honest about: a near-identical EARLIER frame of the SAME static scene would ALSO
pass NCC and pHash — content alone cannot tell two photographs of an unchanged tripod scene apart.
That is exactly why the createdTime window is MANDATORY whenever the card provides it, and why the
receipt records `identity_basis` as ``"content+time"`` (both proofs) or ``"content_only"`` (content
matched, but no timestamp was available to confirm it was THIS shutter). Content-only is a weaker
claim, and the receipt says so.

The time test
-------------
When both timestamps are supplied, the card file's createdTime must land in **[-2 s, +30 s]** of the
shutter time: a small negative slack for clock skew between host and body, and up to 30 s forward to
cover the card write + Contents-Transfer mount latency before the file is browsable.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from image_safety import open_rgb
from stack_session import frame_receipt

# --- thresholds (documented in the module docstring) ---------------------------------------------
NCC_MIN = 0.97          # normalized cross-correlation of luma; 1.0 = identical framing
PHASH_MAX = 6           # max 64-bit difference-hash Hamming distance
ASPECT_TOL = 0.02       # postview vs still aspect ratios must agree within 2%
CREATED_MIN_S = -2.0    # createdTime may sit up to 2 s before the shutter (clock skew)
CREATED_MAX_S = 30.0    # ...and up to 30 s after (card write + Contents-Transfer mount latency)

_WORK_WIDTH = 160       # both images are compared on a 160 px-wide luma grid


def _resized_luma(path: str | Path, size: tuple[int, int]) -> bytes:
    """Decode `path` through the bounded RGB safety gate, resize to `size`, return flat luma bytes."""
    with open_rgb(path) as image:
        return image.resize(size, Image.BILINEAR).convert("L").tobytes()


def _dhash(path: str | Path) -> int:
    """64-bit difference hash: resize to 9x8 luma, one bit per adjacent horizontal pair."""
    with open_rgb(path) as image:
        pixels = image.resize((9, 8), Image.BILINEAR).convert("L").tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _normalized_cross_correlation(first: list[int], second: list[int]) -> float:
    """Zero-mean NCC of two equal-length luma grids; 1.0 = identical structure, 0.0 = uncorrelated.

    A perfectly flat grid has no structure to correlate: two identical flats score 1.0, a flat
    against anything else scores 0.0 (it carries no evidence of a match).
    """
    count = len(first)
    mean_first = sum(first) / count
    mean_second = sum(second) / count
    numerator = 0.0
    variance_first = 0.0
    variance_second = 0.0
    for value_first, value_second in zip(first, second):
        delta_first = value_first - mean_first
        delta_second = value_second - mean_second
        numerator += delta_first * delta_second
        variance_first += delta_first * delta_first
        variance_second += delta_second * delta_second
    if variance_first <= 0 or variance_second <= 0:
        return 1.0 if first == second else 0.0
    return numerator / ((variance_first * variance_second) ** 0.5)


def identity_score(postview_path: str | Path, full_res_path: str | Path) -> dict:
    """Content fingerprint of a candidate full-res file against this capture's verified postview.

    Returns {"ncc", "phash_distance", "postview_size", "full_res_size", "aspect_match"}. Pure
    measurement — no thresholds are applied here (see `verify_identity`).
    """
    with open_rgb(postview_path) as postview:
        postview_size = postview.size
    with open_rgb(full_res_path) as full_res:
        full_res_size = full_res.size

    height = max(1, round(_WORK_WIDTH * postview_size[1] / postview_size[0]))
    work_size = (_WORK_WIDTH, height)
    ncc = _normalized_cross_correlation(
        _resized_luma(postview_path, work_size), _resized_luma(full_res_path, work_size))
    phash_distance = (_dhash(postview_path) ^ _dhash(full_res_path)).bit_count()

    postview_aspect = postview_size[0] / postview_size[1]
    full_res_aspect = full_res_size[0] / full_res_size[1]
    aspect_match = abs(postview_aspect - full_res_aspect) / postview_aspect <= ASPECT_TOL

    return {
        "ncc": round(ncc, 4),
        "phash_distance": phash_distance,
        "postview_size": list(postview_size),
        "full_res_size": list(full_res_size),
        "aspect_match": aspect_match,
    }


def verify_identity(postview_path: str | Path, full_res_path: str | Path, *,
                    created_time: float | None = None, shutter_time: float | None = None) -> dict:
    """Decide whether `full_res_path` is THIS capture. Passes only when the content matches AND
    (when both timestamps are given) the card createdTime is within [-2 s, +30 s] of the shutter.

    Returns {"verified", "reason", "identity_basis", "time_delta_s", **identity_score}. The basis is
    ``"content+time"`` when a timestamp was checked, else ``"content_only"`` — a weaker claim, since
    a near-identical earlier frame of the same static scene would pass content alone.
    """
    scores = identity_score(postview_path, full_res_path)
    ncc_ok = scores["ncc"] >= NCC_MIN
    phash_ok = scores["phash_distance"] <= PHASH_MAX
    aspect_ok = scores["aspect_match"]
    content_ok = ncc_ok and phash_ok and aspect_ok

    have_time = created_time is not None and shutter_time is not None
    basis = "content+time" if have_time else "content_only"
    time_delta = (created_time - shutter_time) if have_time else None
    time_ok = True if not have_time else (CREATED_MIN_S <= time_delta <= CREATED_MAX_S)

    verified = bool(content_ok and time_ok)

    failures = []
    if not ncc_ok:
        failures.append(f"ncc {scores['ncc']} < {NCC_MIN}")
    if not phash_ok:
        failures.append(f"phash_distance {scores['phash_distance']} > {PHASH_MAX}")
    if not aspect_ok:
        failures.append("aspect ratios disagree > 2%")
    if have_time and not time_ok:
        failures.append(f"createdTime {time_delta:+.2f}s outside [{CREATED_MIN_S}, {CREATED_MAX_S}]s window")
    if verified:
        reason = (f"identity confirmed ({basis}): ncc={scores['ncc']}, "
                  f"phash_distance={scores['phash_distance']}"
                  + (f", createdTime {time_delta:+.2f}s after shutter" if have_time else ""))
    else:
        reason = "; ".join(failures) or "identity not confirmed"

    return {"verified": verified, "reason": reason, "identity_basis": basis,
            "time_delta_s": time_delta, **scores}


def _safe_unlink(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass  # a candidate we could not delete is not worth failing the capture over


def pull_full_res_for_capture(cam, postview_path: str | Path, work_dir: str | Path, *,
                              shutter_time: float, count: int = 3) -> dict:
    """Transfer the newest `count` full-res JPEGs, keep the FIRST that proves it is this capture,
    delete the rest, and always resume Remote Shooting before returning.

    `transfer_latest_full_res` DROPS the live session (it switches the body to Contents Transfer),
    so `cam.begin_session()` is re-run in a `finally` no matter what — recorded as `session_resumed`.

    Returns, on a match:
        {"status": "verified", "path", "receipt", "identity", "identity_basis",
         "candidates_considered", "session_resumed"}
    and otherwise:
        {"status": "unverified", "reason", "candidates_considered", "session_resumed"}
    The receipt matches `stack_session.frame_receipt` (path, bytes, width, height, sha256).
    """
    session_resumed = False
    candidates: list[str] = []
    result: dict
    try:
        # Real signature: transfer_latest_full_res(save_dir, count=1, prefer_raw=False) -> list[str],
        # newest-first, JPEG-only (RAW is refused downstream). Never request RAW.
        candidates = list(cam.transfer_latest_full_res(str(work_dir), count=count))
        chosen = None
        chosen_identity = None
        last_reason = "no full-res candidates were transferred"
        for candidate in candidates:  # newest-first
            try:
                # createdTime is not surfaced by transfer_latest_full_res (it returns paths only),
                # so today this proves identity by content only. When a future transport carries the
                # card createdTime, thread it in here to upgrade the basis to content+time.
                identity = verify_identity(postview_path, candidate,
                                           created_time=None, shutter_time=shutter_time)
            except Exception as exc:  # noqa: BLE001 - a broken candidate is simply not a match
                last_reason = f"identity check failed on {Path(candidate).name}: {exc}"
                continue
            last_reason = identity["reason"]
            if identity["verified"]:
                chosen = candidate
                chosen_identity = identity
                break

        if chosen is not None:
            for candidate in candidates:
                if candidate != chosen:
                    _safe_unlink(candidate)
            result = {
                "status": "verified",
                "path": chosen,
                "receipt": frame_receipt(chosen),
                "identity": chosen_identity,
                "identity_basis": chosen_identity["identity_basis"],
                "candidates_considered": len(candidates),
            }
        else:
            for candidate in candidates:
                _safe_unlink(candidate)
            result = {"status": "unverified", "reason": last_reason,
                      "candidates_considered": len(candidates)}
    except Exception as exc:  # noqa: BLE001 - a failed transfer must never crash the shoot
        for candidate in candidates:
            _safe_unlink(candidate)
        result = {"status": "unverified", "reason": f"full-res transfer failed: {exc}",
                  "candidates_considered": len(candidates)}
    finally:
        try:
            cam.begin_session()
            session_resumed = True
        except Exception:  # noqa: BLE001 - report the failure to resume rather than masking it
            session_resumed = False

    result["session_resumed"] = session_resumed
    return result
