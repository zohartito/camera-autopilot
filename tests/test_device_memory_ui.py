"""The phone remembers its token and camera across an app kill, and can forget them.

Evidence (2026-09-17): a cold launch of com.zohar.autopilot on the physical iPhone produced
`GET /status 401` because the token lived in sessionStorage, which the WebView drops when the
app is killed. The server side is untouched: the token still gates every request.
"""
import json
import re
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "backend/web"
CLIENT = (WEB / "client.js").read_text()
INDEX = (WEB / "index.html").read_text()
REVIEW = (WEB / "review.html").read_text()
VALIDATION = (WEB / "validation.html").read_text()
PANORAMA = (WEB / "panorama.html").read_text()

FAKE_STORES = """
function memoryStore() {
  const data = new Map();
  return { getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)),
           removeItem: k => data.delete(k), keys: () => [...data.keys()] };
}
function throwingStore() {
  const boom = () => { throw new DOMException('QuotaExceededError'); };
  return { getItem: boom, setItem: boom, removeItem: boom };
}
"""


def run_js(code: str) -> str:
    script = "const assert=require('node:assert/strict');\n" + FAKE_STORES + code
    return subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout


def test_token_and_camera_survive_a_simulated_app_restart() -> None:
    out = run_js("""
    const disk = memoryStore();                       // localStorage: survives the kill
    let window = { localStorage: disk, sessionStorage: memoryStore() };
    """ + CLIENT + """
    const store = window.Autopilot.deviceStore;
    store.write(store.TOKEN_KEY, 'a'.repeat(48));
    store.write(store.CAMERA_KEY, JSON.stringify({model: 'Sony ILCE-7M3', port: 'usb:001,005'}));
    store.write(store.ACTIVE_SESSION_KEY, 'f'.repeat(32), 'tab');
    // Sibling pages in the same run still find the token where they used to look.
    assert.equal(window.sessionStorage.getItem(store.TOKEN_KEY), 'a'.repeat(48));
    // Kill the app: a fresh WebView keeps localStorage and starts with empty sessionStorage.
    window = { localStorage: disk, sessionStorage: memoryStore() };
    """ + CLIENT + """
    const reborn = window.Autopilot.deviceStore;
    process.stdout.write(JSON.stringify({
      token: reborn.read(reborn.TOKEN_KEY),
      mirrored: window.sessionStorage.getItem(reborn.TOKEN_KEY),
      camera: reborn.read(reborn.CAMERA_KEY),
      session: reborn.read(reborn.ACTIVE_SESSION_KEY, 'tab'),
      sessionOnDisk: disk.getItem(reborn.ACTIVE_SESSION_KEY),
    }));
    """)
    result = json.loads(out)
    assert result["token"] == "a" * 48
    # Reading the durable copy re-mirrors it, so /panorama and /validation (sessionStorage) work too.
    assert result["mirrored"] == "a" * 48
    assert json.loads(result["camera"]) == {"model": "Sony ILCE-7M3", "port": "usb:001,005"}
    # The active session id is per tab: it does not come back after a restart and never hits disk.
    assert result["session"] == "" and result["sessionOnDisk"] is None


def test_forget_clears_token_camera_session_and_baseline_everywhere() -> None:
    out = run_js("""
    const window = { localStorage: memoryStore(), sessionStorage: memoryStore() };
    """ + CLIENT + """
    const store = window.Autopilot.deviceStore;
    store.write(store.TOKEN_KEY, 'b'.repeat(48));
    store.write(store.CAMERA_KEY, '{"model":"x","port":"usb:1,2"}');
    store.write(store.ACTIVE_SESSION_KEY, 'c'.repeat(32), 'tab');
    store.write(store.BASELINE_KEY, '{"settings":{"iso":"200"}}');
    store.write(store.AUTO_SAVE_PHOTOS_KEY, '1');
    window.localStorage.setItem('seen-hint', '1');
    const forgotten = store.forget();
    process.stdout.write(JSON.stringify({forgotten,
      local: window.localStorage.keys(), session: window.sessionStorage.keys(),
      token: store.read(store.TOKEN_KEY), camera: store.read(store.CAMERA_KEY),
      active: store.read(store.ACTIVE_SESSION_KEY, 'tab')}));
    """)
    result = json.loads(out)
    assert set(result["forgotten"]) == {"camera-autopilot-token", "camera-autopilot-selected-camera",
                                        "camera-autopilot-active-session", "camera-autopilot-body-baseline",
                                        "camera-autopilot-auto-save-photos"}
    assert result["local"] == ["seen-hint"] and result["session"] == []
    assert result["token"] == "" and result["camera"] == "" and result["active"] == ""


def test_storage_that_throws_or_is_missing_never_breaks_the_page() -> None:
    out = run_js("""
    const window = { localStorage: throwingStore(), sessionStorage: throwingStore() };
    """ + CLIENT + """
    const store = window.Autopilot.deviceStore;
    assert.equal(store.write(store.TOKEN_KEY, 'x'.repeat(24)), false);
    assert.equal(store.read(store.TOKEN_KEY), '');
    store.remove(store.TOKEN_KEY); store.forget();
    const bare = {};
    Object.defineProperty(bare, 'localStorage', { get() { throw new Error('SecurityError'); } });
    const window2 = bare; { const window = window2; """ + CLIENT + """
      assert.equal(window.Autopilot.deviceStore.read('camera-autopilot-token'), '');
      assert.equal(window.Autopilot.deviceStore.write('camera-autopilot-token', 'y'.repeat(24)), false); }
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_pages_read_the_token_from_device_memory_and_expose_forget() -> None:
    # validation.html has no per-tab state, so like index/review its token is the only local
    # storage it touches; once on deviceStore it must hold no raw sessionStorage at all.
    for page in (INDEX, REVIEW, VALIDATION):
        assert "sessionStorage" not in page, "raw sessionStorage access bypasses the guarded store"
        assert "Autopilot.deviceStore.read(TOKEN_KEY)" in page
    assert 'id="forget-device"' in INDEX
    handler = INDEX[INDEX.index("$('forget-device').onclick"):INDEX.index("$('device-refresh').onclick")]
    assert "Autopilot.deviceStore.forget()" in handler
    for cleared in ("apiToken=''", "activeSession=null", "bodyBaseline=null"):
        assert cleared in handler
    # Camera choice and the active session go through the same guarded store; the session is per tab.
    assert "Autopilot.deviceStore.write(Autopilot.deviceStore.CAMERA_KEY" in INDEX
    assert "Autopilot.deviceStore.read(Autopilot.deviceStore.ACTIVE_SESSION_KEY, 'tab')" in INDEX
    assert "Autopilot.deviceStore.write(Autopilot.deviceStore.ACTIVE_SESSION_KEY,state.id,'tab')" in INDEX
    # panorama.html keeps genuine per-tab state (pending build, local guide) in sessionStorage, so
    # it cannot join the strict no-sessionStorage assertion — but its token must go through the
    # guarded store, never raw sessionStorage, and it must load client.js to have the helper.
    assert '<script src="/client.js?v=20260923"></script>' in PANORAMA
    assert "Autopilot.deviceStore.read(tokenKey)" in PANORAMA
    assert "Autopilot.deviceStore.write(tokenKey,token)" in PANORAMA
    assert "sessionStorage.getItem(tokenKey)" not in PANORAMA
    assert "sessionStorage.setItem(tokenKey" not in PANORAMA


def test_remember_token_is_best_effort_and_still_sets_the_header() -> None:
    remember = re.search(r"  function rememberToken\([^\n]*\) \{.*?\n  \}", INDEX, re.S).group()
    header = re.search(r"  function apiHeaders\([^\n]*\) \{.*?\n  \}", INDEX, re.S).group()
    out = run_js("let apiToken=''; const TOKEN_KEY='camera-autopilot-token';\n" + remember + "\n" + header + """
    // No Autopilot helper at all (storage unavailable): the token must still reach the header.
    rememberToken('  ' + 'z'.repeat(30) + '  ');
    process.stdout.write(JSON.stringify(apiHeaders({})));
    """)
    assert json.loads(out) == {"X-Camera-Autopilot-Token": "z" * 30}


def test_token_never_appears_in_a_url_or_console() -> None:
    for name, page in (("index", INDEX), ("review", REVIEW), ("client", CLIENT)):
        assert not re.search(r"console\.\w+\([^)]*apiToken", page), name
        assert not re.search(r"[?&]token=", page), name
