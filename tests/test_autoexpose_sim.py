"""Hardware-free verification of the auto_expose closed loop.

A FakeCamera models a real scene: each shutter step changes the brightness of the JPEG it
"captures" the way real exposure does (mean luminance ~ exposure^0.45, doubling per 3 one-third
stops). auto_expose runs through its real measure -> compute-stops -> step -> re-shoot logic; we
assert it actually drives a badly-underexposed scene to the target. This exercises everything
except the USB I/O.

Run: ./.venv/bin/python tests/test_autoexpose_sim.py
"""

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from sonyremote.autoexpose import auto_expose

# A realistic 1/3-stop shutter ladder (slow -> fast), like the A7 II reports.
SHUTTERS = [
    "30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "3.2", "2.5", "2", "1.6", "1.3",
    "1", "0.8", "0.6", "0.5", "0.4", "1/3", "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15",
    "1/20", "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200",
    "1/250", "1/320", "1/400", "1/500", "1/640", "1/800", "1/1000",
]
ISOS = ["Auto ISO", "100", "125", "160", "200", "250", "320", "400", "500", "640", "800",
        "1000", "1250", "1600", "2000", "2500", "3200", "4000", "5000", "6400"]
# One shot directory per process: a fixed name under gettempdir() is shared with every concurrent
# run, whose fake shots (another brightness) would then feed this exposure loop.
_SHOT_DIR = Path(tempfile.mkdtemp(prefix="fakeshots_"))
atexit.register(shutil.rmtree, _SHOT_DIR, ignore_errors=True)


class FakeCamera:
    """A scene + camera simulator: brightness responds to the shutter like the real thing."""

    def __init__(self, start_shutter: str, base_mean: float = 21.0, scene_gamma: float = 0.45):
        self.index = SHUTTERS.index(start_shutter)
        self._ref = self.index          # reference where mean == base_mean
        self._base = base_mean
        self._gamma = scene_gamma
        self.iso = "100"

    def shooting_mode(self) -> str:
        return "M"

    def get_settings(self) -> dict:
        return {"iso": self.iso, "aperture": "f/2.8", "shutter": SHUTTERS[self.index],
                "white_balance": "Auto", "exposure_comp": "0", "focus_mode": "Manual"}

    def list_options(self, key: str) -> list:
        return {"shutter": list(SHUTTERS), "iso": list(ISOS), "exposure_comp": ["-3", "0", "3"]}.get(key, [])

    def set_setting(self, key: str, value: str) -> str:
        if key == "shutter":
            self.index = SHUTTERS.index(str(value))
            return SHUTTERS[self.index]
        if key == "iso":
            self.iso = str(value)
        return str(value)

    def _mean(self) -> int:
        # Slower shutter (lower index) = more light. 3 index steps = 1 stop = 2x exposure.
        exposure = 2 ** ((self._ref - self.index) / 3.0)
        return int(max(0, min(255, round(self._base * (exposure ** self._gamma)))))

    def capture(self, download: bool = True, dest_dir=None) -> Path:
        value = self._mean()
        path = _SHOT_DIR / f"fakeshot_{self.index}.jpg"
        Image.new("RGB", (80, 60), (value, value, value)).save(path, "JPEG", quality=90)
        return path


def main() -> int:
    cam = FakeCamera(start_shutter="1/100", base_mean=21.0)  # starts ~5 stops underexposed
    report = auto_expose(cam, target_mean=112.0, tolerance=14.0)

    start = report["iterations"][0]["mean"]
    final = report["final_mean"]
    ok = report["ok"] and abs(final - 112.0) <= 14.0
    print(f"start mean {start} -> final mean {final} in {len(report['iterations'])} shots; "
          f"final shutter {report['final_settings']['shutter']}")
    for step in report["iterations"]:
        print(f"  shot {step['iter']}: mean {step['mean']}"
              + (f" -> {step.get('correction', '')}" if step.get("correction") else ""))
    print(f"RESULT: {'PASS' if ok else 'FAIL'} — converged={report['ok']}, note={report['note']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
