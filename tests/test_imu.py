import math
import threading
import time
import pytest
from backend.imu import Calibration, IMUUnavailable, LinuxIIOIMU, MotionSample, analyze_stability

IDENTITY = ((1,0,0), (0,1,0), (0,0,1))


def calibration(rotation=IDENTITY):
    return Calibration('fixture-gyro', (0.1,0,0), rotation, 1_700_000_000, 'rig-A')


def sensor(tmp_path):
    for name, value in {'name': 'fixture-gyro', 'in_anglvel_scale': '.01',
                        'in_anglvel_x_raw': '10', 'in_anglvel_y_raw': '20', 'in_anglvel_z_raw': '0',
                        'in_accel_scale': '.01', 'in_accel_x_raw': '0',
                        'in_accel_y_raw': '0', 'in_accel_z_raw': '981'}.items():
        (tmp_path / name).write_text(value)


def test_calibrated_read_scales_subtracts_bias_and_rotates(tmp_path):
    sensor(tmp_path)
    reader = LinuxIIOIMU(tmp_path, calibration(((0,-1,0),(1,0,0),(0,0,1))), 'rig-A')
    try:
        sample = reader.read()
        assert sample.gyro_rad_s == pytest.approx((-0.2, 0, 0))
        assert sample.accel_m_s2 == pytest.approx((0, 0, 9.81))
        assert sample.monotonic_ns > 0
        assert sample.source == 'linux-iio-host-timestamp'
    finally: reader.close()


@pytest.mark.parametrize('failure', ['missing', 'uncalibrated', 'wrong-rig', 'wrong-sensor', 'nan'])
def test_unavailable_not_fake_stable(tmp_path, failure):
    sensor(tmp_path)
    cal, rig = calibration(), 'rig-A'
    if failure == 'missing': (tmp_path / 'in_anglvel_x_raw').unlink()
    if failure == 'uncalibrated': cal = None
    if failure == 'wrong-rig': rig = 'rig-B'
    if failure == 'wrong-sensor': (tmp_path / 'name').write_text('different')
    if failure == 'nan': (tmp_path / 'in_anglvel_scale').write_text('nan')
    reader = LinuxIIOIMU(tmp_path, cal, rig)
    try:
        with pytest.raises(IMUUnavailable): reader.read()
        with pytest.raises(IMUUnavailable, match='quarantined'): reader.read()
    finally: reader.close()


def test_bounded_read_quarantines_without_second_operation(tmp_path, monkeypatch):
    reader = LinuxIIOIMU(tmp_path, calibration(), 'rig-A', timeout=.02)
    release = threading.Event()
    calls = []
    def stalled():
        calls.append(1)
        release.wait(1)
    monkeypatch.setattr(reader, '_read', stalled)
    before = time.monotonic()
    try:
        with pytest.raises(IMUUnavailable): reader.read()
        assert time.monotonic() - before < .5
        with pytest.raises(IMUUnavailable): reader.read()
        assert calls == [1]
    finally:
        release.set()
        reader.close()


def test_stationary_tilt_has_no_fake_vibration_or_exposure_claim():
    samples = [MotionSample(i*10_000_000, (0,0,0), (9.81,0,0)) for i in range(10)]
    report = analyze_stability(samples)
    assert report['angular_rms_rad_s'] == 0
    assert report['vibration_rms_m_s2'] == pytest.approx(0)
    assert report['gravity_camera_m_s2'] == pytest.approx((9.81,0,0))
    assert report['camera_exposure_factor_verified'] is False


def test_rotating_trace_retains_motion():
    samples = [MotionSample(i*10_000_000, (0,0,1),
                           (9.81*math.sin(i/20), 0, 9.81*math.cos(i/20))) for i in range(20)]
    report = analyze_stability(samples)
    assert report['angular_rms_rad_s'] == pytest.approx(1)
    assert report['vibration_rms_m_s2'] > 1


@pytest.mark.parametrize('timestamps', [[0,0], [10,0], [0,2_000_000_000]])
def test_stale_nonmonotonic_or_gappy_trace_rejected(timestamps):
    with pytest.raises(ValueError): analyze_stability([MotionSample(t,(0,0,0)) for t in timestamps])


@pytest.mark.parametrize('rotation', [((2,0,0),(0,1,0),(0,0,1)), ((-1,0,0),(0,1,0),(0,0,1))])
def test_invalid_mount_transform_rejected(rotation):
    with pytest.raises(ValueError): calibration(rotation)


def test_recorded_fixture_loader_keeps_unverified_source():
    from pathlib import Path
    from backend.imu import load_recorded_trace
    report = analyze_stability(load_recorded_trace(Path(__file__).parent / 'fixtures/imu/stationary_tilt.jsonl'))
    assert report['source'] == ['recorded-file-unverified']
    assert report['angular_peak_rad_s'] == 0
    assert not report['camera_exposure_factor_verified']


def test_recorded_trace_cannot_spoof_live_source(tmp_path):
    from backend.imu import load_recorded_trace
    path = tmp_path / 'trace.jsonl'
    path.write_text('{"monotonic_ns":0,"gyro_rad_s":[0,0,0],"source":"live-calibrated-hardware"}\n')
    assert load_recorded_trace(path)[0].source == 'recorded-file-unverified'
