"""Aperture movement remains opt-in, lens-bounded and confirmed before capture."""
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from camera_caps import from_live_choices
from stack_planner import timelapse_config, metered_timelapse_step
from timelapse_controls import mounted_lens_for_frame, update_controls


def caps():
    return from_live_choices('Sony ILCE-7M2 (Control)', ['100','200','400'],
                             ['1','2','4','5.6','8','16','64'], ['1/60','1/30','1/15','1','30'])


def bounded():
    from dataclasses import replace
    return replace(caps(),apertures=[4,5.6,8],aperture_labels=['4','5.6','8']), {'usable_apertures':[4,5.6,8]}


def step(config, *, current_ev=0, luminance=.09, choices=None, evidence=None):
    choices = choices or bounded()[0]
    evidence = evidence if evidence is not None else bounded()[1]
    return metered_timelapse_step(config,current_ev=current_ev,luminance=luminance,
        base_shutter_s=1/60,base_iso=100,base_aperture='8',caps=choices,mounted_lens_evidence=evidence)


@pytest.mark.parametrize('order',list(itertools.permutations(['shutter','iso','aperture'])))
def test_all_explicit_three_axis_orders_validate(order):
    result=timelapse_config('auto_custom',{'priority':'_then_'.join(order),'aperture_locked':False})
    assert result['priority']=='_then_'.join(order)


@pytest.mark.parametrize('options',[
    {'aperture_locked':1},{'min_aperture':'4'},{'min_aperture':9,'max_aperture':4},
    {'max_aperture':float('nan')},{'priority':['aperture','iso']}])
def test_invalid_aperture_options_fail_as_validation_errors(options):
    with pytest.raises(ValueError):timelapse_config('auto_custom',options)


def test_default_stays_locked_even_with_aperture_first_order():
    result=step(timelapse_config('auto_custom',{'priority':'aperture_then_iso_then_shutter'}))
    assert result['aperture']=='8' and result['aperture_locked']
    assert result['cascade']=='iso_then_shutter'


def test_aperture_first_opens_within_lens_and_quantization_is_logged():
    result=step(timelapse_config('auto_custom',{'priority':'aperture_then_iso_then_shutter','aperture_locked':False}))
    assert result['aperture']=='5.6' and result['iso']=='100' and result['shutter']=='1/60'
    assert result['quantization_error_ev']==pytest.approx(result['actual_ev']-1)
    assert result['mounted_lens_limits']['usable_apertures']==[4,5.6,8]


def test_aperture_last_only_contributes_after_other_axes_saturate():
    config=timelapse_config('auto_custom',{'priority':'iso_then_shutter_then_aperture','aperture_locked':False,
        'min_iso':100,'max_iso':100,'min_shutter_s':1/60,'max_shutter_s':1/60})
    result=step(config,current_ev=3,luminance=.01)
    assert result['aperture']=='4' and result['actual_ev']==pytest.approx(2)
    assert result['range_limited'] and result['iso']=='100' and result['shutter']=='1/60'


def test_unlocked_planner_rejects_missing_mounted_evidence():
    with pytest.raises(ValueError,match='mounted-lens'):
        metered_timelapse_step(timelapse_config('auto_custom',{'aperture_locked':False}),
            current_ev=0,luminance=.09,base_shutter_s=1/60,base_iso=100,base_aperture='8',caps=caps())


def test_no_metadata_holds_current_despite_wide_driver_ladder(tmp_path):
    photo=tmp_path/'accepted.jpg';Image.new('RGB',(64,48)).save(photo)
    choices,evidence=mounted_lens_for_frame(caps(),'8',photo)
    assert choices.apertures==[8] and evidence['sources']==['current_readback_only']
    result=step(timelapse_config('auto_custom',{'aperture_locked':False}),choices=choices,evidence=evidence)
    assert result['aperture']=='8' and result['effective_aperture_locked']


def test_accepted_lens_spec_clips_raw_advertised_apertures(tmp_path):
    photo=tmp_path/'accepted.jpg';exif=Image.Exif();exif[42034]=(24,105,4,4)
    Image.new('RGB',(64,48)).save(photo,exif=exif)
    choices,evidence=mounted_lens_for_frame(caps(),'8',photo)
    assert choices.apertures==[4,5.6,8]
    assert evidence['source']=='previous_accepted_jpeg'
    assert evidence['unverified_stop_down_beyond_current_blocked']


@pytest.mark.parametrize('lens_model,profiled', [
    ('Tamron 28-75 f/2.8', True), ('Tamron 28-75mm F2.8', True), ('Tamron 28-75mm F2.8 Di III RXD', True),
    ('28-75mm F2.8 Di III RXD', True),
    ('E 28-75mm F2.8-2.8', False),      # the generic body form without a LensSpecification
    ('Tamron 28-75mm F/2.8 Di III VXD G2', False),
])
def test_timelapse_names_the_profiled_tamron_by_the_shared_normalisation(tmp_path, lens_model, profiled):
    photo=tmp_path/'accepted.jpg';exif=Image.Exif();exif[42036]=lens_model
    Image.new('RGB',(64,48)).save(photo,exif=exif)
    choices,evidence=mounted_lens_for_frame(caps(),'8',photo)
    assert ('verified_lens_profile' in evidence['sources']) is profiled
    assert (16 in choices.apertures) is profiled


def _spec(*values):
    from PIL.TiffImagePlugin import IFDRational
    return tuple(IFDRational(round(value * 10), 10) for value in values)


@pytest.mark.parametrize('lens_model,specification,profiled', [
    ('E 28-75mm F2.8-2.8', (28, 75, 2.8, 2.8), True),   # the A7 II's real LensModel, as /shoot accepts it
    ('E 28-75mm F2.8-2.8', (24, 70, 2.8, 2.8), False),  # another generic 2.8 zoom
    ('E 28-75mm F2.8-2.8', (28, 75, 2.8, 4.0), False),
    ('Tamron 28-75mm F/2.8 Di III VXD G2', (28, 75, 2.8, 2.8), False),
])
def test_timelapse_takes_the_generic_tamron_form_only_with_its_lens_specification(
        tmp_path, lens_model, specification, profiled):
    import rig_profile
    photo=tmp_path/'accepted.jpg';exif=Image.Exif();exif[42036]=lens_model;exif[42034]=_spec(*specification)
    Image.new('RGB',(64,48)).save(photo,exif=exif)
    choices,evidence=mounted_lens_for_frame(caps(),'8',photo)
    assert ('verified_lens_profile' in evidence['sources']) is profiled
    assert (16 in choices.apertures) is profiled
    # One rule: the same answer /shoot's rig resolution gives for this EXIF.
    rig=rig_profile.resolve_rig('Sony ILCE-7M2 (Control)',lens_model=lens_model,lens_specification=specification)
    assert rig.lens_profiled is profiled


def _mock_product(tmp_path,monkeypatch,*,bad_range=False):
    import sonyremote,engine,product_sessions
    monkeypatch.setattr(engine,'SHOTS_DIR',tmp_path/'shots')
    monkeypatch.setattr(engine,'_free_usb',lambda:None)
    baseline={'iso':'100','shutter':'1/60','aperture':'8','focus_mode':'Automatic'}
    live=dict(baseline);writes=[];shots=[]
    runner=product_sessions.runner()
    state=runner.create('timelapse',3,timelapse_mode='auto_custom',metadata={
        'camera_model':'Sony ILCE-7M2 (Control)','camera_port':'usb:000,003','camera_serial':'A7II00SERIAL'})
    update_controls(runner,state['id'],{'exposure':{'aperture_locked':False,
        'priority':'aperture_then_iso_then_shutter','min_aperture':4,'max_aperture':4 if bad_range else 8}})
    class Camera:
        def __init__(self,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def info(self):return SimpleNamespace(model='Sony ILCE-7M2 (Control)',port='usb:000,003')
        def wake(self):return True
        def shooting_mode(self):return 'M'
        def get_settings(self):return dict(live)
        def list_options(self,key):return {'iso':['100','200','400'],'aperture':['1','4','5.6','8','16','64'],
                                          'shutter':['1/60','1/30','1','30']}[key]
        def set_setting(self,key,value):writes.append((key,value));live[key]=value
        def set_exposure(self,**kwargs):writes.append(dict(kwargs));live.update(kwargs)
        def capture(self,**kwargs):
            shots.append(dict(live));photo=tmp_path/f'source{len(shots)}.jpg'
            exif=Image.Exif();exif[42034]=(24,105,4,4)
            Image.new('RGB',(64,48),(40,40,40)).save(photo,exif=exif)
            if len(shots)==2: update_controls(runner,state['id'],{'exposure':{'aperture_locked':True}})
            return str(photo)
    monkeypatch.setattr(sonyremote,'Camera',Camera)
    result=product_sessions.run(state['id'])
    return result,writes,shots,live,baseline


def test_product_transition_uses_accepted_lens_then_relocks_and_restores(tmp_path,monkeypatch):
    result,writes,shots,live,baseline=_mock_product(tmp_path,monkeypatch)
    assert result['status']=='completed',result
    assert [s['aperture'] for s in shots]==['8','5.6','8']
    assert [f['applied']['timelapse_control_revision'] for f in result['frames']]==[1,1,2]
    import product_sessions
    for frame in result['frames']:
        transition=product_sessions.runner().frame_evidence(result,frame['index'])['applied']['exposure_transition']
        assert transition['readback_verified'] and transition['after']==transition['requested']
    assert live==baseline and result['restore']=='completed'
    assert not any(isinstance(w,dict) and w['aperture'] in ('1','16','64') for w in writes)
    assert not any(isinstance(w,dict) and w['shutter']=='30' for w in writes)


def test_invalid_first_frame_aperture_bounds_issue_no_capture_or_unsafe_write(tmp_path,monkeypatch):
    result,writes,shots,live,baseline=_mock_product(tmp_path,monkeypatch,bad_range=True)
    assert result['status']=='failed' and shots==[]
    assert all(f['status']=='pending' for f in result['frames'])
    # Restoration may reapply the known-safe baseline; no speculative aperture is written.
    assert all(not isinstance(w,dict) or w=={'iso':'100','shutter':'1/60','aperture':'8'} for w in writes)
    assert live==baseline
