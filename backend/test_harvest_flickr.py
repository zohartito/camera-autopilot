"""Network-free tests for the Flickr taste harvester (the 2nd source).

No API key, no network: the search + EXIF JSON are canned Flickr responses, so the REAL parsing
path (_ratio / _exif_map / _parse_photo) is exercised against the shapes Flickr actually returns.
Pure stdlib — runs in the plain-python lane of run_tests.sh.

    python3 backend/test_harvest_flickr.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harvest_flickr as hf


def test_ratio_parses_rationals_and_fstops() -> None:
    assert hf._ratio("1/250") == 1.0 / 250.0
    assert hf._ratio("f/2.8") == 2.8          # the 'f/' prefix is stripped
    assert hf._ratio("100") == 100.0
    assert hf._ratio("0/0") is None           # zero denominator -> None, not a crash
    assert hf._ratio(None) is None
    assert hf._ratio("not-a-number") is None
    print("  ok  _ratio (rationals, f-stop prefix, bad input)")


def _exif_item(tag: str, value: str) -> dict:
    """One entry shaped like flickr.photos.getExif's list element (value nested under 'raw')."""
    return {"tag": tag, "raw": {"_content": value}}


def test_exif_map_flattens_flickr_shape() -> None:
    resp = {"stat": "ok", "photo": {"exif": [
        _exif_item("ISO", "400"),
        _exif_item("FNumber", "f/4.0"),
        _exif_item("ExposureTime", "1/125"),
        _exif_item("FocalLength", "50 mm"),
    ]}}
    flat = hf._exif_map(resp)
    assert flat["ISO"] == "400" and flat["FNumber"] == "f/4.0", flat
    assert flat["ExposureTime"] == "1/125" and flat["FocalLength"] == "50 mm", flat
    print("  ok  _exif_map (flattens Flickr's nested raw._content)")


def test_parse_photo_builds_settings_row() -> None:
    photo = {"id": "123", "owner": "42@N00", "title": "Golden hour", "license": "4",
             "server": "65535", "secret": "abc"}
    exif = {"ISO": "400", "FNumber": "f/4.0", "ExposureTime": "1/125",
            "FocalLength": "50", "Make": "SONY", "Model": "ILCE-7M2",
            "FocalLengthIn35mmFormat": "50"}
    row = hf._parse_photo(photo, exif)
    assert row is not None, "valid EXIF must parse"
    assert row["iso"] == 400 and row["f_number"] == 4.0, row
    assert abs(row["shutter_s"] - 1.0 / 125.0) < 1e-9 and row["focal_mm"] == 50.0, row
    assert row["model"] == "ILCE-7M2", row
    assert row["license"] == "CC BY 2.0", row            # id 4 mapped to a name
    # title carries the photo id so non-unique titles can't collapse under embed_index's dedup
    assert row["title"] == "flickr:123:Golden hour", row
    # url is the DIRECT static image, built from server/id/secret, so embed_index._fetch can
    # download the actual Flickr pixels (the page URL would be HTML, not an image)
    assert row["url"] == "https://live.staticflickr.com/65535/123_abc_b.jpg", row
    # same column set as the Wikimedia harvester so embed_index reads both
    assert set(row) == set(hf.FIELDNAMES), (set(row), set(hf.FIELDNAMES))
    print("  ok  _parse_photo (settings row, license name, id-keyed title, direct url, compatible)")


def test_parse_photo_prefers_largest_real_size_url() -> None:
    # The search now returns per-photo size URLs (extras=url_o,url_l,url_c,url_z,url_n). The row
    # builder must store the LARGEST one Flickr actually returned: url_o/url_l > url_c > url_z > url_n.
    # Here the photo carries url_l (large) + url_c (medium-800) — url_l must win.
    photo = {"id": "555", "title": "Has real sizes", "license": "4", "server": "65535",
             "secret": "abc",
             "url_l": "https://live.staticflickr.com/65535/555_abc_b.jpg",
             "url_c": "https://live.staticflickr.com/65535/555_abc_c.jpg"}
    exif = {"ISO": "400", "FNumber": "f/4.0", "ExposureTime": "1/125", "FocalLength": "50"}
    row = hf._parse_photo(photo, exif)
    assert row is not None, "valid EXIF must parse"
    assert row["url"] == "https://live.staticflickr.com/65535/555_abc_b.jpg", row  # url_l chosen
    print("  ok  _parse_photo (picks largest real size url Flickr returns: url_l > url_c)")


def test_parse_photo_falls_back_to_b_synthesis_without_size_urls() -> None:
    # A photo whose search response carried NONE of the url_* size fields (only server/secret):
    # the builder falls back to synthesizing the '_b'/1024px static URL so the row still has a url.
    photo = {"id": "777", "title": "No size urls", "license": "9", "server": "42", "secret": "zzz"}
    exif = {"ISO": "200", "FNumber": "f/2.8", "ExposureTime": "1/60", "FocalLength": "35"}
    row = hf._parse_photo(photo, exif)
    assert row is not None, "valid EXIF must parse"
    assert row["url"] == "https://live.staticflickr.com/42/777_zzz_b.jpg", row  # synthesized _b
    print("  ok  _parse_photo (falls back to _b synthesis when no url_* size fields returned)")


def test_parse_photo_converts_apex_aperture_and_shutter() -> None:
    # A camera that wrote ONLY APEX ApertureValue/ShutterSpeedValue (no FNumber/ExposureTime).
    # Av=3 -> f/sqrt(2**3)=f/2.83 ; Tv=7 -> 2**-7 = 1/128 s. Without conversion these APEX
    # numbers (3 and 7) would be stored as a nonsensical "f/3 at 7s" — the bug this guards.
    photo = {"id": "9", "title": "Apex only", "license": "9", "server": "1", "secret": "z"}
    exif = {"ISO": "200", "ApertureValue": "3", "ShutterSpeedValue": "7", "FocalLength": "35"}
    row = hf._parse_photo(photo, exif)
    assert row is not None, "APEX-only EXIF must convert + parse"
    assert abs(row["f_number"] - 2.83) < 0.01, row          # f = sqrt(2**Av)
    assert abs(row["shutter_s"] - 1.0 / 128.0) < 1e-6, row  # s = 2**(-Tv)

    # Real FNumber/ExposureTime must WIN over APEX when both are present (Av/Tv ignored).
    both = {"ISO": "200", "FNumber": "f/8", "ApertureValue": "3",
            "ExposureTime": "1/250", "ShutterSpeedValue": "7", "FocalLength": "35"}
    row2 = hf._parse_photo(photo, both)
    assert row2 is not None and row2["f_number"] == 8.0, row2
    assert abs(row2["shutter_s"] - 1.0 / 250.0) < 1e-9, row2
    print("  ok  _parse_photo (APEX Av/Tv -> f/shutter, real tags preferred)")


def test_ratio_strips_units() -> None:
    # Flickr's raw EXIF often carries units/labels; _ratio must read the leading number.
    assert hf._ratio("50 mm") == 50.0
    assert abs(hf._ratio("1/125 sec") - 1.0 / 125.0) < 1e-9
    assert hf._ratio("100 ISO") == 100.0
    assert hf._ratio("f/2.8") == 2.8
    assert hf._ratio("2.8 (rounded)") == 2.8
    assert hf._ratio("no number here") is None
    print("  ok  _ratio strips units / labels (50 mm, 1/125 sec, 100 ISO)")


def test_parse_photo_unique_title_per_photo_id() -> None:
    # Two DIFFERENT photos both titled "Untitled": the id keeps their stored titles distinct,
    # so embed_index's title dedup keeps both instead of collapsing them into one.
    exif = {"ISO": "100", "FNumber": "f/2.8", "ExposureTime": "1/60", "FocalLength": "35"}
    a = hf._parse_photo({"id": "111", "title": "Untitled", "license": "9",
                         "server": "1", "secret": "a"}, exif)
    b = hf._parse_photo({"id": "222", "title": "Untitled", "license": "9",
                         "server": "1", "secret": "b"}, exif)
    assert a["title"] == "flickr:111:Untitled" and b["title"] == "flickr:222:Untitled", (a, b)
    assert a["title"] != b["title"], "same-titled photos must not collapse on dedup"
    print("  ok  _parse_photo (photo id makes same-titled rows unique for dedup)")


def test_parse_photo_rejects_incomplete_or_insane() -> None:
    photo = {"id": "1", "owner": "x", "title": "t", "license": "9"}
    # missing shutter -> rejected
    assert hf._parse_photo(photo, {"ISO": "100", "FNumber": "2.8", "FocalLength": "35"}) is None
    # out-of-range aperture (f/0.1) -> rejected
    assert hf._parse_photo(photo, {"ISO": "100", "FNumber": "0.1", "ExposureTime": "1/60",
                                   "FocalLength": "35"}) is None
    # absurd ISO -> rejected
    assert hf._parse_photo(photo, {"ISO": "9", "FNumber": "2.8", "ExposureTime": "1/60",
                                   "FocalLength": "35"}) is None
    print("  ok  _parse_photo (rejects missing + out-of-range settings)")


def test_only_commercial_licenses_requested() -> None:
    # The harvester must request ONLY commercial-use-OK licenses (no NC/ND ids 1,2,3,6).
    requested = set(hf._LICENSES.split(","))
    assert requested == {"4", "5", "7", "8", "9", "10"}, requested
    assert not ({"1", "2", "3", "6"} & requested), "non-commercial/no-derivative license requested"
    print("  ok  license scope (commercial-use-OK only)")


def test_cursor_is_keyed_by_query() -> None:
    # The resume cursor must remember the QUERY it belongs to: a run with a different `text`
    # against the same file must NOT resume on the stored page (which would skip the new
    # query's early pages). Same query -> resume; changed query -> no match (fresh start).
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cont = Path(tmp) / "flickr_settings.page"
        hf._write_cursor(cont, "landscape", 5)
        same = hf._read_cursor(cont)
        assert same == {"query": "landscape", "page": 5}, same        # round-trips query + page
        # A different query reads back its OWN query, which the harvester compares against `text`
        # and finds != current -> starts fresh instead of jumping to page 5.
        assert hf._read_cursor(cont)["query"] != "portrait"

        # Exhausted sentinel: page=None, but still keyed to the query.
        hf._write_cursor(cont, "landscape", None)
        done = hf._read_cursor(cont)
        assert done == {"query": "landscape", "page": None}, done

        # A legacy bare-number sidecar (pre-query-tracking) reads as query=None so it never
        # matches the current query -> the run restarts cleanly rather than on the wrong page.
        cont.write_text("7")
        legacy = hf._read_cursor(cont)
        assert legacy["query"] is None, legacy
    print("  ok  resume cursor keyed + validated by query text")


def _run_all() -> int:
    tests = [
        test_ratio_parses_rationals_and_fstops,
        test_ratio_strips_units,
        test_exif_map_flattens_flickr_shape,
        test_parse_photo_builds_settings_row,
        test_parse_photo_prefers_largest_real_size_url,
        test_parse_photo_falls_back_to_b_synthesis_without_size_urls,
        test_parse_photo_converts_apex_aperture_and_shutter,
        test_parse_photo_unique_title_per_photo_id,
        test_parse_photo_rejects_incomplete_or_insane,
        test_only_commercial_licenses_requested,
        test_cursor_is_keyed_by_query,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"  FAIL {test.__name__}: {error}")
    print(f"\n{len(tests) - failures}/{len(tests)} flickr-harvest tests passed")
    return failures


if __name__ == "__main__":
    print("flickr-harvest tests:")
    raise SystemExit(_run_all())
