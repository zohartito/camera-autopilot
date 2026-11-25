"""CAM-04: default local sun times use the requested date's DST rules."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

import pytest
import server


@pytest.mark.parametrize("now,day", [("2026-11-15", "2027-07-01"), ("2026-07-01", "2027-01-01")])
def test_local_sun_times_follow_target_date_dst(now, day, monkeypatch):
    zone = ZoneInfo("America/Los_Angeles")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(now).replace(tzinfo=zone)
            return value.astimezone(tz) if tz else value
    with monkeypatch.context() as patch:
        patch.setenv("TZ", zone.key)
        time.tzset()
        patch.setattr(server, "datetime", Clock)
        actual = server.sun_times(39.09, -120.04, day)
        expected = server.sun_times(39.09, -120.04, day, zone.key)
    time.tzset()
    for event in ("sunrise", "sunset", "golden_hour_morning", "blue_hour_evening"):
        assert actual[event] == expected[event]


def test_local_sun_times_read_zoneinfo_when_tz_is_unset(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    # Use the host's date-aware timezone file, without assuming which zone it is.
    with open("/etc/localtime", "rb") as stream:
        zone = ZoneInfo.from_file(stream)
    result = server.sun_times(0, 0, "2027-07-01")
    assert datetime.fromisoformat(result["noon"]).utcoffset() == datetime(2027, 7, 1, tzinfo=zone).utcoffset()
