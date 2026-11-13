"""Calibrated, read-only Linux IIO motion adapter and deterministic trace analysis.

I2C sensors must already have a kernel IIO driver configured. This module does
not probe addresses, write registers, load drivers, or claim camera calibration.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import math
from pathlib import Path
import time
import threading
from typing import Iterable


class IMUUnavailable(RuntimeError):
    pass


def _vector(value) -> tuple[float, float, float]:
    if len(value) != 3 or any(not math.isfinite(float(v)) for v in value):
        raise ValueError("Expected three finite axis values")
    return tuple(float(v) for v in value)


@dataclass(frozen=True)
class Calibration:
    sensor_name: str
    gyro_bias_rad_s: tuple[float, float, float]
    sensor_to_camera: tuple[tuple[float, float, float], ...]
    measured_at_unix: float
    rig_id: str

    def __post_init__(self):
        object.__setattr__(self, "gyro_bias_rad_s", _vector(self.gyro_bias_rad_s))
        rows = tuple(_vector(row) for row in self.sensor_to_camera)
        object.__setattr__(self, "sensor_to_camera", rows)
        if len(rows) != 3:
            raise ValueError("Rotation must be 3x3")
        for i in range(3):
            for j in range(3):
                if abs(sum(rows[i][k] * rows[j][k] for k in range(3)) - (i == j)) > 1e-5:
                    raise ValueError("Rotation must be orthonormal")
        determinant = sum(rows[0][i] * (rows[1][(i + 1) % 3] * rows[2][(i + 2) % 3] -
                                       rows[1][(i + 2) % 3] * rows[2][(i + 1) % 3]) for i in range(3))
        if abs(determinant - 1) > 1e-5:
            raise ValueError("Rotation must preserve handedness")
        if not self.sensor_name or not self.rig_id or not math.isfinite(self.measured_at_unix) or self.measured_at_unix <= 0:
            raise ValueError("Calibration requires sensor, rig and measured timestamp")

    def rotate(self, vector):
        return tuple(sum(row[k] * vector[k] for k in range(3)) for row in self.sensor_to_camera)


@dataclass(frozen=True)
class MotionSample:
    monotonic_ns: int
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float] | None = None
    source: str = "recorded-fixture"

    def __post_init__(self):
        if type(self.monotonic_ns) is not int or self.monotonic_ns < 0:
            raise ValueError("Invalid monotonic timestamp")
        _vector(self.gyro_rad_s)
        if self.accel_m_s2 is not None:
            _vector(self.accel_m_s2)


class LinuxIIOIMU:
    """One outstanding read maximum, caller timeout <=1s; no automatic retries.

    Kernel sysfs reads may stall. A timed-out reader is quarantined and cannot
    spawn more reads. Python cannot kill a stuck kernel read; service supervision
    is the recovery boundary. Timestamps describe host acquisition, not sensor sync.
    """
    def __init__(self, device: Path | str, calibration: Calibration | None,
                 rig_id: str, timeout: float = 0.2):
        if not 0 < timeout <= 1:
            raise ValueError("Read timeout must be in (0, 1] seconds")
        self.device = Path(device)
        self.calibration, self.rig_id, self.timeout = calibration, rig_id, timeout
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="imu-read")
        self._failed = False
        self._read_lock = threading.Lock()

    def _read_text(self, name):
        with (self.device / name).open() as stream:
            value = stream.read(129)
        if len(value) > 128:
            raise ValueError("Oversized IIO attribute")
        return value.strip()

    def _number(self, name):
        value = float(self._read_text(name))
        if not math.isfinite(value):
            raise ValueError("Nonfinite IIO value")
        return value

    def _axes(self, kind):
        values = []
        for axis in "xyz":
            prefix = f"in_{kind}_{axis}"
            scale_file = prefix + "_scale" if (self.device / (prefix + "_scale")).exists() else f"in_{kind}_scale"
            offset_file = prefix + "_offset" if (self.device / (prefix + "_offset")).exists() else f"in_{kind}_offset"
            offset = self._number(offset_file) if (self.device / offset_file).exists() else 0
            scale = self._number(scale_file)
            if scale <= 0:
                raise ValueError("Invalid IIO scale")
            values.append((self._number(prefix + "_raw") + offset) * scale)
        return tuple(values)

    def _read(self):
        calibration = self.calibration
        if calibration is None or calibration.rig_id != self.rig_id:
            raise IMUUnavailable("Measured calibration for the mounted rig is required")
        if self._read_text("name") != calibration.sensor_name:
            raise IMUUnavailable("Sensor identity differs from calibration")
        gyro = self._axes("anglvel")
        corrected = tuple(gyro[i] - calibration.gyro_bias_rad_s[i] for i in range(3))
        accel = self._axes("accel") if (self.device / "in_accel_x_raw").exists() else None
        return MotionSample(time.monotonic_ns(), calibration.rotate(corrected),
                            calibration.rotate(accel) if accel is not None else None,
                            "linux-iio-host-timestamp")

    def read(self) -> MotionSample:
        if self._failed:
            raise IMUUnavailable("IMU read quarantined after failure; inspect the device")
        if not self._read_lock.acquire(blocking=False):
            raise IMUUnavailable("An IMU read is already pending")
        try:
            future = self._executor.submit(self._read)
            try:
                return future.result(timeout=self.timeout)
            except (TimeoutError, OSError, ValueError, IMUUnavailable) as exc:
                self._failed = True
                raise IMUUnavailable(f"IMU unavailable: {type(exc).__name__}") from exc
        finally:
            self._read_lock.release()

    def close(self):
        self._failed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


def analyze_stability(samples: Iterable[MotionSample], *, maximum_gap_s: float = 0.1) -> dict:
    """Angular RMS and gravity-relative vibration, never an exposure recommendation.

    Gravity is the trace mean in camera coordinates, so a stationary tilted rig
    is stable. Rotation changes gravity direction and is intentionally retained
    as motion. This is not translational integration or inertial stabilization.
    """
    if not math.isfinite(maximum_gap_s) or not 0 < maximum_gap_s <= 1:
        raise ValueError("Invalid maximum sample gap")
    trace = []
    for sample in samples:
        trace.append(sample)
        if len(trace) > 10000:
            raise ValueError("At most 10000 samples per analysis")
    if len(trace) < 2:
        raise ValueError("At least two timestamped samples required")
    gaps = [(b.monotonic_ns - a.monotonic_ns) / 1e9 for a, b in zip(trace, trace[1:])]
    if any(gap <= 0 or gap > maximum_gap_s for gap in gaps):
        raise ValueError("Nonmonotonic or discontinuous samples")
    squared = [sum(v*v for v in item.gyro_rad_s) for item in trace]
    accel = [item.accel_m_s2 for item in trace if item.accel_m_s2 is not None]
    gravity = tuple(sum(v[i] for v in accel) / len(accel) for i in range(3)) if len(accel) == len(trace) else None
    vibration = (math.sqrt(sum(sum((v[i] - gravity[i])**2 for i in range(3)) for v in accel) / len(accel))
                 if gravity is not None else None)
    return {"sample_count": len(trace), "duration_s": sum(gaps),
            "angular_rms_rad_s": math.sqrt(sum(squared) / len(squared)),
            "angular_peak_rad_s": math.sqrt(max(squared)),
            "gravity_camera_m_s2": gravity, "vibration_rms_m_s2": vibration,
            "source": sorted(set(item.source for item in trace)),
            "camera_exposure_factor_verified": False}


def load_recorded_trace(path: Path | str) -> list[MotionSample]:
    """Load bounded offline JSONL samples; file evidence never becomes live proof."""
    import json
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Trace exceeds 2 MiB")
    samples = []
    with path.open() as stream:
        for line in stream:
            if len(line) > 8192 or len(samples) >= 10000:
                raise ValueError("Trace exceeds sample or line bound")
            if not line.strip():
                continue
            row = json.loads(line)
            samples.append(MotionSample(row["monotonic_ns"], _vector(row["gyro_rad_s"]),
                _vector(row["accel_m_s2"]) if row.get("accel_m_s2") is not None else None,
                "recorded-file-unverified"))
    return samples


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Analyze an offline IMU trace; no sensor access")
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze_stability(load_recorded_trace(args.trace)), indent=2))
