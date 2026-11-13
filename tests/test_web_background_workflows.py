"""Execute browser workflow logic against camera-free network and pixel fixtures."""
from pathlib import Path
import re
import subprocess

import pytest


WEB = Path(__file__).resolve().parents[1] / "backend/web"


def run_js(code: str) -> None:
    subprocess.run(["node", "-e", "const assert=require('node:assert/strict');\n" + code],
                   check=True, capture_output=True, text=True)


def function(name: str) -> str:
    text = (WEB / "index.html").read_text()
    return re.search(r"  (?:async )?function " + name + r"\([^\n]*\) \{.*?\n  \}", text, re.S).group()


def test_histogram_and_clipping_are_measured_from_received_pixels() -> None:
    run_js("const window={};\n" + (WEB / "workflows.js").read_text() + """
      const output=window.AutopilotWorkflows.analyzePixels(new Uint8ClampedArray([
        0,0,0,255, 255,255,255,255, 255,0,0,255, 128,128,128,255
      ]));
      assert.equal(output.histogram.reduce((a,b)=>a+b),4);
      assert.equal(output.histogram[0],1);
      assert.equal(output.histogram[255],1);
      assert.equal(output.histogram[54],1);
      assert.equal(output.histogram[128],1);
      assert.equal(output.clippedPercent,50);
      assert.equal(window.AutopilotWorkflows.analyzePixels([]).clippedPercent,0);
      assert.deepEqual(window.AutopilotWorkflows.thirdsGridFractions(),[1/3,2/3]);
      assert.equal(window.AutopilotWorkflows.zebraAt(0,0,255,255,255),true);
      assert.equal(window.AutopilotWorkflows.zebraAt(6,0,255,255,255),false);
      assert.equal(window.AutopilotWorkflows.zebraAt(0,0,10,10,10),false);
      assert.deepEqual(window.AutopilotWorkflows.displaySize({naturalWidth:3200,naturalHeight:1600},1600),{width:1600,height:800,scale:0.5});
      assert.deepEqual(window.AutopilotWorkflows.containedDrawRect(375,812,480,320),{x:0,y:281,width:375,height:250,scale:375/480});
      assert.deepEqual(window.AutopilotWorkflows.containedDrawRect(375,812,320,480),{x:0,y:125,width:375,height:563,scale:375/320});
      assert.match(window.AutopilotWorkflows.lastFrameAidsCaption({width:6000,height:4000,clippedPercent:12.5}),/Last captured frame — not a live view/);
      assert.equal(window.AutopilotWorkflows.cardFileViewable('DSC0001.JPG'),true);
      assert.equal(window.AutopilotWorkflows.cardFileViewable('DSC0001.ARW'),false);
    """)


def test_lost_submit_response_only_starts_read_only_reconciliation() -> None:
    run_js(function("runSession") + """
      let submissions=0,watched=[],locked=[];
      const elements={};const $=id=>elements[id] || (elements[id]={});
      const sessionBusy=value=>locked.push(value);
      const sessionRequest=async(path,body)=>{submissions++;assert.equal(path,'/sessions/fixture/jobs');assert.deepEqual(body,{delay_seconds:0});throw new TypeError('Lost response');};
      const watchSessionJob=id=>watched.push(id);
      (async()=>{
        await runSession('fixture');
        assert.equal(submissions,1);
        assert.deepEqual(watched,['fixture']);
        assert.deepEqual(locked,[true]);
        assert.match(elements['session-status'].textContent,/Checking the existing session/);
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_job_poll_connection_failure_keeps_camera_locked_without_post() -> None:
    run_js(function("pollSessionJob") + """
      let activeSession='fixture',jobPollInFlight=false,jobPollTimer=null;
      let calls=0,scheduled=0,locked=[];
      const elements={};const $=id=>elements[id] || (elements[id]={});
      const sessionBusy=value=>locked.push(value);
      const sessionRequest=async(path,body)=>{calls++;assert.equal(body,undefined);throw new TypeError('Offline');};
      const setTimeout=(callback,delay)=>{scheduled++;assert.equal(delay,1500);return 1;};
      (async()=>{
        await pollSessionJob('fixture');
        assert.equal(calls,1);assert.equal(scheduled,1);
        assert.deepEqual(locked,[true]);
        assert.equal(jobPollInFlight,false);
        assert.match(elements['session-status'].textContent,/no new camera action/);
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_recovery_required_hides_resume_when_retry_is_unsafe() -> None:
    run_js(function("pollSessionJob") + """
      let activeSession='fixture',jobPollInFlight=false,jobPollTimer=null;
      const activeJobStates=['queued','running'];let locked=[];
      const elements={'session-status':{textContent:''}};const $=id=>elements[id] || (elements[id]={});
      const sessionBusy=value=>locked.push(value);
      const showSession=state=>assert.equal(state.id,'fixture');
      const sessionRequest=async(path,body)=>{
        assert.equal(body,undefined);
        return {status:'recovery_required',retry_safe:false,error:'Interrupted shutter',session:{id:'fixture'}};
      };
      (async()=>{
        await pollSessionJob('fixture');
        assert.deepEqual(locked,[false]);
        assert.equal(elements['session-resume'].hidden,true);
        assert.match(elements['session-status'].textContent,/No automatic recapture/);
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_new_session_defaults_match_server_frame_and_exposure_limits() -> None:
    source=(WEB / "index.html").read_text()
    assert 'id="session-frames" class="prompt" type="number" min="1" max="100"' in source
    assert "'/sessions/'+id+'/run'" not in source
    assert "state.video?.status==='completed' && state.video?.url" in source
    assert "liveFrame.decode()" in source
    assert "No still-photo fallback was requested" in source


@pytest.mark.parametrize("baseline", ["metered", "current"])
def test_explicit_stack_policy_and_exact_camera_reach_capture_request(baseline) -> None:
    run_js(function("takePhoto") + function("stackBaselineSource") + """
      let cameraSelectionRequired=false;
      const availableCameras=[{model:'Exact fixture camera',port:'usb:exact',serial:'A7III0SERIAL'}];
      const values={'camera-select':{value:'0'},refine:{checked:false},'stack-policy':{value:'fixed'},'stack-mode':{value:'night'},'stack-frames':{value:'5'},'stack-baseline-source':{value:'""" + baseline + """'}};
      const $=id=>values[id];
      const captureRequest=async(path,body,operation)=>{
        assert.equal(path,'/shoot');assert.equal(operation,'shoot');
        assert.deepEqual(body.stack_policy,{mode:'fixed',stack_mode:'night',frames:5,baseline:'""" + baseline + """'});
        assert.equal(body.refine,false);assert.equal(body.camera_port,'usb:exact');assert.equal(body.camera_serial,'A7III0SERIAL');
        return body;
      };
      takePhoto('fixture').catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_delayed_submit_sends_the_explicit_delay_once() -> None:
    run_js(function("runSession") + """
      let count=0;const noted=[];
      const $=id=>id==='session-delay'?{value:'90'}:{};
      const sessionBusy=()=>{};const watchSessionJob=id=>assert.equal(id,'fixture');
      const noteSessionCameraWarning=(id,job)=>noted.push([id,job]);
      const sessionRequest=async(path,body)=>{count++;assert.deepEqual(body,{delay_seconds:90});return {status:'queued'};};
      (async()=>{await runSession('fixture');assert.equal(count,1);
        // The job answer reached the warning note: the submit did not take the failure path.
        assert.deepEqual(noted,[['fixture',{status:'queued'}]]);})().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_smartlapse_invalid_frame_budget_is_rejected_before_submission() -> None:
    run_js(function("executionBudgetSeconds") + function("timelapseControls") + """
      const values={'timelapse-policy':'holy_grail','timelapse-priority':'shutter_then_iso','timelapse-min-iso':100,'timelapse-max-iso':6400,'timelapse-min-shutter':.000125,'timelapse-max-shutter':30,'timelapse-step':.3,'timelapse-preview-every':2,'smartlapse-duration':300,'smartlapse-output':11,'timelapse-fps':24};
      const $=id=>id==='smartlapse-enabled'?{checked:true}:{value:values[id]};
      assert.throws(()=>timelapseControls(true),/2–240 movie frames/);
      values['smartlapse-output']=2;
      const result=timelapseControls(true);
      assert.deepEqual(result.smartlapse,{duration_s:300,output_s:2,fps:24});
      assert.equal(timelapseControls(false).smartlapse,undefined,'Midrun bounds cannot replan source frames');
    """)


def test_smartlapse_duration_follows_the_execution_budget_of_the_policy() -> None:
    run_js(function("executionBudgetSeconds") + function("timelapseControls") + """
      const values={'timelapse-policy':'holy_grail','timelapse-priority':'shutter_then_iso','timelapse-min-iso':100,'timelapse-max-iso':6400,'timelapse-min-shutter':.000125,'timelapse-max-shutter':30,'timelapse-step':.3,'timelapse-preview-every':2,'smartlapse-duration':10800,'smartlapse-output':5,'timelapse-fps':24};
      const $=id=>id==='smartlapse-enabled'?{checked:true}:{value:values[id]};
      assert.equal(timelapseControls(true).smartlapse.duration_s,10800);
      values['smartlapse-duration']=10801;
      assert.throws(()=>timelapseControls(true),/up to 10800 seconds \\(the 3-hour Holy Grail/);
      values['timelapse-policy']='auto_custom';values['smartlapse-duration']=3601;
      assert.throws(()=>timelapseControls(true),/up to 3600 seconds \\(the 1-hour session/);
      // The server keeps SmartLapse frames at most one hour apart: 3 h over 2 frames is refused here,
      // before the session is created, and 4 frames (exactly 1 h apart) is accepted.
      values['timelapse-policy']='holy_grail';values['smartlapse-duration']=10800;values['smartlapse-output']=2;values['timelapse-fps']=1;
      assert.throws(()=>timelapseControls(true),/at most one hour apart: 4 or more movie frames/);
      values['smartlapse-output']=4;
      assert.deepEqual(timelapseControls(true).smartlapse,{duration_s:10800,output_s:4,fps:1});
    """)


def test_session_status_reports_budget_used_and_remaining() -> None:
    run_js(function("executionBudgetText") + """
      assert.equal(executionBudgetText(undefined),'');
      assert.equal(executionBudgetText({budget_s:10800,applies:'holy_grail_timelapse',used_s:1800,remaining_s:9000,exhausted:false}),
        ' · Execution budget (Holy Grail, 3 h for this session, counted across resumes): 30 min used, 150 min left.');
      assert.match(executionBudgetText({budget_s:3600,applies:'session',used_s:3700,remaining_s:0,exhausted:true}),
        /\\(session, 1 h .*62 min used, 0 min left; spent, so this session captures no more frames\\./);
    """)


def test_holy_grail_shows_deflicker_on_by_default_and_sends_an_explicit_off() -> None:
    run_js(function("applyPolicyDeflickerDefault") + function("deflickerOption") + """
      const el={'session-mode':{value:'timelapse'},'timelapse-policy':{value:'holy_grail'},
        'timelapse-deflicker':{checked:false,onchange(){el['timelapse-deflicker'].shown=el['timelapse-deflicker'].checked;}},
        'timelapse-deflicker-window':{value:'5'},'timelapse-deflicker-gain':{value:'2'}};
      const $=id=>el[id];
      applyPolicyDeflickerDefault();
      assert.equal(el['timelapse-deflicker'].checked,true);
      assert.equal(el['timelapse-deflicker'].shown,true,'options panel follows the default');
      assert.deepEqual(deflickerOption(),{deflicker:{enabled:true,window:7,max_gain_stops:1}});
      el['timelapse-deflicker'].checked=false;
      assert.deepEqual(deflickerOption(),{deflicker:{enabled:false}},'unchecking Holy Grail must override the server default');
      el['timelapse-policy'].value='auto_custom';applyPolicyDeflickerDefault();
      assert.equal(el['timelapse-deflicker'].checked,false);
      assert.deepEqual(deflickerOption(),{},'other policies stay off and carry no option');
      el['timelapse-policy'].value='holy_grail';applyPolicyDeflickerDefault();el['session-mode'].value='hdr';
      assert.equal(el['timelapse-deflicker'].checked,true);
      assert.deepEqual(deflickerOption(),{},'a non-timelapse session never carries the option, even with the box checked');
      el['timelapse-deflicker'].checked=false;
      assert.deepEqual(deflickerOption(),{},'nor the explicit off');
    """)


def test_duration_window_is_optional_and_scoped_to_long_exposure() -> None:
    run_js(function("longExposureDuration") + """
      const values={'session-mode':{value:'long_exposure'},'long-exposure-duration':{value:''},'session-frames':{value:'3'}};
      const $=id=>values[id];
      assert.equal(longExposureDuration(),undefined);
      values['long-exposure-duration'].value='20';assert.equal(longExposureDuration(),20);
      values['session-mode'].value='timelapse';assert.equal(longExposureDuration(),undefined);
      values['session-mode'].value='long_exposure';values['session-frames'].value='17';
      assert.throws(()=>longExposureDuration(),/between 2 and 16/);
      values['session-frames'].value='3';values['long-exposure-duration'].value='3601';
      assert.throws(()=>longExposureDuration(),/between 1 and 3600/);
    """)


def test_duration_controls_bound_budget_and_remove_added_interval() -> None:
    run_js(function("showLongExposureOptions") + """
      const values={'session-mode':{value:'long_exposure'},'long-exposure-duration':{value:'20'},'session-frames':{value:'80'},'session-interval':{value:'4'},'long-exposure-options':{},'session-frames-label':{}};
      const $=id=>values[id];showLongExposureOptions();
      assert.equal(values['session-frames'].value,16);
      assert.equal(values['session-frames'].max,'16');
      assert.equal(values['session-interval'].value,'0');assert.equal(values['session-interval'].disabled,true);
      values['session-mode'].value='timelapse';showLongExposureOptions();
      assert.equal(values['session-frames'].max,'100');assert.equal(values['session-interval'].disabled,false);
    """)


def test_device_telemetry_never_invents_level_battery_or_radio_state() -> None:
    run_js("const window={};\n" + (WEB / "workflows.js").read_text() + """
      const rows=Object.fromEntries(window.AutopilotWorkflows.healthRows({platform:'Darwin',battery:null,temperature:null,free_bytes:0},'http://controller'));
      assert.match(rows.Battery,/Unavailable/);
      assert.match(rows['Level / orientation'],/does not assume the camera is level/);
      assert.match(rows['Access point'],/unverified/);
      assert.equal(rows['Free storage'],'0.0 GiB');
      const measured=Object.fromEntries(window.AutopilotWorkflows.healthRows({orientation:{available:true,roll_deg:0,pitch_deg:1.5,source:'fixture IMU'},radio:{supported_bands:['2.4 GHz'],configured_band:'5 GHz'}},'http://controller'));
      assert.match(measured['Level / orientation'],/Roll 0.0°/);
      assert.equal(measured['Configured radio band'],'Unavailable');
      assert.match(measured['Access point'],/unverified/);
    """)


def test_manual_apply_requires_an_explicit_plan_for_the_current_exact_camera() -> None:
    run_js(function("manualApplyAllowed") + """
      const availableCameras=[{model:'Sony ILCE-7M2 (Control)',port:'usb:1',serial:'A7II00SERIAL'},
        {model:'Sony Alpha-A7 III (PC Control)',port:'usb:2',serial:'A7III0SERIAL'},
        {model:'Sony ILCE-7M2 (Control)',port:'usb:1'}];
      let selected='0';const $=()=>({value:selected});let manualPayload={value:200};
      let manualPlan={can_attempt_live_write:true};assert.equal(manualApplyAllowed(),false);
      manualPlan={identity_bound_live_write:true,camera_identity:{explicit_selection:true,model:'Sony ILCE-7M2 (Control)',port:'usb:1',serial:'A7II00SERIAL'}};
      assert.equal(manualApplyAllowed(),true);selected='1';assert.equal(manualApplyAllowed(),false);
      selected='2';assert.equal(manualApplyAllowed(),false);  // same model and port, no serial
      selected='0';manualPlan.camera_identity.serial='A7III0SERIAL';assert.equal(manualApplyAllowed(),false);
      manualPlan.camera_identity.serial='A7II00SERIAL';manualPayload=null;assert.equal(manualApplyAllowed(),false);
    """)


def test_development_download_rejects_unowned_paths_and_oversized_buffers() -> None:
    run_js("const window={};\n" + (WEB / "workflows.js").read_text() + """
      let calls=0;
      const fetch=async()=>{calls++;return new Response('fixture',{headers:{'Content-Length':String(257*1024*1024)}});};
      (async()=>{
        await assert.rejects(window.AutopilotWorkflows.downloadDevelopment(fetch,'https://elsewhere/file.dng'),/Invalid development/);
        assert.equal(calls,0);
        await assert.rejects(window.AutopilotWorkflows.downloadDevelopment(fetch,'/development/'+'a'.repeat(32)+'/output.dng'),/streaming file downloads/);
        assert.equal(calls,1);
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


@pytest.mark.parametrize("policy,settings_hidden,baseline_hidden", [
    ("off", True, True), ("auto", False, True), ("fixed", False, False)])
def test_fixed_baseline_controls_show_only_for_a_fixed_stack(policy, settings_hidden, baseline_hidden) -> None:
    # PR #72 review: Auto kept the Fixed-baseline selector and its "A Fixed stack meters first" text
    # visible although Auto requests never carry a baseline.
    run_js("const window={};\n" + (WEB / "workflows.js").read_text() + "\nconst AutopilotWorkflows=window.AutopilotWorkflows;\n"
           + function("stackBaselineSource") + function("updateCaptureBudget") + """
      const values={'stack-policy':{value:'POLICY'},'stack-frames':{value:'3'},'stack-settings':{hidden:null},
        'stack-baseline-row':{hidden:null},'stack-baseline':{hidden:null},'stack-baseline-source':{value:'metered'},
        refine:{checked:false,disabled:false},'refine-note':{},'capture-count':{}};
      const $=id=>values[id];
      let busy=false, captureMode='photo';
      updateCaptureBudget();
      assert.equal(values['stack-settings'].hidden, SETTINGS);
      assert.equal(values['stack-baseline-row'].hidden, BASELINE);
      assert.equal(values['stack-baseline'].hidden, BASELINE);
    """.replace("POLICY", policy).replace("SETTINGS", str(settings_hidden).lower())
       .replace("BASELINE", str(baseline_hidden).lower()))


def test_stack_panel_visibility_helper() -> None:
    run_js("const window={};\n" + (WEB / "workflows.js").read_text() + """
      const view=window.AutopilotWorkflows.stackPanelVisibility;
      assert.deepEqual(view('off'),{settings:false,fixedBaseline:false});
      assert.deepEqual(view('auto'),{settings:true,fixedBaseline:false});
      assert.deepEqual(view('fixed'),{settings:true,fixedBaseline:true});
    """)
