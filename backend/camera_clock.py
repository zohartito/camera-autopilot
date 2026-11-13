"""Compare the camera's clock (the delivered JPEG's EXIF capture time) with this computer's.

On 2026-09-29 the A7 III wrote DateTimeOriginal 2026:09:30 04:22:10 OffsetTimeOriginal +01:00 for a
capture at 19:29 PDT: about 53 minutes ahead and in the wrong time zone. Photos then sort and
caption wrongly. This only reads EXIF and reports; it never changes the camera's clock.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MAX_SKEW_S = 5 * 60
_EXIF_IFD = 34665
_DATE_TIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _offset(text) -> timedelta | None:
    match = _OFFSET.match(str(text or "").strip())
    if not match:
        return None
    sign, hours, minutes = match.groups()
    value = timedelta(hours=int(hours), minutes=int(minutes))
    if int(minutes) >= 60 or value > timedelta(hours=14):   # real zones lie within -12:00..+14:00
        return None
    return -value if sign == "-" else value


def _format_offset(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def exif_time(exif) -> tuple[str | None, str | None]:
    """(DateTimeOriginal, OffsetTimeOriginal) from a PIL Exif, or None for each missing."""
    ifd = exif.get_ifd(_EXIF_IFD) if _EXIF_IFD in exif else exif
    stamp = ifd.get(_DATE_TIME_ORIGINAL, exif.get(_DATE_TIME_ORIGINAL))
    offset = ifd.get(_OFFSET_TIME_ORIGINAL, exif.get(_OFFSET_TIME_ORIGINAL))
    return (str(stamp).strip("\x00 ") if stamp else None,
            str(offset).strip("\x00 ") if offset else None)


def safe_exif_time(exif) -> tuple[str | None, str | None]:
    """exif_time() that never raises: a clock diagnostic must not fail a delivered photo."""
    try:
        return exif_time(exif)
    except Exception:  # noqa: BLE001 — diagnostics only
        return None, None


def host_now() -> datetime | None:
    """This computer's time in its local zone, or None if the zone cannot be read. Never raises."""
    try:
        return datetime.now().astimezone()
    except Exception:  # noqa: BLE001 — diagnostics only
        return None


def check_file(path, host_time, host_time_after=None) -> dict:
    """safe_check() on a JPEG on disk; never raises."""
    try:
        from image_safety import open_image
        with open_image(path) as image:
            fields = exif_time(image.getexif())
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        return {"status": "unavailable", "skew_s": None, "camera_time": None, "host_time": None,
                "reason": f"EXIF could not be read: {exc}"}
    return safe_check(*fields, host_time, host_time_after)


def safe_check(camera_time, camera_offset, host_time, host_time_after=None) -> dict:
    """check() that never raises: a clock diagnostic must not fail a delivered photo."""
    try:
        if host_time is None:
            raise ValueError("this computer's time at the shutter was not recorded")
        return check(camera_time, camera_offset, host_time, host_time_after)
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        return {"status": "unavailable", "skew_s": None, "camera_time": camera_time,
                "camera_offset": camera_offset, "host_time": None, "reason": f"clock check failed: {exc}"}


def check(camera_time: str | None, camera_offset: str | None, host_time: datetime,
          host_time_after: datetime | None = None) -> dict:
    """Judge the camera clock against `host_time`: this computer's time just before the capture
    call, aware and in its own local zone (`host_now()`). `host_time_after`, taken when the call
    returned, only widens the zones accepted: a daylight-saving change during the call (transport
    preparation can take seconds) must not make a right camera look wrong.

    Without an offset the camera time is local wall-clock time and is compared with the host's
    local wall-clock time. With one, both are compared as instants and the zone must match the
    host's zone at that moment. status: ok | wrong | unavailable.
    """
    host_offset = host_time.utcoffset()
    if host_offset is None:
        raise ValueError("host_time must be timezone-aware")
    offsets = [host_offset]
    after_offset = host_time_after.utcoffset() if host_time_after is not None else None
    if after_offset is not None and after_offset != host_offset:
        offsets.append(after_offset)
    evidence = {"camera_time": camera_time, "camera_offset": camera_offset,
                "host_time": host_time.isoformat(timespec="seconds"),
                "host_offset": _format_offset(host_offset), "max_skew_s": MAX_SKEW_S,
                **({"host_offset_changed_during_capture": _format_offset(after_offset)}
                   if len(offsets) > 1 else {})}
    try:
        naive = datetime.strptime(str(camera_time), "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return {**evidence, "status": "unavailable", "skew_s": None, "zone_mismatch": None,
                "reason": "the JPEG has no readable DateTimeOriginal"}
    offset = _offset(camera_offset)
    if offset is not None:
        skew = (naive.replace(tzinfo=timezone(offset)) - host_time).total_seconds()
        zone_mismatch = offset not in offsets
    else:
        # Wall-clock time in each zone in force during the call; the closer one is judged.
        skew = min(((naive - host_time.astimezone(timezone(zone)).replace(tzinfo=None)).total_seconds()
                    for zone in offsets), key=abs)
        zone_mismatch = None    # no zone written: only the wall-clock time can be judged
    skew_exceeded = abs(skew) > MAX_SKEW_S
    wrong = skew_exceeded or bool(zone_mismatch)
    return {**evidence, "status": "wrong" if wrong else "ok", "skew_s": round(skew, 1),
            "skew_exceeded": skew_exceeded, "zone_mismatch": zone_mismatch,
            **({"camera_offset_ignored": "not in +HH:MM form"} if camera_offset and offset is None else {})}


def summary_line(evidence: dict | None) -> str | None:
    """One short line for the owner when the camera clock is wrong, else None."""
    if not evidence or evidence.get("status") != "wrong":
        return None
    parts = []
    skew = evidence["skew_s"]
    if evidence.get("skew_exceeded"):
        minutes = round(abs(skew) / 60)
        amount = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
        parts.append(f"{amount} {'ahead' if skew > 0 else 'behind'}")
    if evidence.get("zone_mismatch"):
        camera_zone = _format_offset(_offset(evidence["camera_offset"]))
        parts.append(f"set to {camera_zone}, not {evidence['host_offset']}")
    return (f"The camera's clock is wrong ({'; '.join(parts)}): set its date, time and time zone "
            "in the camera menu.")
