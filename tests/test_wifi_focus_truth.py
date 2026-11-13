"""A native AF lock does not prove that chosen-subject coordinates were applied."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import wifi_autopilot
import wifi_a7ii


class FocusCamera:
    def __init__(self, coordinate_result):
        self.coordinate_result = coordinate_result
        self.half_presses = 0
        self.cancelled = 0
        self.calls = []
    def touch_af(self, x, y): self.calls.append((x, y)); return self.coordinate_result
    def half_press(self): self.half_presses += 1
    def focus_status(self): return 'Focused'
    def cancel_half_press(self): self.cancelled += 1
    def cancel_touch_af(self): pass


ANALYSIS = {'saliency_centroid': [30, 40], 'subject_box': [20, 30, 20, 20]}


def test_generic_focus_lock_without_coordinate_confirmation_has_no_point(monkeypatch):
    monkeypatch.setattr(wifi_autopilot.time, 'sleep', lambda _: None)
    cam = FocusCamera(False)
    locked, point, status = wifi_autopilot.focus_subject(cam, ANALYSIS, 100, 100)
    assert locked is True
    assert point is None
    assert status == 'Focused'
    assert cam.half_presses == cam.cancelled == 1


def test_confirmed_coordinate_result_can_report_applied_point(monkeypatch):
    monkeypatch.setattr(wifi_autopilot.time, 'sleep', lambda _: None)
    cam = FocusCamera(True)
    locked, point, status = wifi_autopilot.focus_subject(cam, ANALYSIS, 100, 100)
    assert locked is True
    assert point == (30, 40)
    assert status == 'Focused'


def test_real_touch_af_gate_off_sends_no_coordinate_command(monkeypatch):
    monkeypatch.setattr(wifi_autopilot.time, 'sleep', lambda _: None)
    monkeypatch.delenv('CAMERA_AUTOPILOT_ENABLE_TOUCH_AF', raising=False)
    class GateCamera(FocusCamera):
        touch_af = wifi_a7ii.CameraRemote.touch_af
        def call(self, method, params=None):
            raise AssertionError('No coordinate request permitted with opt-in gate off')
    cam = GateCamera(False)
    assert wifi_autopilot.focus_subject(cam, ANALYSIS, 100, 100) == (True, None, 'Focused')
