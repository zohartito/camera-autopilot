"""Provisional short-shutter transport policy keeps ordinary Auto usable."""
from dataclasses import replace
from unittest.mock import patch
import pytest
from test_final_wave_engine import rig
import brain
import camera_caps
from sonyremote.camera import CameraError, validate_sony_capture_shutter


def test_caps_separate_advertised_long_shutters_from_enabled_transport():
    raw = camera_caps.from_live_choices("Sony ILCE-7M2 (Control)", ["100","6400"], ["2.8","5.6"], ["1/500","1/15","1","30"])
    usable,evidence = camera_caps.constrain_capture_duration(raw,max_shutter_s=1)
    assert 30 in raw.shutters and 30 not in usable.shutters
    assert evidence["status"] == "provisional_short_exposure_policy"
    assert evidence["max_enabled_shutter_s"] == 1
    with pytest.raises(CameraError,match="disabled"):
        validate_sony_capture_shutter("30")


def test_bright_scene_cannot_select_driver_shutter_faster_than_transport_supports():
    raw = camera_caps.from_live_choices("Sony ILCE-7M2 (Control)", ["100"], ["2.8"],
        ["1/32000", "1/16000", "1/10000", "1/8000", "1/4000", "1", "30"])
    caps, evidence = camera_caps.constrain_capture_duration(raw, max_shutter_s=1)
    assert min(raw.shutters) == 1/32000
    assert evidence["raw_driver_shutters_s"] == raw.shutters
    assert evidence["min_enabled_shutter_s"] == 1/8000
    assert caps.shutters == [1/8000, 1/4000, 1]
    assert caps.shutter_labels == ["1/8000", "1/4000", "1"]
    with patch.object(brain.embed_index, "recommend", return_value={"f_number":2.8}):
        rec = brain.recommend_live("unused.jpg", 20, caps.name, caps, 2.8, 28)
    assert rec.shutter_s == 1/8000
    assert validate_sony_capture_shutter(rec.shutter_label)["configured_shutter_s"] == 1/8000


def test_only_unsupported_fast_shutters_refuses_before_planning():
    caps = camera_caps.from_live_choices("Sony ILCE-7M2 (Control)", ["100"], ["2.8"],
                                         ["1/32000", "1/10000"])
    with pytest.raises(ValueError, match="No live shutter choices"):
        camera_caps.constrain_capture_duration(caps, max_shutter_s=1)


@pytest.mark.parametrize("original_shutter", ["30","Bulb","unknown"])
def test_auto_bounds_meter_before_capture_and_logs_original_settings(rig,original_shutter):
    rig.camera.settings["shutter"] = original_shutter
    original_options = rig.camera.list_options
    rig.camera.list_options = lambda key: ["1/500","1/125","1","30"] if key=="shutter" else original_options(key)
    captured=[]
    original_capture=rig.camera.capture
    def capture(*args,**kwargs):
        captured.append(dict(rig.camera.settings))
        assert camera_caps._shutter_seconds(rig.camera.settings["shutter"])<=1
        return original_capture(*args,**kwargs)
    rig.camera.capture=capture
    shot=rig.real.shoot("natural",variant_id="natural")
    assert rig.camera.capture_count==2
    assert captured[0]["shutter"]=="1"
    assert shot.evidence["meter_preflight"]["before"]["shutter"]==original_shutter
    assert shot.evidence["meter_preflight"]["setting_writes"]==[{"shutter":"1"}]
    assert shot.evidence["profile"]["capture_transport_limits"]["raw_driver_shutters_s"][-1]==30


def test_dark_scene_ranks_degree_of_motion_failure_before_low_iso():
    caps=camera_caps.from_live_choices("Sony ILCE-7M2 (Control)",
        ["100","200","400","800","1600","3200","6400"],["2.8","4","5.6"],
        ["1/500","1/125","1/60","1/30","1/15","1/8","1/4","1/2","1","30"])
    caps,_=camera_caps.constrain_capture_duration(caps,max_shutter_s=1)
    with patch.object(brain.embed_index,"recommend",return_value={"f_number":5.6}):
        rec=brain.recommend_live("unused.jpg",.544,caps.name,caps,5.6,28)
    assert rec.shutter_s<=1/15
    assert rec.iso==6400
    assert rec.aperture==2.8
    assert any("closest achievable shutter" in note for note in rec.notes)


def test_no_enabled_meter_setting_fails_before_first_capture(rig):
    rig.camera.settings["shutter"]="30"
    original_options=rig.camera.list_options
    rig.camera.list_options=lambda key:["30"] if key=="shutter" else original_options(key)
    with pytest.raises(CameraError,match="no shutter was issued"):
        rig.real.shoot("natural",variant_id="natural")
    assert rig.camera.capture_count==0


def test_session_long_shutter_preflight_remains_pending_not_ambiguous(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import sonyremote,engine,product_sessions
    monkeypatch.setattr(engine,"SHOTS_DIR",tmp_path/"shots")
    monkeypatch.setattr(engine,"_free_usb",lambda:None)
    baseline={"iso":"100","aperture":"5.6","shutter":"30","focus_mode":"Automatic"}
    live=dict(baseline)
    captures=[]
    class Camera:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def info(self):return SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:001,002")
        def wake(self):return True
        def shooting_mode(self):return "M"
        def get_settings(self):return dict(live)
        def list_options(self,key):return {"iso":["100"],"aperture":["5.6"],"shutter":["1/60","30"]}[key]
        def set_setting(self,key,value):live[key]=value
        def set_exposure(self,**kwargs):live.update(kwargs)
        def capture(self,**kwargs):captures.append(1);pytest.fail("preflight must prevent this call")
    monkeypatch.setattr(sonyremote,"Camera",Camera)
    runner=product_sessions.runner()
    state=runner.create("timelapse",2,metadata={"camera_model":"Sony ILCE-7M2 (Control)","camera_port":"usb:001,002","camera_serial":"A7II00SERIAL"})
    result=product_sessions.run(state["id"])
    assert result["status"]=="failed"
    assert all(frame["status"]=="pending" for frame in result["frames"])
    assert "longer than 1 second" in result["error"]
    assert result["restore"]=="completed" and live==baseline and not captures
