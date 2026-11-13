"""Execute the browser's real request/error helpers against lost-response fixtures."""
from pathlib import Path
import re
import subprocess

import pytest

INDEX = Path(__file__).resolve().parents[1] / "backend/web/index.html"


def browser_helpers() -> str:
    source = INDEX.read_text()
    names = ["rememberToken", "apiHeaders", "pairWithCode", "apiFetch", "apiError", "cameraFailureMessage", "captureRequest", "takePhoto", "recordVideo"]
    pairing_code = re.search(r"  const PAIRING_CODE = [^\n]*", source).group()
    return "\n".join([pairing_code] + [re.search(r"  (?:async )?function " + name + r"\([^\n]*\) \{.*?\n  \}", source, re.S).group() for name in names])


@pytest.mark.parametrize("operation", ["shoot", "record"])
@pytest.mark.parametrize("failure", ["network", "unreadable_success"])
def test_capture_response_loss_does_not_invite_retry(operation: str, failure: str) -> None:
    script = browser_helpers() + f"""
    const assert = require('node:assert/strict');
    let apiToken='', cameraSelectionRequired=false;
    const availableCameras=[{{model:'Sony fixture',port:'usb:fixture',serial:'A7III0SERIAL'}}];
    const $ = id => id==='camera-select' ? {{value:'0'}} : {{checked:false}};
    let calls=0;
    const fetch=async()=>{{calls++; {'throw new TypeError("Failed to fetch");' if failure == 'network' else 'return new Response("incomplete JSON",{status:200});'}}};
    (async()=>{{
      let failure;
      try {{await {"takePhoto" if operation == "shoot" else "recordVideo"}('fixture');}} catch(e) {{failure=e;}}
      assert.ok(failure, 'Response loss must surface');
      assert.equal(failure.operation, '{operation}');
      assert.equal(failure.outcomeUncertain, true);
      assert.equal(calls,1,'No automatic retry after an ambiguous action');
      const message=cameraFailureMessage(failure);
      assert.match(message, /Do not retry/);
      assert.doesNotMatch(message, /try again/i);
      assert.match(message, {r'/Check Review and the camera card/' if operation == 'shoot' else r'/actual camera recording state and its card/'});
    }})().catch(e=>{{console.error(e);process.exitCode=1;}});
    """
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_401_before_action_keeps_authenticated_retry() -> None:
    script = browser_helpers() + """
    const assert=require('node:assert/strict');
    let apiToken='', cameraSelectionRequired=false;
    const TOKEN_KEY='fixture'; const sessionStorage={setItem(){}};
    const window={prompt:()=> 'fixture-token'};
    let calls=0;
    const fetch=async()=> ++calls===1 ? new Response('{}',{status:401}) : new Response('{"image":"fixture.jpg"}',{status:200});
    (async()=>{
      const result=await captureRequest('/shoot',{},'shoot');
      assert.equal(result.image,'fixture.jpg');
      assert.equal(calls,2);
      assert.equal(apiToken,'fixture-token');
    })().catch(e=>{console.error(e);process.exitCode=1;});
    """
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
