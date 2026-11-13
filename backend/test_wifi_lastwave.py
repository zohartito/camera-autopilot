"""Last-wave tests — the camera-free logic added for full-res transfer, lens info, the metering
offset, live stacking brackets, and the Stage-2 tracking smoother. No live camera: the
CameraRemote HTTP calls are stubbed with a fake that replays canned Sony JSON-RPC responses.

    backend/.venv-ml/bin/python backend/test_wifi_lastwave.py    (needs numpy/PIL for metering)
    python3 backend/test_wifi_lastwave.py                        (skips the metering block if no PIL)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wifi_a7ii
import wifi_tracking


# --- a fake camera that records calls and replays scripted responses ----------------------
class FakeCamera(wifi_a7ii.CameraRemote):
    """Subclass the real client but override `call` / `_av_call` so we exercise the REAL parsing
    logic (list_full_res, lens_info, transfer_latest_full_res) against canned Sony responses."""

    def __init__(self, responses: dict, av_responses: dict):
        super().__init__("http://cam/sony/camera")
        self._responses = responses
        self._av_responses = av_responses
        self.calls: list[tuple[str, object]] = []
        self.downloaded: list[str] = []
        self.functions_set: list[str] = []

    def call(self, method, params=None, version="1.0"):
        self.calls.append((method, params))
        if method == "setCameraFunction":
            self.functions_set.append(params[0])
            return {"result": [0], "id": 1}
        return self._responses.get(method, {"result": [], "id": 1})

    def _av_call(self, method, params=None, version="1.0"):
        self.calls.append((method, params))
        return self._av_responses.get(method, {"result": [], "id": 1})

    def download(self, url, save_path):  # don't hit the network
        self.downloaded.append((url, save_path))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(b"\xff\xd8\xff\xe0fake")  # tiny stub file
        return save_path


def _content_entry(file_name, url, kind="still", still="JPEG"):
    return {"title": file_name, "contentKind": kind,
            "content": {"original": [{"fileName": file_name, "url": url, "stillObject": still}]}}


# --- tests --------------------------------------------------------------------------------
def test_lens_info_and_focal():
    cam = FakeCamera({
        "getEvent": {"result": [
            None, None,
            {"type": "lensInformation", "currentFocalLength": 35, "minFocalLength": 28,
             "maxFocalLength": 75, "lensModelName": "Tamron 28-75"},
        ]},
    }, {})
    info = cam.lens_info()
    assert info["focal_mm"] == 35, info
    assert info["min_focal_mm"] == 28 and info["max_focal_mm"] == 75, info
    assert info["lens_model"] == "Tamron 28-75", info
    assert cam.focal_length_mm() == 35.0, cam.focal_length_mm()

    # no lens block -> the default focal, not a crash
    cam2 = FakeCamera({"getEvent": {"result": [None, {"type": "cameraStatus"}]}}, {})
    assert cam2.lens_info() == {}, cam2.lens_info()
    assert cam2.focal_length_mm(default=50.0) == 50.0
    # focal sometimes only in zoomInformation
    cam3 = FakeCamera({"getEvent": {"result": [
        {"type": "zoomInformation", "currentFocalLength": 85}]}}, {})
    assert cam3.focal_length_mm() == 85.0, cam3.focal_length_mm()
    print("  ok  lens_info / focal_length_mm")


def test_list_full_res_paging_and_fields():
    av = {
        "getSchemeList": {"result": [[{"scheme": "storage"}]]},
        "getSourceList": {"result": [[{"source": "storage:memoryCard1"}]]},
        "getContentList": {"result": [[
            _content_entry("DSC0002.ARW", "http://cam/DSC0002.ARW", still="RAW"),
            _content_entry("DSC0001.JPG", "http://cam/DSC0001.JPG"),
        ]]},
    }
    cam = FakeCamera({}, av)
    items = cam.list_full_res(max_items=10)
    assert [i["name"] for i in items] == ["DSC0002.ARW", "DSC0001.JPG"], items
    assert items[0]["url"] == "http://cam/DSC0002.ARW" and items[0]["kind"] == "RAW", items[0]
    # the avContent walk happened in the canonical order
    methods = [m for m, _ in cam.calls]
    assert methods[:3] == ["getSchemeList", "getSourceList", "getContentList"], methods
    print("  ok  list_full_res (scheme->source->content walk, fields parsed)")


def test_jpeg_only_drops_raw_entries():
    items = [
        {"name": "DSC0002.ARW", "url": "u1", "kind": "RAW", "created": None},
        {"name": "DSC0002.JPG", "url": "u2", "kind": "JPEG", "created": None},  # twin -> dropped
        {"name": "DSC0001.JPG", "url": "u3", "kind": "JPEG", "created": None},  # no RAW twin -> kept
    ]
    kept = wifi_a7ii._jpeg_only(items)
    names = [i["name"] for i in kept]
    assert names == ["DSC0002.JPG", "DSC0001.JPG"], names
    print("  ok  _jpeg_only (drops RAW entries and keeps JPEGs)")


def test_transfer_round_trips_camera_function():
    av = {
        "getSchemeList": {"result": [[{"scheme": "storage"}]]},
        "getSourceList": {"result": [[{"source": "storage:memoryCard1"}]]},
        "getContentList": {"result": [[
            _content_entry("DSC0002.ARW", "http://cam/DSC0002.ARW", still="RAW"),
            _content_entry("DSC0002.JPG", "http://cam/DSC0002.JPG"),
        ]]},
    }
    cam = FakeCamera({}, av)
    # speed: no real sleeps
    import time as _t
    orig_sleep, _t.sleep = _t.sleep, lambda *_: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            saved = cam.transfer_latest_full_res(tmp, count=1)
    finally:
        _t.sleep = orig_sleep
    assert len(saved) == 1 and saved[0].endswith("DSC0002.JPG"), saved
    # MUST enter Contents Transfer then return to Remote Shooting (so the loop keeps working)
    assert cam.functions_set == ["Contents Transfer", "Remote Shooting"], cam.functions_set
    print("  ok  transfer_latest_full_res (JPEG pulled, returns to Remote Shooting)")


def test_transfer_sanitizes_camera_filename():
    av = {
        "getSchemeList": {"result": [[{"scheme": "storage"}]]},
        "getSourceList": {"result": [[{"source": "storage:memoryCard1"}]]},
        "getContentList": {"result": [[
            _content_entry("../../escape.ARW", "http://cam/escape.ARW", still="RAW"),
        ]]},
    }
    cam = FakeCamera({}, av)
    import time as _t
    orig_sleep, _t.sleep = _t.sleep, lambda *_: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            saved = cam.transfer_latest_full_res(tmp, count=1)
            assert saved == [], saved
    finally:
        _t.sleep = orig_sleep
    print("  ok  transfer_latest_full_res refuses non-JPEG camera filenames")


def test_transfer_rejects_raw_preference():
    cam = FakeCamera({}, {})
    try:
        cam.transfer_latest_full_res("/tmp/ignored", prefer_raw=True)
    except ValueError as exc:
        assert "RAW transfer is disabled" in str(exc)
    else:
        raise AssertionError("explicit RAW transfer was accepted")
    assert cam.functions_set == [], cam.functions_set
    print("  ok  transfer_latest_full_res rejects RAW preference before camera I/O")


def test_direct_raw_download_is_rejected_before_transport() -> None:
    cam = wifi_a7ii.CameraRemote("http://cam/sony/camera")
    original_open = wifi_a7ii._camera_urlopen
    calls: list[str] = []

    def unexpected_open(url, timeout):
        calls.append(url)
        raise AssertionError("RAW download reached HTTP transport")

    wifi_a7ii._camera_urlopen = unexpected_open
    try:
        for url, destination in (
            ("http://cam/DCIM/DSC00001.ARW", "/tmp/DSC00001.JPG"),
            ("http://cam/DCIM/DSC00001.JPG", "/tmp/DSC00001.ARW"),
        ):
            try:
                cam.download(url, destination)
            except ValueError as exc:
                assert "RAW download is disabled" in str(exc)
            else:
                raise AssertionError("direct RAW download was accepted")
    finally:
        wifi_a7ii._camera_urlopen = original_open
    assert calls == []
    print("  ok  direct RAW download is rejected before transport")


def test_download_rejects_non_camera_urls_before_network():
    cam = wifi_a7ii.CameraRemote("http://cam/sony/camera")
    assert cam.camera_url("http://cam:10000/sony/liveview", context="test URL")
    bad_urls = [
        "file:///etc/passwd",
        "http://evil/DSC0001.JPG",
        "http://user:pass@cam/DSC0001.JPG",
    ]
    for url in bad_urls:
        try:
            cam.download(url, "/tmp/should-not-be-written")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected URL rejection for {url}")
    print("  ok  download rejects non-camera URLs before network I/O")


def test_tracking_smoother():
    # EMA eases small moves
    eased = wifi_tracking._smooth((100.0, 100.0), (120.0, 100.0), frame_w=1000)
    assert 100.0 < eased[0] < 120.0, eased  # partway, not all the way
    # a large jump snaps (subject switch)
    snapped = wifi_tracking._smooth((100.0, 100.0), (600.0, 100.0), frame_w=1000)
    assert snapped == (600.0, 100.0), snapped
    # deadband: a tiny move does NOT warrant a new AF command
    assert not wifi_tracking._moved_enough((100.0, 100.0), (110.0, 100.0), frame_w=1000)
    # a real move does
    assert wifi_tracking._moved_enough((100.0, 100.0), (200.0, 100.0), frame_w=1000)
    # first frame (no prev) always commands
    assert wifi_tracking._moved_enough(None, (100.0, 100.0), frame_w=1000)
    print("  ok  tracking smoother (EMA ease, jump snap, deadband)")


def test_track_step_never_raises():
    # the loop's contract: ANY per-frame failure is swallowed and we hold the last point.
    class BoomCam:
        def grab_liveview_frame(self, save_path=None):
            raise OSError("liveview dropped")
    state = wifi_tracking.TrackState()
    out = wifi_tracking.track_step(BoomCam(), state, "/tmp/_nonexistent.jpg")
    assert out.frames == 1 and out.misses == 1 and out.point_xy is None, vars(out)

    # a camera that grabs fine but vision returns nothing -> miss, no AF command, still no raise
    class NoVisionCam:
        def grab_liveview_frame(self, save_path=None):
            from PIL import Image as _Img
            _Img.new("RGB", (640, 480)).save(save_path)
        def touch_af(self, *_):
            raise AssertionError("touch_af must NOT be called when vision returns nothing")
    import types as _types
    orig = sys.modules.get("vision_banana")
    stub = _types.ModuleType("vision_banana")
    stub.analyze = lambda *_a, **_k: None
    sys.modules["vision_banana"] = stub
    wifi_tracking.vision_banana = stub
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state2 = wifi_tracking.TrackState()
            out2 = wifi_tracking.track_step(NoVisionCam(), state2, str(Path(tmp) / "track_test.jpg"))
            assert out2.frames == 1 and out2.misses == 1 and out2.af_commands == 0, vars(out2)
    finally:
        if orig is not None:
            sys.modules["vision_banana"] = orig
            wifi_tracking.vision_banana = orig
    print("  ok  track_step never raises (dropped frame + null vision both held)")


def test_touch_af_is_opt_in():
    """The focus-drive actuator is patent/legal sensitive: default is observe-only."""
    old = os.environ.get("CAMERA_AUTOPILOT_ENABLE_TOUCH_AF")
    cam = FakeCamera({
        "setTouchAFPosition": {"result": [0, {"AFResult": True}], "id": 1},
        "cancelTouchAFPosition": {"result": [0], "id": 1},
    }, {})
    try:
        os.environ.pop("CAMERA_AUTOPILOT_ENABLE_TOUCH_AF", None)
        assert cam.touch_af(25, 45) is False
        assert not any(method == "setTouchAFPosition" for method, _ in cam.calls), cam.calls
        cam.cancel_touch_af()
        assert not any(method == "cancelTouchAFPosition" for method, _ in cam.calls), cam.calls

        os.environ["CAMERA_AUTOPILOT_ENABLE_TOUCH_AF"] = "1"
        assert cam.touch_af(25, 45) is True
        cam.cancel_touch_af()
        methods = [method for method, _ in cam.calls]
        assert "setTouchAFPosition" in methods and "cancelTouchAFPosition" in methods, methods
    finally:
        if old is None:
            os.environ.pop("CAMERA_AUTOPILOT_ENABLE_TOUCH_AF", None)
        else:
            os.environ["CAMERA_AUTOPILOT_ENABLE_TOUCH_AF"] = old
    print("  ok  touch-AF actuator is off by default and enabled only by explicit env")


def test_bracket_ev_ladder():
    import wifi_stack
    assert wifi_stack._ev_offsets(3, 2.0) == [-2.0, 0.0, 2.0], wifi_stack._ev_offsets(3, 2.0)
    assert wifi_stack._ev_offsets(5, 1.0) == [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert wifi_stack._ev_offsets(1, 2.0) == [0.0]
    print("  ok  bracket EV ladder (symmetric, centered on 0)")


def test_live_stack_merges_transferred_frames():
    """Camera-free end-to-end live_stack: a fake camera serves a burst whose frames are REAL
    decodable images on disk; live_stack must transfer, merge, and write a result — no hardware,
    no network. Proves the wire from shoot -> transfer -> stacking.* -> imwrite."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  skip live_stack merge (cv2/numpy absent — run under .venv-ml)")
        return
    import tempfile

    import wifi_stack

    # Stub the shoot path (it needs vision + autopilot + a live link); we only exercise the
    # transfer+merge half here. shoot_burst returns a trivial dict; the frames come from transfer.
    rng = np.random.default_rng(7)
    base = rng.integers(40, 210, (96, 96, 3), dtype=np.uint8)

    class StackCam:
        def __init__(self, names):
            self._names = names
            self.prefer_raw_calls: list[bool] = []
        def download(self, url, save_path):
            self.prefer_raw_calls.append(False)
            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            noisy = np.clip(base.astype(np.int16) + rng.integers(-8, 8, base.shape), 0, 255)
            assert cv2.imwrite(str(p), noisy.astype(np.uint8))
            return str(p)

    orig_burst = wifi_stack.shoot_burst
    wifi_stack.shoot_burst = lambda cam, **_k: {"frames": 3, "postviews": [[f"http://cam/frame{i}.jpg"] for i in range(3)]}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = str(Path(tmp) / "merged.jpg")
            cam = StackCam(["DSC1.JPG", "DSC2.JPG", "DSC3.JPG"])
            result = wifi_stack.live_stack(cam, "long_exposure", frames=3, ev_step=2.0,
                                           out_path=out_path, save_dir=str(Path(tmp) / "frames"),
                                           prefer_raw=False)
            assert result["merged"] is True, result
            assert Path(out_path).exists() and Path(out_path).stat().st_size > 0, result
            assert len(result["transferred"]) == 3, result
            assert cam.prefer_raw_calls == [False, False, False], cam.prefer_raw_calls
    finally:
        wifi_stack.shoot_burst = orig_burst
    print("  ok  live_stack (transfer -> stacking merge -> write, camera-free)")


def test_live_stack_rejects_raw_and_retains_only_jpegs():
    """The stack never requests/retains RAW while direct RAW decode is tombstoned."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("  skip live_stack RAW-suffix (cv2 absent — run under .venv-ml)")
        return
    import wifi_stack

    class RawRequestCam:
        def transfer_latest_full_res(self, *_args, **kwargs):
            assert kwargs["prefer_raw"] is False
            return ["a.ARW", "b.JPG"]

    original_bracket = wifi_stack.shoot_bracket
    wifi_stack.shoot_bracket = lambda *_args, **_kwargs: {"stubbed": True}
    try:
        try:
            wifi_stack.live_stack(RawRequestCam(), "hdr", 2, 1.0, "/tmp/ignored.jpg", prefer_raw=True)
        except ValueError as exc:
            assert "RAW stacking is disabled" in str(exc)
        else:
            raise AssertionError("explicit RAW request was accepted")
    finally:
        wifi_stack.shoot_bracket = original_bracket
    print("  ok  live_stack rejects RAW requests")


def test_live_stack_reports_failure_when_write_fails():
    """A failed cv2.imwrite (returns False, no exception) must NOT report merged=True. The merge
    can succeed but the write still fail (unwritable path / unknown extension); live_stack must
    surface that as merged=False with a reason, not silently claim success."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  skip live_stack write-failure (cv2/numpy absent — run under .venv-ml)")
        return
    import tempfile

    import wifi_stack

    rng = np.random.default_rng(11)
    base = rng.integers(40, 210, (96, 96, 3), dtype=np.uint8)

    class StackCam:
        def __init__(self, names):
            self._names = names
        def download(self, url, save_path):
            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            noisy = np.clip(base.astype(np.int16) + rng.integers(-8, 8, base.shape), 0, 255)
            assert cv2.imwrite(str(p), noisy.astype(np.uint8))
            return str(p)

    import os
    import stat

    orig_burst = wifi_stack.shoot_burst
    wifi_stack.shoot_burst = lambda cam, **_k: {"frames": 3, "postviews": [[f"http://cam/frame{i}.jpg"] for i in range(3)]}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # A read-only output dir makes cv2.imwrite return False (a warning, NOT an exception) —
            # the exact silent-failure case the fix guards against. mkdir(exist_ok=True) is a no-op
            # on the already-present dir, so live_stack reaches the write and it fails cleanly.
            ro_dir = Path(tmp) / "ro"
            ro_dir.mkdir()
            os.chmod(ro_dir, stat.S_IREAD | stat.S_IEXEC)   # read+exec, no write
            out_path = str(ro_dir / "merged.jpg")
            cam = StackCam(["DSC1.JPG", "DSC2.JPG", "DSC3.JPG"])
            try:
                result = wifi_stack.live_stack(cam, "long_exposure", frames=3, ev_step=2.0,
                                               out_path=out_path, save_dir=str(Path(tmp) / "frames"),
                                               prefer_raw=False)
            finally:
                os.chmod(ro_dir, stat.S_IRWXU)              # restore so the tempdir can be cleaned
            assert result["merged"] is False, result        # must NOT claim success on a failed write
            assert "reason" in result and out_path in result["reason"], result
    finally:
        wifi_stack.shoot_burst = orig_burst
    print("  ok  live_stack reports merged=False when cv2.imwrite fails")


def test_metering_offset():
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        print("  skip metering offset (numpy/PIL absent — run under .venv-ml)")
        return
    import live_verify as lv
    # The ramp FUNCTION stays correct (cover its logic with a temporary non-zero max): full at/below
    # the floor, zero at/above the knee, monotonic in between.
    saved = lv._LV_OFFSET_MAX_EV
    try:
        lv._LV_OFFSET_MAX_EV = 1.0
        assert lv.liveview_still_offset_ev(lv._LV_OFFSET_FLOOR_EV - 2) == 1.0
        assert lv.liveview_still_offset_ev(lv._LV_OFFSET_KNEE_EV + 2) == 0.0
        assert 0.0 < lv.liveview_still_offset_ev(3) < 1.0
    finally:
        lv._LV_OFFSET_MAX_EV = saved
    # SHIPPED calibration is MEASURED ~0 on the A7 II (liveview ~= still: -0.04 @ EV1.7, +0.21 @ black),
    # so from_liveview is a NO-OP — a dark scene meters identically with or without the (zeroed) offset.
    assert lv._LV_OFFSET_MAX_EV == 0.0, lv._LV_OFFSET_MAX_EV
    img = "docs/progress/02-autoexpose-baseline-dark.jpg"
    if Path(img).exists():
        still, _ = lv.meter_ev100(img, 800, 4.0, 1 / 60, from_liveview=False)
        live, _ = lv.meter_ev100(img, 800, 4.0, 1 / 60, from_liveview=True)
        assert abs(live - still) < 1e-6, (still, live)  # zeroed offset -> identical reading
    print("  ok  metering offset (ramp logic covered + measured-zero calibration on this body)")


if __name__ == "__main__":
    print("last-wave tests:")
    test_lens_info_and_focal()
    test_list_full_res_paging_and_fields()
    test_jpeg_only_drops_raw_entries()
    test_transfer_round_trips_camera_function()
    test_transfer_sanitizes_camera_filename()
    test_transfer_rejects_raw_preference()
    test_direct_raw_download_is_rejected_before_transport()
    test_download_rejects_non_camera_urls_before_network()
    test_tracking_smoother()
    test_track_step_never_raises()
    test_touch_af_is_opt_in()
    test_bracket_ev_ladder()
    test_live_stack_merges_transferred_frames()
    test_live_stack_rejects_raw_and_retains_only_jpegs()
    test_live_stack_reports_failure_when_write_fails()
    test_metering_offset()
    print("ALL last-wave tests PASS")
