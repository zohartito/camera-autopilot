import copy
import json
from pathlib import Path

import pytest
from PIL import Image

from stack_session import SessionRunner
from stack_planner import timelapse_config, metered_timelapse_step
from timelapse_controls import smartlapse_settings, update_controls, read_controls, adapt_pending
from camera_caps import A7II_CAPS


def session(tmp_path, count=4):
    runner = SessionRunner(tmp_path / 'sessions')
    state = runner.create('timelapse', count, metadata={'camera_model': 'Sony ILCE-7M2 (Control)', 'camera_port': 'usb:000,003', 'camera_serial': 'A7II00SERIAL'})
    return runner, state


@pytest.mark.parametrize('value', [
    {'duration_s': 3601, 'output_s': 1}, {'duration_s': 10, 'output_s': 11},
    {'duration_s': 10, 'output_s': True}, {'duration_s': float('nan'), 'output_s': 1},
    {'duration_s': 10, 'output_s': 1, 'fps': 61}])
def test_smartlapse_rejects_unbounded_demand(value):
    with pytest.raises(ValueError): smartlapse_settings(value)


def test_configure_smartlapse_sets_real_frame_count_and_duration(tmp_path):
    runner, state = session(tmp_path)
    result = update_controls(runner, state['id'], {'smartlapse': {'duration_s': 20, 'output_s': .5, 'fps': 24}})
    actual = runner.read(state['id'])
    assert result['revision'] == 1 and actual['frame_count'] == 12
    assert actual['interval_s'] * 11 == pytest.approx(20)
    assert len(actual['frames']) == 12 and all(f['status'] == 'pending' for f in actual['frames'])


def test_smartlapse_requires_prestart_and_rejects_queued_job(tmp_path):
    runner, state = session(tmp_path)
    (runner._directory(state['id']) / 'job.json').write_text(json.dumps({'status':'queued'}))
    with pytest.raises(ValueError, match='before submitting'):
        update_controls(runner, state['id'], {'smartlapse': {'duration_s': 20, 'output_s': .5}})


def test_control_update_during_operation_is_nonblocking(tmp_path):
    runner, state = session(tmp_path)
    with runner.operation(state['id']):
        result = update_controls(runner, state['id'], {'exposure': {'mode': 'auto_custom', 'priority':'iso_then_shutter', 'max_iso': 800}, 'preview_every': 2})
    assert result['revision'] == 1
    assert result['exposure']['priority'] == 'iso_then_shutter'
    assert read_controls(runner, state['id']) == result


def test_invalid_control_update_keeps_revision(tmp_path):
    runner, state = session(tmp_path)
    result = update_controls(runner, state['id'], {'preview_every': 2})
    with pytest.raises(ValueError): update_controls(runner, state['id'], {'exposure': {'priority':'aperture_first'}})
    assert read_controls(runner, state['id']) == result


def test_iso_first_is_actually_different_and_still_bounded():
    kwargs = dict(current_ev=0, luminance=.09, base_shutter_s=1/60, base_iso=100, caps=A7II_CAPS)
    shutter = metered_timelapse_step(timelapse_config('auto_custom'), **kwargs)
    iso = metered_timelapse_step(timelapse_config('auto_custom', {'priority':'iso_then_shutter'}), **kwargs)
    assert shutter['iso'] == '100' and shutter['shutter'] == '1/30'
    assert iso['iso'] == '200' and iso['shutter'] == '1/60'
    assert iso['cascade'] == 'iso_then_shutter' and iso['actual_ev'] == pytest.approx(1)


def test_measured_throughput_reduces_only_pending_suffix(tmp_path):
    _, state = session(tmp_path, 10)
    state['frames'][0].update(status='accepted', receipt={'sha256':'a'*64})
    original = copy.deepcopy(state['frames'][0])
    plan = smartlapse_settings({'duration_s':60,'output_s':1,'fps':10})
    status = adapt_pending(state, plan, elapsed_s=10, observed_frame_s=10)
    assert state['frame_count'] == 6 and state['frames'][0] == original
    assert status['requested_count'] == 10 and status['reduced_for_throughput']
    assert state['interval_s'] == 10
    assert status['actual_output_s'] == .1


def test_runner_hook_reduces_no_extra_shutters_and_preserves_receipts(tmp_path):
    runner, state = session(tmp_path, 8)
    captures = []
    def capture(index, spec, dest):
        captures.append(index)
        Image.new('RGB',(32,24),(index*20,70,80)).save(dest)
        return dest
    def after(live):
        count = sum(f['status']=='accepted' for f in live['frames'])
        adapt_pending(live, smartlapse_settings({'duration_s':1,'output_s':1,'fps':8}), elapsed_s=count*1, observed_frame_s=1)
    result = runner.run(state['id'], capture, after_frame=after)
    assert captures == [0,1] and result['status']=='completed' and result['frame_count']==2
    assert all(f['status']=='accepted' for f in result['frames'])


def test_incremental_video_contains_accepted_frames_only(tmp_path):
    from timelapse_video import probe_encoder
    if probe_encoder() is None: pytest.skip('ffmpeg/ffprobe did not execute (-version)')
    import product_sessions
    runner, state = session(tmp_path, 4)
    seen = []
    def capture(index,spec,dest):
        Image.new('RGB',(64,48),(index*30,60,80)).save(dest)
        return dest
    def after(live):
        if sum(f['status']=='accepted' for f in live['frames']) == 2:
            live['preview_video'] = product_sessions._incremental_video(runner, live, 24)
            seen.append(copy.deepcopy(live['preview_video']))
    runner.run(state['id'], capture, after_frame=after)
    assert seen[0]['frame_count']==2 and seen[0]['full_decode_verified']
    assert seen[0]['width']==1920 and seen[0]['height']==1080
    assert seen[0]['playback_duration_s']==pytest.approx(2/24)
    assert int(seen[0]['probe']['nb_read_frames'])==2
    assert int(seen[0]['probe']['width'])==1920
    assert len(seen[0]['inputs'])==2
    assert runner.artifact(state['id'], seen[0]['name']).is_file()
    with pytest.raises(FileNotFoundError): runner.artifact(state['id'],'preview_0003.mp4')


def test_snapshot_failure_does_not_rewrite_accepted_frames(tmp_path, monkeypatch):
    import product_sessions
    import subprocess
    import timelapse_video
    runner, state = session(tmp_path, 2)
    def capture(index,spec,dest):
        Image.new('RGB',(32,24),(index*20,70,80)).save(dest)
        return dest
    completed = runner.run(state['id'], capture)
    before = copy.deepcopy(completed['frames'])
    monkeypatch.setattr(timelapse_video, 'probe_encoder', lambda: {'ffmpeg': '/x', 'ffprobe': '/x'})
    def fail(*a, **k): raise subprocess.TimeoutExpired('ffmpeg',8)
    monkeypatch.setattr(timelapse_video, 'render_h264', fail)
    with pytest.raises(subprocess.TimeoutExpired): product_sessions._incremental_video(runner,completed,24)
    assert runner.read(state['id'])['frames']==before
    assert not (runner._directory(state['id'])/'preview.pending.mp4').exists()


def test_incremental_preview_without_encoder_is_unavailable(tmp_path, monkeypatch):
    import product_sessions
    import timelapse_video
    from timelapse_video import EncoderUnavailable
    runner, state = session(tmp_path, 2)
    def capture(index,spec,dest):
        Image.new('RGB',(32,24),(index*20,70,80)).save(dest)
        return dest
    completed = runner.run(state['id'], capture)
    before = copy.deepcopy(completed['frames'])
    monkeypatch.setattr(timelapse_video, 'probe_encoder', lambda: None)
    with pytest.raises(EncoderUnavailable):
        product_sessions._incremental_video(runner, completed, 24)
    assert runner.read(state['id'])['frames']==before
    assert not (runner._directory(state['id'])/'preview.pending.mp4').exists()


def test_midrun_update_changes_next_frame_only_and_restores(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sonyremote
    import engine
    import product_sessions
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setattr(engine, '_free_usb', lambda: None)
    baseline = {'iso':'100','shutter':'1/60','aperture':'8','focus_mode':'Automatic'}
    live = dict(baseline)
    captures = []
    runner = product_sessions.runner()
    state = runner.create('timelapse',3,timelapse_mode='auto_custom',
        metadata={'camera_model':'Sony ILCE-7M2 (Control)','camera_port':'usb:000,003','camera_serial':'A7II00SERIAL'})
    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def info(self): return SimpleNamespace(model='Sony ILCE-7M2 (Control)',port='usb:000,003')
        def wake(self): return True
        def shooting_mode(self): return 'M'
        def get_settings(self): return dict(live)
        def list_options(self,key): return {'iso':['100','200','400','800'], 'aperture':['8'], 'shutter':['1/60','1/30','1/15']}[key]
        def set_setting(self,key,value): live[key]=value
        def set_exposure(self,**kwargs): live.update(kwargs)
        def capture(self,**kwargs):
            captures.append(dict(live))
            path=tmp_path/f'source_{len(captures)}.jpg'
            Image.new('RGB',(64,48),(40,40,40)).save(path)
            if len(captures)==1:
                update_controls(runner,state['id'],{'exposure':{'priority':'iso_then_shutter','max_iso':400}})
            return str(path)
    monkeypatch.setattr(sonyremote,'Camera',Camera)
    result=product_sessions.run(state['id'])
    assert result['status']=='completed' and result['restore']=='completed'
    assert captures[0]['iso']=='100' and captures[1]['iso']=='200'
    assert captures[1]['shutter']=='1/60'
    assert [f['applied']['timelapse_control_revision'] for f in result['frames']]==[0,1,1]
    assert all(c['aperture']=='8' for c in captures) and live==baseline


def test_smartlapse_ambiguous_suffix_is_never_rewritten(tmp_path):
    _,state=session(tmp_path)
    state['frames'][0]['status']='accepted'
    state['frames'][1]['status']='ambiguous'
    before=copy.deepcopy(state)
    with pytest.raises(ValueError):
        adapt_pending(state, smartlapse_settings({'duration_s':1,'output_s':1,'fps':4}),elapsed_s=2,observed_frame_s=1)
    assert state==before


def test_midrun_fps_change_is_rejected(tmp_path):
    runner,state=session(tmp_path)
    with pytest.raises(ValueError,match='fps'):
        update_controls(runner,state['id'],{'exposure':{'fps':30}})


def test_tampered_incremental_preview_path_is_rejected(tmp_path):
    runner,state=session(tmp_path)
    state['preview_video']={'status':'completed','name':'../../outside.mp4','receipt':{'path':'/tmp/outside.mp4'}}
    runner.save(state)
    with pytest.raises(ValueError,match='preview identity'):
        runner.read(state['id'])


def test_incremental_preview_preserves_1080p_when_source_supports_it(tmp_path):
    from timelapse_video import probe_encoder
    if probe_encoder() is None: pytest.skip('ffmpeg/ffprobe did not execute (-version)')
    import product_sessions
    runner,state=session(tmp_path,2)
    def capture(index,spec,dest):
        Image.new('RGB',(1920,1080),(index*30,60,90)).save(dest)
        return dest
    result=runner.run(state['id'],capture)
    video=product_sessions._incremental_video(runner,result,24)
    assert (int(video['probe']['width']),int(video['probe']['height']))==(1920,1080)
    assert video['playback_duration_s']==pytest.approx(2/24)
    assert int(video['probe']['nb_read_frames'])==2 and video['full_decode_verified']


def test_republication_preserves_policy_and_intent_evidence(tmp_path, monkeypatch):
    import engine
    import product_sessions
    from photo_lifecycle import PhotoLifecycle
    monkeypatch.setattr(engine,'SHOTS_DIR',tmp_path/'shots')
    runner=product_sessions.runner()
    policy={'mode':'hdr','frames':2}
    state=runner.create('timelapse',2,metadata={'capture_policy':policy,'intent':'protect the bright windows'})
    def capture(index,spec,dest):
        Image.new('RGB',(32,24),(index*20,70,80)).save(dest)
        return dest
    completed=runner.run(state['id'],capture)
    product_sessions.run(state['id'])
    photos=PhotoLifecycle(engine.SHOTS_DIR)
    name=f"session_{state['id']}.json"
    sidecar=photos._json(name)
    sidecar['evidence']['stack_policy']={'requested':policy,'decision':{'mode':'hdr'}}
    sidecar['evidence']['capture_timing']={'stack_session_s':12}
    photos._write_json(name,sidecar)
    product_sessions.run(state['id'])
    final=photos._json(name)
    assert final['intent']=='protect the bright windows'
    assert final['evidence']['capture_policy']==policy
    assert final['evidence']['stack_policy']==sidecar['evidence']['stack_policy']
    assert final['evidence']['capture_timing']=={'stack_session_s':12}


def test_transport_timing_survives_success_and_ambiguous_frame(tmp_path):
    runner,state=session(tmp_path)
    def capture(index,spec,dest):
        spec['transport_receipt']={'timings':{'total_s':1.25},'capture_confirmed':index==0}
        if index==1: raise RuntimeError('lost camera after shutter')
        Image.new('RGB',(32,24),(40,70,80)).save(dest)
        return dest
    result=runner.run(state['id'],capture)
    assert result['frames'][0]['transport_receipt']['timings']['total_s']==1.25
    assert result['frames'][1]['status']=='ambiguous'
    assert result['frames'][1]['transport_receipt']['capture_confirmed'] is False


def test_even_sample_keeps_both_ends_and_stays_strictly_increasing():
    import product_sessions
    assert product_sessions._even_sample(5, 100) == [0, 1, 2, 3, 4]
    for count in (101, 150, 199, 240):
        picks = product_sessions._even_sample(count, 100)
        assert len(picks) == 100
        assert picks[0] == 0 and picks[-1] == count - 1
        assert all(b > a for a, b in zip(picks, picks[1:]))


def test_long_run_preview_samples_the_whole_run_instead_of_freezing(tmp_path):
    """Owner's call 2026-09-22: past 100 frames the live preview must keep updating by sampling
    the whole accepted run evenly, not refuse (it used to raise and freeze at frame 100)."""
    from timelapse_video import decode_frames, probe_encoder
    if probe_encoder() is None: pytest.skip('ffmpeg/ffprobe did not execute (-version)')
    import product_sessions
    runner, state = session(tmp_path, 120)
    def capture(index, spec, dest):
        # Red channel encodes the frame index, so the decoded preview proves WHICH frames it holds.
        Image.new('RGB', (64, 48), (index * 2, 60, 80)).save(dest, quality=98)
        return dest
    completed = runner.run(state['id'], capture)
    preview = product_sessions._incremental_video(runner, completed, 24)
    picks = product_sessions._even_sample(120, product_sessions.PREVIEW_MAX_FRAMES)
    assert preview['frame_count'] == 100 and preview['accepted_frames'] == 120
    assert preview['sampling'] == 'even'
    assert [item['index'] for item in preview['inputs']] == picks
    assert int(preview['probe']['nb_read_frames']) == 100
    assert preview['name'] == 'preview_0120.mp4'
    directory = runner._directory(state['id'])
    manifest = json.loads((directory / 'preview-inputs.json').read_text())
    assert [item['index'] for item in manifest['frames']] == picks, "manifest must not overclaim frames"
    assert manifest['sampling'] == 'even' and manifest['accepted_frames'] == 120
    assert not (directory / 'preview_frames').exists(), "sampled link folder must be cleaned up"
    decoded = decode_frames(directory / preview['name'], tmp_path / 'decoded')
    last_red = Image.open(decoded[-1]).convert('RGB').getpixel((960, 540))[0]
    assert abs(last_red - 119 * 2) <= 8, f"preview ends on the wrong frame (red {last_red})"


def test_short_run_preview_is_unchanged(tmp_path):
    from timelapse_video import probe_encoder
    if probe_encoder() is None: pytest.skip('ffmpeg/ffprobe did not execute (-version)')
    import product_sessions
    runner, state = session(tmp_path, 3)
    def capture(index, spec, dest):
        Image.new('RGB', (64, 48), (index * 40, 60, 80)).save(dest)
        return dest
    completed = runner.run(state['id'], capture)
    preview = product_sessions._incremental_video(runner, completed, 24)
    assert preview['frame_count'] == 3 and preview['sampling'] == 'all'
    assert [item['index'] for item in preview['inputs']] == [0, 1, 2]
