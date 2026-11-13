"""One final focus lock, no recapture, and evidence-bound restoration."""
from pathlib import Path
import pytest
from test_final_wave_engine import rig
from sonyremote import CameraError, CaptureOutcomeUncertain


def test_final_carries_meter_focus_and_restores_original_mode(rig):
    modes=[]
    original=rig.camera.capture
    def capture(**kwargs):
        modes.append(rig.camera.settings['focus_mode'])
        return original(**kwargs)
    rig.camera.capture=capture
    shot=rig.real.shoot(None,variant_id='natural')
    policy=shot.evidence['final_focus_policy']
    assert modes==['Automatic','Manual']
    assert rig.camera.settings['focus_mode']=='Automatic'
    assert policy['restore']=='verified' and policy['sharpness_verified'] is False
    assert policy['before_final_readback']=='Manual'
    assert policy['setting_writes']==[{'focus_mode':'Manual'},{'focus_mode':'Automatic'}]
    assert shot.evidence['transport_receipt']['final_focus_policy']==policy
    assert 'native camera focus for metering' in shot.mood['camera']
    assert 'in Manual for the final photo' in shot.mood['camera']
    assert 'original focus mode restored' in shot.mood['settings']['focus']
    assert 'sharpness unverified' in shot.mood['settings']['focus']
    assert 'leaving native autofocus in charge' not in shot.mood['camera']


def test_original_manual_needs_no_focus_setting_writes(rig):
    rig.camera.settings['focus_mode']='Manual'
    shot=rig.real.shoot(None,variant_id='natural')
    assert shot.evidence['final_focus_policy']['setting_writes']==[]
    assert shot.evidence['final_focus_policy']['restore']=='verified'
    assert 'manual focus for metering' in shot.mood['camera']


def test_lock_readback_refusal_preserves_meter_without_final_shutter(rig):
    original=rig.camera.set_setting
    rig.camera.set_setting=lambda key,value: None if key=='focus_mode' else original(key,value)
    with pytest.raises(CaptureOutcomeUncertain,match='no final shutter') as caught:
        rig.real.shoot(None,variant_id='natural')
    assert rig.camera.capture_count==1
    assert caught.value.capture_path.name=='frame-1.jpg'
    assert caught.value.focus_policy['restore']=='verified'
    assert caught.value.focus_policy['final_capture_attempted'] is False


@pytest.mark.parametrize('elapsed,reconnected,restored',[(20,True,True),(1,True,False),(20,False,False),(None,True,False)])
def test_uncertain_capture_restores_only_after_known_short_settle(rig,elapsed,reconnected,restored):
    original=rig.camera.capture
    failure=CaptureOutcomeUncertain('timeout',capture_path=Path('/preserved/final.jpg'))
    def capture(**kwargs):
        if rig.camera.capture_count==1:
            rig.camera.capture_count+=1
            rig.camera.last_capture_receipt={'reconnect_ok':reconnected,'timings':{'trigger_and_download_s':elapsed}}
            raise failure
        return original(**kwargs)
    rig.camera.capture=capture
    with pytest.raises(CaptureOutcomeUncertain) as caught:
        rig.real.shoot(None,variant_id='natural')
    assert caught.value is failure
    assert caught.value.capture_path==Path('/preserved/final.jpg')
    assert caught.value.may_have_fired and not caught.value.retry_safe
    assert rig.camera.capture_count==2
    assert rig.camera.settings['focus_mode']==('Automatic' if restored else 'Manual')
    assert failure.focus_policy['restore']==('verified' if restored else 'deferred_until_physical_idle_verified')


def test_restore_failure_preserves_successful_photo_and_receipt(rig):
    original=rig.camera.set_setting
    def set_setting(key,value):
        if key=='focus_mode' and value=='Automatic':
            raise CameraError('restore refused')
        return original(key,value)
    rig.camera.set_setting=set_setting
    shot=rig.real.shoot(None,variant_id='natural')
    assert shot.ok and rig.camera.capture_count==2
    assert shot.evidence['final_focus_policy']['restore']=='failed'
    assert 'restore refused' in shot.evidence['final_focus_policy']['restore_error']
    assert 'focus restoration unverified' in shot.mood['camera']
    assert 'original focus mode restored' not in shot.mood['settings']['focus']


def test_unknown_original_focus_refuses_final_exposure(rig):
    rig.camera.settings.pop('focus_mode')
    with pytest.raises(CaptureOutcomeUncertain,match='Original focus mode is unknown'):
        rig.real.shoot(None,variant_id='natural')
    assert rig.camera.capture_count==1


def test_final_refused_by_identity_keeps_the_meter_frame_as_the_earlier_capture(rig):
    from sonyremote import CameraIdentityError
    original = rig.camera.capture
    refusal = CameraIdentityError('fixture', reason='serial_mismatch')

    def capture(**kwargs):
        if rig.camera.capture_count == 1:
            raise refusal  # the final shutter is refused; the meter frame already exists
        return original(**kwargs)
    rig.camera.capture = capture
    with pytest.raises(CameraIdentityError) as caught:
        rig.real.shoot(None, variant_id='natural')
    assert caught.value is refusal and caught.value.may_have_fired is False
    assert caught.value.capture_path.name == 'frame-1.jpg'
    assert caught.value.as_dict()['earlier_capture_path'].endswith('frame-1.jpg')
