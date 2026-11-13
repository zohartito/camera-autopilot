"""Network-mocked tests for harvest_wikimedia_broad: recursion, dedup-vs-QI, all-four filter,
depth cap, resume idempotency. No network — `_get` is replaced with a canned category tree."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest_wikimedia_broad as hb

_ORIG = {k: getattr(hb, k) for k in ("_get", "QI_OUT", "SEED_CATEGORIES", "MAX_DEPTH")}


def _reset() -> None:
    for key, value in _ORIG.items():
        setattr(hb, key, value)


def _file_page(title, iso="400", fnum="28/10", shutter="1/200", focal="50/1",
               license_name="CC BY-SA 4.0"):
    meta = []
    for name, value in (("ISOSpeedRatings", iso), ("FNumber", fnum),
                        ("ExposureTime", shutter), ("FocalLength", focal)):
        if value is not None:
            meta.append({"name": name, "value": value})
    ext = {"LicenseShortName": {"value": license_name}} if license_name is not None else {}
    return {"title": title, "imageinfo": [{"metadata": meta, "extmetadata": ext,
                                           "url": f"https://x/{title}"}]}


def _subcat_page(title):
    return {"title": title}  # a Category: member (no imageinfo)


def _make_get(tree):
    """tree: {gcmtitle: [pages]} — returns all members in one page (no continuation)."""
    def fake_get(params):
        pages = tree.get(params["gcmtitle"], [])
        return {"query": {"pages": {str(i): p for i, p in enumerate(pages)}}}
    return fake_get


def _harvest(tmp, tree, qi_titles=None, seeds=("RootA",)):
    out = Path(tmp) / "broad.csv"
    qi = Path(tmp) / "qi.csv"
    with qi.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=hb.FIELDNAMES)
        writer.writeheader()
        for title in (qi_titles or []):
            writer.writerow({**{k: "" for k in hb.FIELDNAMES}, "title": title})
    hb.QI_OUT = qi
    hb.SEED_CATEGORIES = list(seeds)
    hb._get = _make_get(tree)
    total = hb.harvest(max_pages=1000, out=out)
    rows = list(csv.DictReader(out.open(newline=""))) if out.exists() else []
    return total, rows, out


_TREE = {
    "Category:RootA": [
        _subcat_page("Category:RootA-Sub"),
        _file_page("File:Good.jpg"),
        _file_page("File:Already.jpg"),          # also in the QI corpus -> must be skipped
        _file_page("File:NC.jpg", license_name="CC BY-NC 4.0"),
    ],
    "Category:RootA-Sub": [
        _file_page("File:Deep.jpg"),             # only reachable by recursing into the subcat
        _file_page("File:NoISO.jpg", iso=None),  # missing a setting -> dropped by the all-four filter
    ],
}


def test_recursion_collects_subcategory_files():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        total, rows, _ = _harvest(tmp, _TREE, qi_titles=["File:Already.jpg"])
        titles = {r["title"] for r in rows}
        assert "File:Deep.jpg" in titles, "recursion failed to reach the subcategory file"
        assert "File:Good.jpg" in titles
        assert total == len(rows) == 2, f"expected 2 rows, got {total}"


def test_dedup_against_qi_corpus():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        _, rows, _ = _harvest(tmp, _TREE, qi_titles=["File:Already.jpg"])
        assert "File:Already.jpg" not in {r["title"] for r in rows}, "QI-dup title was not skipped"


def test_all_four_settings_filter():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        _, rows, _ = _harvest(tmp, _TREE)
        assert "File:NoISO.jpg" not in {r["title"] for r in rows}, "incomplete-EXIF file was not dropped"


def test_license_filter_blocks_noncommercial_and_unknown():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        _, rows, _ = _harvest(tmp, {
            "Category:RootA": [
                _file_page("File:OK.jpg", license_name="CC0"),
                _file_page("File:NC.jpg", license_name="CC BY-NC 4.0"),
                _file_page("File:Unknown.jpg", license_name=None),
            ],
        })
        titles = {r["title"] for r in rows}
        assert "File:OK.jpg" in titles, titles
        assert "File:NC.jpg" not in titles, titles
        assert "File:Unknown.jpg" not in titles, titles


def test_depth_cap_blocks_recursion():
    _reset()
    hb.MAX_DEPTH = 0  # seeds are depth 0; their subcats are depth 1 > cap -> not recursed
    with tempfile.TemporaryDirectory() as tmp:
        _, rows, _ = _harvest(tmp, _TREE)
        assert "File:Deep.jpg" not in {r["title"] for r in rows}, "depth cap did not block recursion"
        assert "File:Good.jpg" in {r["title"] for r in rows}, "depth-0 files should still be harvested"


def test_resume_is_idempotent_and_persists_state():
    _reset()
    with tempfile.TemporaryDirectory() as tmp:
        total1, rows1, out = _harvest(tmp, _TREE)
        state = json.loads(hb._state_path(out).read_text())
        assert "Category:RootA" in state["visited"], "seed category not recorded in resume state"
        # Re-run: the tree is exhausted, so a second pass must add NO new rows. (harvest returns the
        # cumulative row count, like the QI harvester, so total is unchanged — not zero.)
        hb._get = _make_get(_TREE)
        total2 = hb.harvest(max_pages=1000, out=out)
        rows2 = list(csv.DictReader(out.open(newline="")))
        assert total2 == total1 and len(rows2) == len(rows1) == total1, "resume re-harvested exhausted tree"


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc}")
            failed += 1
    if failed:
        print(f"{failed}/{len(tests)} wikimedia-broad tests FAILED")
        return 1
    print(f"ALL wikimedia-broad harvest tests PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
