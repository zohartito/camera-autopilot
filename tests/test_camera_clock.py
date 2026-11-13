"""The camera's clock, read from the delivered JPEG, is compared with this computer's at the shutter.

29 Sep 2026: the A7 III wrote DateTimeOriginal 2026:09:30 04:22:10 OffsetTimeOriginal +01:00 for a
capture at 19:29 PDT (about 53 min ahead, wrong zone); the A7 II was right. Report only: the
camera's clock is never changed.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import camera_clock
from test_final_wave_engine import rig  # noqa: F401 — the camera-free engine fixture

PDT_SHUTTER = datetime(2026, 9, 29, 19, 29, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_the_29_september_a7iii_clock_is_wrong_by_53_minutes_and_its_zone():
    clock = camera_clock.check("2026:09:30 04:22:10", "+01:00", PDT_SHUTTER)
    assert clock["status"] == "wrong"
    assert clock["skew_s"] == pytest.approx(53 * 60 + 10)
    assert clock["zone_mismatch"] is True
    assert clock["camera_time"] == "2026:09:30 04:22:10"
    assert clock["host_time"] == "2026-09-29T19:29:00-07:00"
    line = camera_clock.summary_line(clock)
    assert "53 min ahead" in line and "UTC+01:00" in line and "UTC-07:00" in line
    assert "set its date, time and time zone" in line


def test_the_a7ii_clock_within_seconds_is_ok_and_says_nothing():
    clock = camera_clock.check("2026:09:29 19:29:40", "-07:00", PDT_SHUTTER)
    assert (clock["status"], clock["zone_mismatch"]) == ("ok", False)
    assert camera_clock.summary_line(clock) is None


@pytest.mark.parametrize("camera_time, status", [
    ("2026:09:29 19:34:00", "ok"),      # exactly 5 min: within the limit
    ("2026:09:29 19:34:01", "wrong"),
    ("2026:09:29 19:19:00", "wrong"),   # 10 min behind
])
def test_without_an_offset_only_the_wall_clock_is_judged(camera_time, status):
    clock = camera_clock.check(camera_time, None, PDT_SHUTTER)
    assert clock["status"] == status
    assert clock["zone_mismatch"] is None
    if camera_time == "2026:09:29 19:19:00":
        assert "10 min behind" in camera_clock.summary_line(clock)
        assert "UTC" not in camera_clock.summary_line(clock)


def test_a_right_instant_in_the_wrong_zone_is_still_wrong():
    # Same instant written with the wrong zone: the photo sorts right, but local time reads wrong.
    clock = camera_clock.check("2026:09:30 02:29:00", "+00:00", PDT_SHUTTER)
    assert clock["skew_s"] == 0 and clock["zone_mismatch"] is True
    assert clock["status"] == "wrong"
    line = camera_clock.summary_line(clock)
    assert "ahead" not in line and "behind" not in line and "UTC+00:00" in line


def test_hours_of_skew_read_in_hours():
    clock = camera_clock.check("2026:09:30 00:29:00", None, PDT_SHUTTER)
    assert "5 h 0 min ahead" in camera_clock.summary_line(clock)


@pytest.mark.parametrize("camera_time", [None, "", "0000:00:00 00:00:00", "not a date"])
def test_a_missing_or_unreadable_capture_time_is_unavailable_not_wrong(camera_time):
    clock = camera_clock.check(camera_time, None, PDT_SHUTTER)
    assert clock["status"] == "unavailable" and clock["skew_s"] is None
    assert camera_clock.summary_line(clock) is None


def test_an_unparseable_offset_is_ignored_and_said():
    clock = camera_clock.check("2026:09:29 19:29:00", "PDT", PDT_SHUTTER)
    assert clock["status"] == "ok" and clock["camera_offset_ignored"]


def test_host_time_must_carry_its_zone():
    with pytest.raises(ValueError):
        camera_clock.check("2026:09:29 19:29:00", None, datetime(2026, 9, 29, 19, 29))


def _jpeg_with_time(path, stamp, offset):
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[36867] = stamp
    if offset:
        ifd[36881] = offset
    Image.new("RGB", (64, 48), (112, 112, 112)).save(path, exif=exif)


def test_exif_time_reads_the_exif_ifd(tmp_path):
    path = tmp_path / "a.jpg"
    _jpeg_with_time(path, "2026:09:30 04:22:10", "+01:00")
    with Image.open(path) as image:
        assert camera_clock.exif_time(image.getexif()) == ("2026:09:30 04:22:10", "+01:00")


def _stamp_final_frame(camera, *, ahead, offset):
    """Wrap the fake body's capture so each frame carries a capture time `ahead` of now."""
    original = camera.capture

    def capture(download=True):
        path = original(download)
        now = datetime.now().astimezone()
        zone = offset if offset is not None else now.utcoffset()
        stamp = (now + ahead).astimezone(timezone(zone)).strftime("%Y:%m:%d %H:%M:%S")
        sign = "-" if zone < timedelta(0) else "+"
        minutes = abs(int(zone.total_seconds() // 60))
        _jpeg_with_time(path, stamp, f"{sign}{minutes // 60:02d}:{minutes % 60:02d}")
        return path
    camera.capture = capture


def test_shoot_records_a_wrong_camera_clock_and_tells_the_owner(rig):
    before = dict(rig.camera.settings)
    _stamp_final_frame(rig.camera, ahead=timedelta(minutes=53), offset=None)
    shot = rig.real.shoot(None, variant_id="natural")
    clock = shot.evidence["camera_clock"]
    assert clock["status"] == "wrong" and 52 * 60 < clock["skew_s"] < 54 * 60
    assert clock["zone_mismatch"] is False
    assert shot.summary.count("The camera's clock is wrong") == 1
    assert "53 min ahead" in shot.summary
    assert not any(key in ("datetime", "date_time") for key, _ in rig.camera.setting_writes)
    assert rig.camera.settings.keys() == before.keys()


def test_shoot_flags_a_wrong_zone(rig):
    local = datetime.now().astimezone().utcoffset()
    other = timedelta(hours=1) if local != timedelta(hours=1) else timedelta(hours=2)
    _stamp_final_frame(rig.camera, ahead=timedelta(0), offset=other)
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["camera_clock"]["zone_mismatch"] is True
    assert "The camera's clock is wrong" in shot.summary


def test_shoot_with_a_right_clock_records_ok_and_adds_no_line(rig):
    _stamp_final_frame(rig.camera, ahead=timedelta(seconds=20), offset=None)
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["camera_clock"]["status"] == "ok"
    assert "clock" not in shot.summary


def test_shoot_without_a_capture_time_records_unavailable(rig):
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["camera_clock"]["status"] == "unavailable"
    assert "clock" not in shot.summary


@pytest.mark.parametrize("offset", ["+24:00", "+00:99", "-15:00", "+1:00", "Z"])
def test_an_impossible_offset_is_ignored_never_raised(offset):
    clock = camera_clock.check("2026:09:29 19:29:00", offset, PDT_SHUTTER)
    assert clock["status"] == "ok" and clock["camera_offset_ignored"]
    assert clock["zone_mismatch"] is None


def test_safe_check_never_raises():
    assert camera_clock.safe_check("2026:09:29 19:29:00", None, None)["status"] == "unavailable"
    assert camera_clock.safe_check("2026:09:29 19:29:00", None, datetime(2026, 9, 29))["status"] == "unavailable"
    assert camera_clock.safe_exif_time(object()) == (None, None)


def test_the_host_zone_is_the_one_in_force_at_the_shutter_across_dst():
    # 1 Nov 2026 01:30 happens twice in Los Angeles; the second (fold=1) is PST, UTC-08:00.
    after_change = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Los_Angeles"), fold=1)
    assert camera_clock.check("2026:11:01 01:30:20", "-08:00", after_change)["status"] == "ok"
    stale_zone = camera_clock.check("2026:11:01 01:30:20", "-07:00", after_change)
    assert stale_zone["status"] == "wrong" and stale_zone["zone_mismatch"] is True


def test_shoot_with_an_impossible_offset_keeps_the_photo_and_says_nothing(rig):
    original = rig.camera.capture

    def capture(download=True):
        path = original(download)
        stamp = datetime.now().astimezone().strftime("%Y:%m:%d %H:%M:%S")
        _jpeg_with_time(path, stamp, "+24:00")
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.ok is True
    assert shot.evidence["camera_clock"]["status"] == "ok"
    assert "clock" not in shot.summary


def test_the_jpeg_feedback_fallback_checks_the_clock_too(rig):
    from unittest.mock import patch
    _stamp_final_frame(rig.camera, ahead=timedelta(minutes=53), offset=None)
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")):
        shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["source"] == "jpeg_feedback_fallback"
    assert shot.evidence["camera_clock"]["status"] == "wrong"
    assert shot.summary.count("The camera's clock is wrong") == 1


def test_a_dst_change_during_the_capture_call_does_not_make_a_right_camera_wrong():
    los_angeles = ZoneInfo("America/Los_Angeles")
    before = datetime(2026, 11, 1, 1, 59, 58, tzinfo=los_angeles)            # PDT, UTC-07:00
    after = datetime(2026, 11, 1, 1, 0, 3, tzinfo=los_angeles, fold=1)       # PST, UTC-08:00
    with_zone = camera_clock.check("2026:11:01 01:00:00", "-08:00", before, after)
    assert (with_zone["status"], with_zone["zone_mismatch"]) == ("ok", False)
    assert with_zone["host_offset_changed_during_capture"] == "UTC-08:00"
    wall_clock = camera_clock.check("2026:11:01 01:00:00", None, before, after)
    assert wall_clock["status"] == "ok" and abs(wall_clock["skew_s"]) < 5
    assert camera_clock.check("2026:11:01 01:00:00", "+01:00", before, after)["status"] == "wrong"


@pytest.mark.parametrize("fallback", [False, True])
def test_an_unreadable_host_zone_never_fails_the_photo(rig, monkeypatch, fallback):
    from unittest.mock import patch
    monkeypatch.setattr(camera_clock, "host_now", lambda: None)
    _stamp_final_frame(rig.camera, ahead=timedelta(0), offset=None)
    if fallback:
        with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")):
            shot = rig.real.shoot(None, variant_id="natural")
    else:
        shot = rig.real.shoot(None, variant_id="natural")
        assert rig.camera.capture_count == 2
    assert shot.evidence["camera_clock"]["status"] == "unavailable"
    assert "clock" not in shot.summary


def test_host_now_never_raises(monkeypatch):
    import datetime as datetime_module

    class Broken(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            raise OverflowError("no zone")
    monkeypatch.setattr(camera_clock, "datetime", Broken)
    assert camera_clock.host_now() is None


@pytest.mark.parametrize("seconds, words", [(300.04, "5 min ahead"), (-300.04, "5 min behind")])
def test_a_skew_just_past_the_limit_still_says_how_far(seconds, words):
    host = datetime(2026, 9, 29, 19, 29, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    host = host - timedelta(seconds=seconds - round(seconds))     # sub-second host time, whole-second EXIF
    stamp = (host + timedelta(seconds=seconds)).strftime("%Y:%m:%d %H:%M:%S")
    clock = camera_clock.check(stamp, None, host)
    assert clock["status"] == "wrong" and clock["skew_exceeded"] is True
    assert words in camera_clock.summary_line(clock)
    assert "()" not in camera_clock.summary_line(clock)
