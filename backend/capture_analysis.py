"""Measured capture analysis. Unusable timing is explicit, never invented motion evidence."""
from pathlib import Path


def motion_between_files(first, second, dt_s: float, *, frame_timestamps=None) -> dict:
    """Two-frame optical-flow motion between two saved liveview frames.

    `dt_s` is the interval the caller measured around the two grabs. `frame_timestamps=(t_a, t_b)`
    is an optional cross-check: the interval is re-derived as `t_b - t_a`; when both are supplied
    and disagree (beyond a small tolerance), or the derived interval is unusable, the result is
    marked unverified with a reason rather than trusting a guessed dt.
    """
    try:
        import numpy as np
        from image_safety import open_rgb
        from motion_texture import analyze_frame_pair
        dt_measured = True
        timestamp_reason = None
        if frame_timestamps is not None:
            t_a, t_b = frame_timestamps
            derived = float(t_b) - float(t_a)
            if not 0 < derived <= 2.0:
                dt_measured = False
                timestamp_reason = "frame timestamps give an interval outside (0, 2] seconds"
            else:
                tolerance = max(0.02, 0.15 * derived)
                if abs(derived - dt_s) > tolerance:
                    dt_measured = False
                    timestamp_reason = (f"supplied interval {dt_s:.3f}s disagrees with the frame "
                                        f"timestamp interval {derived:.3f}s")
                dt_s = derived  # measured timestamps are authoritative when usable
        arrays = []
        full_width = None
        for path in (first, second):
            with open_rgb(Path(path)) as im:
                full_width = im.width
                im.thumbnail((960, 640))
                arrays.append(np.asarray(im).copy())
        result = analyze_frame_pair(*arrays, dt_s, output_width_px=full_width,
                                    dt_measured=dt_measured)
        result = {**result, "sample_interval_s": round(dt_s, 4)}
        if timestamp_reason:
            existing = result.get("reason")
            result["status"] = "estimated"
            result["outcome_verified"] = False
            result["reason"] = f"{existing}; {timestamp_reason}" if existing else timestamp_reason
        return result
    except (ImportError, ValueError, OSError) as exc:
        return {"status": "unavailable", "reason": str(exc),
                "subject_motion_floor_s": None, "sample_interval_s": round(dt_s, 4)}


def motion_evidence_for_shot(analysis) -> dict:
    """Project a two-frame optical-flow analysis into exactly the dict the capture-policy stack
    gate reads. Non-dict or degenerate input yields an honest, unverified record.

    The gate accepts motion only when `outcome_verified is True`, `status in ("measured",
    "verified")`, and `motion_fraction` is a finite float in [0, 1]; this projection preserves
    that contract faithfully instead of asserting a verification the source did not make.
    """
    source = analysis if isinstance(analysis, dict) else {}
    status = source.get("status", "unavailable")
    verified = source.get("outcome_verified") is True and status in ("measured", "verified")
    return {
        "status": status,
        "outcome_verified": bool(verified),
        "motion_fraction": source.get("motion_fraction"),
        "subject_motion_floor_s": source.get("subject_motion_floor_s"),
        "reason": source.get("reason"),
        "source": "two_frame_optical_flow",
    }


def usb_motion_unavailable(reason: str) -> dict:
    """The honest 'no motion evidence' record for still-USB capture.

    Full-resolution USB stills transfer seconds apart — far outside the two-frame model's (0, 2]s
    window — so no verified motion can be produced there. Kept here so the USB engine and the gate
    share one shape instead of inlining it.
    """
    return {"status": "unavailable", "subject_motion_floor_s": None,
            "outcome_verified": False, "reason": reason}
