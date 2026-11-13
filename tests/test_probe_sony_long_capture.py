from pathlib import Path
from types import SimpleNamespace
import importlib.util
import sys
import pytest
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("probe_sony_long_capture",ROOT/"tools/probe_sony_long_capture.py")
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


def test_plan_only_never_uses_camera_or_subprocess(tmp_path,monkeypatch):
    monkeypatch.setattr(probe.subprocess,"run",lambda *a,**k:pytest.fail("dry run may not act"))
    args=SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003",shutter="30",execute=False)
    result,code=probe.run_probe(args)
    assert code==0 and result["status"]=="plan_only"
    assert result["capture"].count("--trigger-capture")==1
    assert "--trigger-capture" not in result["pre_drain"]
    assert result["capture_timeout_s"]==90


@pytest.mark.parametrize("old_ram,wrong_exif",[(True,False),(False,False),(False,True)])
def test_probe_quarantines_old_ram_or_new_photos_and_restores(tmp_path,monkeypatch,old_ram,wrong_exif):
    import sonyremote,engine
    baseline={"iso":"6400","aperture":"f/5.6","shutter":"1/500","focus_mode":"Automatic"}
    live=dict(baseline);commands=[];clock={"now":0};connections=[]
    class Camera:
        def __init__(self,**kwargs):connections.append(kwargs)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def info(self):return SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003")
        def shooting_mode(self):return "M"
        def get_settings(self):return dict(live)
        def set_exposure(self,**kwargs):live.update(kwargs)
        def set_setting(self,key,value):live[key]=value
    def run(argv,**kwargs):
        commands.append(argv)
        if "--trigger-capture" in argv:
            clock["now"]+=75
            exif=Image.Exif();exif[272]="ILCE-7M2";exif[34855]=100;exif[33437]=5.6;exif[33434]=15 if wrong_exif else 30
            Image.new("RGB",(32,24),(90,80,70)).save(kwargs["cwd"]/"new.jpg",exif=exif)
        else:
            clock["now"]+=5
            if old_ram:Image.new("RGB",(32,24)).save(kwargs["cwd"]/"old.jpg")
        return SimpleNamespace(returncode=0,stdout="fixture",stderr="")
    monkeypatch.setattr(sonyremote,"Camera",Camera)
    monkeypatch.setattr(engine,"_free_usb",lambda:None)
    monkeypatch.setattr(probe.shutil,"which",lambda _:"/fake/gphoto2")
    monkeypatch.setattr(probe.subprocess,"run",run)
    monkeypatch.setattr(probe.time,"monotonic",lambda:clock["now"])
    monkeypatch.setattr(probe.time,"sleep",lambda seconds:clock.update(now=clock["now"]+seconds))
    args=SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003",shutter="30",aperture="f/5.6",execute=True,out=str(tmp_path),serial="A7II00SERIAL")
    monkeypatch.setattr("sonyremote.camera.verify_serial_at_port",lambda model,port,serial:{"serial":serial,"port":port})
    result,code=probe.run_probe(args)
    assert live==baseline and result["publication"]=="quarantined" and not result["attribution_verified"]
    if old_ram:
        assert code==2 and result["status"]=="prior_ram_unresolved_no_trigger"
        assert not connections and len(commands)==1
    else:
        assert result["restore"]=="verified" and len(commands)==2
        assert sum("--trigger-capture" in cmd for cmd in commands)==1
        assert result["readback_and_exif_verified"] is (not wrong_exif)
        assert code==(2 if wrong_exif else 0)
        assert result["jpeg_receipts"][0]["sha256"]


def test_timeout_defers_restore_and_never_retriggers(tmp_path,monkeypatch):
    import sonyremote,engine,subprocess
    baseline={"iso":"6400","aperture":"f/5.6","shutter":"1/500","focus_mode":"Automatic"}
    live=dict(baseline);connections=[];commands=[];clock={"now":0}
    class Camera:
        def __init__(self,**kwargs):connections.append(kwargs)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def info(self):return SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003")
        def shooting_mode(self):return "M"
        def get_settings(self):return dict(live)
        def set_exposure(self,**kwargs):live.update(kwargs)
        def set_setting(self,key,value):live[key]=value
    def run(argv,**kwargs):
        commands.append(argv)
        if "--trigger-capture" in argv:
            raise subprocess.TimeoutExpired(argv,90)
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    monkeypatch.setattr(sonyremote,"Camera",Camera)
    monkeypatch.setattr(engine,"_free_usb",lambda:None)
    monkeypatch.setattr(probe.shutil,"which",lambda _:"/fake/gphoto2")
    monkeypatch.setattr(probe.subprocess,"run",run)
    monkeypatch.setattr(probe.time,"sleep",lambda _:pytest.fail("timeout must not auto-wait and restore"))
    args=SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003",shutter="30",aperture="f/5.6",execute=True,out=str(tmp_path),serial="A7II00SERIAL")
    monkeypatch.setattr("sonyremote.camera.verify_serial_at_port",lambda model,port,serial:{"serial":serial,"port":port})
    result,code=probe.run_probe(args)
    assert code==2 and result["restore"]=="deferred_until_physical_idle_verified"
    assert result["recommended_wait_before_readback_s"]==65
    assert result["baseline"]==baseline
    assert live["shutter"]=="30" and live["focus_mode"]=="Manual"
    assert len(connections)==1 and len(commands)==2


def test_drain_error_with_zero_exit_code_still_refuses_trigger(tmp_path,monkeypatch):
    import sonyremote,engine
    commands=[]
    monkeypatch.setattr(sonyremote,"Camera",lambda **kw:pytest.fail("failed drain cannot advance to camera writes"))
    monkeypatch.setattr(engine,"_free_usb",lambda:None)
    monkeypatch.setattr(probe.shutil,"which",lambda _:"/fake/gphoto2")
    def run(argv,**kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0,stdout="ERROR: Could not wait for event.",stderr="")
    monkeypatch.setattr(probe.subprocess,"run",run)
    args=SimpleNamespace(model="Sony ILCE-7M2 (Control)",port="usb:000,003",shutter="30",aperture="f/5.6",execute=True,out=str(tmp_path),serial="A7II00SERIAL")
    monkeypatch.setattr("sonyremote.camera.verify_serial_at_port",lambda model,port,serial:{"serial":serial,"port":port})
    result,code=probe.run_probe(args)
    assert code==2 and result["status"]=="prior_ram_unresolved_no_trigger"
    assert not result["trigger_attempted"] and len(commands)==1
