from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import cv2
import numpy as np
import pytest
from long_exposure_plan import plan_long_exposure, validate_duration, apply_duration_plan, timing_receipt
from stack_session import SessionRunner


@pytest.mark.parametrize("duration", [True, float('nan'), float('inf'), 0, 3601])
def test_duration_bounds(duration):
    with pytest.raises(ValueError):
        validate_duration(duration)


@pytest.mark.parametrize("shutter", [0, 31, float('nan'), True])
def test_no_bulb_or_unbounded_shutter(shutter):
    with pytest.raises(ValueError):
        plan_long_exposure(30, shutter)


def test_plan_distinguishes_window_from_integrated_shutter():
    plan = plan_long_exposure(30, 1/60)
    assert plan['frames'] == 4
    assert plan['interval_s'] * (plan['frames']-1) + plan['shutter_s'] == pytest.approx(30)
    assert plan['planned_integrated_shutter_s'] == pytest.approx(4/60)
    assert plan['continuous_exposure'] is False
    assert plan['allowance_source'] == 'conservative_unverified_planning_allowance'
    assert plan_long_exposure(3600, 1/60)['frames'] == 16


def test_physically_unachievable_window_rejected_before_capture():
    with pytest.raises(ValueError, match='at least 28.000s'):
        plan_long_exposure(20, 10)


def test_duration_mode_interval_conflicts(tmp_path):
    runner = SessionRunner(tmp_path)
    with pytest.raises(ValueError, match='requires long_exposure'):
        runner.create('hdr', 3, duration_s=30)
    with pytest.raises(ValueError, match='interval_s must be zero'):
        runner.create('long_exposure', 3, duration_s=30, interval_s=2)


def test_unplanned_duration_cannot_bypass_baseline_preflight(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create('long_exposure', 16, duration_s=30)
    with pytest.raises(ValueError, match='confirmed-baseline planning'):
        runner.run(state['id'], lambda *args: pytest.fail('unplanned capture'))


def test_duration_scheduler_executes_selected_count_and_records_actual_window(tmp_path, monkeypatch, module_time):
    import stack_session
    clock = {'now': 100.0}
    module_time(stack_session, monotonic=lambda: clock['now'],
                sleep=lambda seconds: clock.update(now=clock['now']+seconds))
    runner = SessionRunner(tmp_path)
    state = runner.create('long_exposure', 16, duration_s=30)
    plan = apply_duration_plan(state, 1/60)
    runner.save(state)
    captures, restored = [], []
    def capture(index, spec, destination):
        captures.append(clock['now'])
        clock['now'] += 2.0  # Fixture capture+transfer, no real shutter.
        assert cv2.imwrite(destination, np.full((16, 16, 3), 80+index, np.uint8))
        return destination
    result = runner.run(state['id'], capture, prepare_frame=lambda *args: {'shutter':'1/60'},
                        restore_settings=lambda: restored.append(True))
    assert result['status'] == 'completed' and restored == [True]
    assert len(captures) == 4 and result['frame_count'] == 4
    assert captures[-1]-captures[0]+1/60 == pytest.approx(30)
    timing = result['long_exposure_timing']
    assert timing['actual_capture_window_s'] == pytest.approx(30)
    assert timing['integrated_shutter_s'] == pytest.approx(4/60)
    assert timing['observed_capture_completion_s'] == pytest.approx(30-1/60+2)
    assert timing['window_within_tolerance'] and not timing['continuous_exposure']


def test_slow_transfer_records_overrun_without_calling_it_continuous(tmp_path, monkeypatch, module_time):
    import stack_session
    clock = {'now': 100.0}
    module_time(stack_session, monotonic=lambda: clock['now'],
                sleep=lambda seconds: clock.update(now=clock['now']+seconds))
    runner = SessionRunner(tmp_path)
    state = runner.create('long_exposure', 2, duration_s=10)
    apply_duration_plan(state, 1/60)
    runner.save(state)
    def capture(index, spec, destination):
        clock['now'] += 12
        assert cv2.imwrite(destination, np.full((16, 16, 3), 80+index, np.uint8))
        return destination
    result = runner.run(state['id'], capture, prepare_frame=lambda *args: {'shutter':'1/60'})
    timing = result['long_exposure_timing']
    assert timing['overrun_s'] > 2
    assert not timing['window_within_tolerance']
    assert timing['integrated_shutter_s'] == pytest.approx(2/60)


def test_interrupted_window_cannot_resume_new_shutters(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create('long_exposure', 3, duration_s=30)
    apply_duration_plan(state, 1/60)
    state['frames'][0]['status'] = 'accepted'
    with pytest.raises(ValueError, match='cannot resume'):
        apply_duration_plan(state, 1/60)


def test_ambiguity_leaves_integrated_seconds_unknown():
    plan = plan_long_exposure(30, 1/60)
    receipt = timing_receipt(plan, [{'status':'ambiguous', 'applied':{'shutter':'1/60'}}])
    assert receipt['integrated_shutter_s'] is None
    assert receipt['unresolved_capture']
    assert not receipt['window_within_tolerance']


def test_resumed_wall_clock_does_not_claim_monotonic_duration():
    plan = plan_long_exposure(30, 1/60, frame_budget=2)
    frames = [{'status':'accepted', 'applied':{'shutter':'1/60'}},
              {'status':'accepted', 'applied':{'shutter':'1/60'}, 'actual_interval_s':30,
               'actual_interval_source':'wall_clock_after_resume', 'capture_pipeline_s':2}]
    receipt = timing_receipt(plan, frames)
    assert receipt['actual_capture_window_s'] is None
    assert not receipt['window_within_tolerance']


@pytest.fixture
def product_duration_rig(tmp_path, monkeypatch, module_time):
    from types import SimpleNamespace
    import engine, product_capture, product_sessions, sonyremote, stack_session
    clock = {'now':100.0}
    for module in (engine, product_capture, product_sessions, stack_session):  # the product path's clocks
        module_time(module, monotonic=lambda: clock['now'],
                    sleep=lambda seconds: clock.update(now=clock['now']+seconds))
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setattr(engine, '_free_usb', lambda: None)
    baseline = {'iso':'100','aperture':'8','shutter':'1/60','focus_mode':'Automatic'}
    live = dict(baseline)
    captures, writes = [], []
    config = {'capture_s':2}
    class Camera:
        def __init__(self, **kwargs):
            assert kwargs == {'model':'Sony ILCE-7M2 (Control)', 'port':'usb:001,002', 'serial':'A7II00SERIAL'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model='Sony ILCE-7M2 (Control)', port='usb:001,002')
        def wake(self): return True
        def shooting_mode(self): return 'M'
        def get_settings(self): return dict(live)
        def list_options(self, key): return {'iso':['100'],'aperture':['8'],'shutter':['1/60']}[key]
        def set_exposure(self, **kwargs):
            writes.append(dict(kwargs));live.update(kwargs)
        def set_setting(self, key, value):
            writes.append({key:value});live[key]=value
        def capture(self, **kwargs):
            captures.append(clock['now']);clock['now']+=config['capture_s']
            destination = tmp_path/f'capture-{len(captures)}.jpg'
            assert cv2.imwrite(str(destination), np.full((32, 32, 3), 80+len(captures), np.uint8))
            return destination
    monkeypatch.setattr(sonyremote, 'Camera', Camera)
    sessions=product_sessions.runner()
    def create(duration, frames=16):
        return sessions.create('long_exposure', frames, duration_s=duration,
            metadata={'camera_model':'Sony ILCE-7M2 (Control)','camera_port':'usb:001,002','camera_serial':'A7II00SERIAL'})
    return SimpleNamespace(create=create, run=product_sessions.run, captures=captures,
                           writes=writes, config=config, baseline=baseline, live=live)


def test_product_duration_preflight_does_not_fire_unachievable_job(product_duration_rig):
    rig=product_duration_rig
    state=rig.create(5)
    with pytest.raises(ValueError, match='too short'):
        rig.run(state['id'])
    assert not rig.captures and not rig.writes and rig.live==rig.baseline


def test_product_duration_captures_bounded_window_restores_and_publishes(product_duration_rig):
    rig=product_duration_rig
    result=rig.run(rig.create(30)['id'])
    assert result['status']=='completed' and result['publication']['status']=='published'
    assert len(rig.captures)==4 and result['frame_count']==4
    assert result['long_exposure_timing']['actual_capture_window_s']==pytest.approx(30)
    assert result['long_exposure_timing']['integrated_shutter_s']==pytest.approx(4/60)
    assert rig.live==rig.baseline
    assert all(change.get('shutter','1/60')=='1/60' for change in rig.writes)


def test_product_duration_stops_before_second_shutter_when_transport_misses_window(product_duration_rig):
    rig=product_duration_rig
    rig.config['capture_s']=12
    result=rig.run(rig.create(10,2)['id'])
    assert result['status']=='failed' and len(rig.captures)==1
    assert 'duration window elapsed' in result['error']
    assert result['frames'][0]['status']=='accepted'
    assert result['frames'][1]['status']=='pending'
    assert result['long_exposure_timing']['incomplete']
    assert result['long_exposure_timing']['completion_overrun_s']>=2
    assert rig.live==rig.baseline
