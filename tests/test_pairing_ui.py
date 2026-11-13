"""The phone offers "Pair with code" next to the token paste and stores the token where a paste goes.

The functions are lifted verbatim from index.html and run in node against a fake fetch, like
test_device_memory_ui.py. The code travels only in a JSON POST body to /api/pair, never a URL.
"""
import json
import re
import subprocess
from pathlib import Path

INDEX = (Path(__file__).resolve().parents[1] / "backend/web/index.html").read_text()


def _block(pattern: str) -> str:
    return re.search(pattern, INDEX, re.S).group()


PAIRING = "\n".join([
    _block(r"  const PAIRING_CODE = [^\n]*"),
    _block(r"  function rememberToken\([^\n]*\) \{.*?\n  \}"),
    _block(r"  function apiHeaders\([^\n]*\) \{.*?\n  \}"),
    _block(r"  async function pairWithCode\([^\n]*\) \{.*?\n  \}"),
    _block(r"  async function apiFetch\([^\n]*\) \{.*?\n  \}"),
])

HARNESS = """
const calls = [];
let apiToken = '';
const stored = {};
const Autopilot = { deviceStore: { write: (key, value) => { stored[key] = value; } } };
const TOKEN_KEY = 'camera-autopilot-token';
let replies = [];
async function fetch(url, options = {}) {
  calls.push({ url, method: options.method || 'GET', headers: options.headers || {}, body: options.body });
  const reply = replies.shift() || { status: 200, body: {} };
  return { status: reply.status, ok: reply.status >= 200 && reply.status < 300,
           json: async () => reply.body };
}
let promptAnswer = null;
const window = { prompt: () => promptAnswer };
"""


def run_js(code: str) -> dict:
    script = HARNESS + PAIRING + "\n(async () => {\n" + code + "\n})().catch(e => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_pair_control_is_a_numeric_six_digit_input_next_to_forget() -> None:
    control = re.search(r'<input id="pair-code"[^>]*>', INDEX).group()
    for attribute in ('inputmode="numeric"', 'maxlength="6"', 'autocomplete="one-time-code"'):
        assert attribute in control
    assert '<button id="pair-device" class="ghost" type="button">Pair with code</button>' in INDEX
    panel = INDEX[INDEX.index('<details id="device-panel"'):INDEX.index('id="device-health"')]
    assert 'id="forget-device"' in panel and 'id="pair-code"' in panel
    handler = INDEX[INDEX.index("$('pair-device').onclick"):INDEX.index("$('forget-device').onclick")]
    assert "pairWithCode(code)" in handler and "$('pair-code').value=''" in handler


def test_pairing_posts_the_code_as_json_and_stores_the_returned_token() -> None:
    result = run_js("""
    replies = [{ status: 200, body: { token: 't'.repeat(64) } }];
    const status = await pairWithCode(' 123 456 ');
    process.stdout.write(JSON.stringify({ status, calls, apiToken, stored }));
    """)
    assert result["status"] == 200
    assert result["calls"] == [{"url": "/api/pair", "method": "POST",
                                "headers": {"Content-Type": "application/json"},
                                "body": json.dumps({"code": "123456"}, separators=(",", ":"))}]
    assert result["apiToken"] == "t" * 64
    assert result["stored"] == {"camera-autopilot-token": "t" * 64}


def test_bad_codes_and_refusals_store_nothing() -> None:
    result = run_js("""
    const skipped = [await pairWithCode('12345'), await pairWithCode('abcdef'), await pairWithCode('')];
    const before = calls.length;
    replies = [{ status: 401, body: { detail: 'pairing code not accepted' } }, { status: 429, body: {} },
               { status: 200, body: {} }];
    const refused = [await pairWithCode('000000'), await pairWithCode('000000'), await pairWithCode('000000')];
    process.stdout.write(JSON.stringify({ skipped, before, refused, apiToken, stored }));
    """)
    assert result["skipped"] == [0, 0, 0] and result["before"] == 0
    assert result["refused"] == [401, 429, 0]
    assert result["apiToken"] == "" and result["stored"] == {}


def test_the_401_prompt_accepts_a_pairing_code_or_a_pasted_token() -> None:
    result = run_js("""
    promptAnswer = '654321';
    replies = [{ status: 401, body: {} }, { status: 200, body: { token: 'p'.repeat(64) } },
               { status: 200, body: { ok: true } }];
    const paired = await apiFetch('/gallery');
    const pairedCalls = calls.splice(0);
    promptAnswer = 'x'.repeat(48);
    apiToken = '';
    replies = [{ status: 401, body: {} }, { status: 200, body: {} }];
    await apiFetch('/gallery');
    process.stdout.write(JSON.stringify({ paired: paired.status, pairedCalls, pastedCalls: calls, apiToken }));
    """)
    assert result["paired"] == 200
    assert [call["url"] for call in result["pairedCalls"]] == ["/gallery", "/api/pair", "/gallery"]
    assert result["pairedCalls"][2]["headers"] == {"X-Camera-Autopilot-Token": "p" * 64}
    # A pasted 48-character token still works exactly as before; it never goes to /api/pair.
    assert [call["url"] for call in result["pastedCalls"]] == ["/gallery", "/gallery"]
    assert result["apiToken"] == "x" * 48


def test_code_and_token_never_appear_in_a_url_or_console() -> None:
    assert not re.search(r"/api/pair\?", INDEX)
    assert not re.search(r"console\.\w+\([^)]*(apiToken|code|payload)", INDEX)
