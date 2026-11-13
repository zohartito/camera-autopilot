"""ImageCapture clients that take the camera's USB (1 Oct 2026, frame 14 of a 200-frame Holy
Grail: Preview relaunched ptpcamerad inside the gphoto2 CLI window and the CLI could not claim
the device). An uncertain CLI capture's receipt names the clients from the unified log; /shoot,
session create and job submit warn when Preview/Photos/Image Capture is running. Camera-free:
the CLI, the unified log and the process list are fakes, and pytest never runs the real ones."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import conftest  # noqa: E402
import host_camera_clients as preflight  # noqa: E402
import sonyremote.camera as camera_module  # noqa: E402
from sonyremote.camera import Camera, CameraError, CameraInfo, CaptureOutcomeUncertain  # noqa: E402

from test_final_wave_product_api import product  # noqa: E402,F401  (pytest fixture import)
from test_section1_api import _real_runner  # noqa: E402

MODEL, PORT, SERIAL = "Sony Alpha-A7 III (PC Control)", "usb:000,004", "A7III0SERIAL"
VERIFIED = {"serial": SERIAL, "method": "os_usb_serial_at_connected_port", "os_source": "ioreg",
            "model": MODEL, "port": PORT}

# The CLI window of the 1 Oct failure (transport receipt) and the unified log's exact lines.
OCT1_CLI = {"started_at": "2026-10-02T02:02:43.553Z", "ended_at": "2026-10-02T02:02:43.905Z"}
OCT1_LOG = (
    "Timestamp               Ty Process[PID:TID]\n"
    "2026-10-01 19:02:43.700 Df ptpcamerad[94824:88797e] [com.apple.imagecapture:ptpcamerad]"
    "         requestStart | Process: com.apple.Preview, Request Order Ascending: NO\n"
    "2026-10-01 19:02:43.701 Df ptpcamerad[94824:88797d] [com.apple.imagecapture:ptpcamerad]"
    "         requestStart | Process: com.apple.Preview, Request Order Ascending: NO\n"
    "2026-10-01 19:02:43.701 Df ptpcamerad[94824:88797f] [com.apple.imagecapture:ptpcamerad]"
    "         requestStart | Process: com.apple.Preview, Request Order Ascending: NO\n")
CLAIM_FAILURE = ("*** Error ***\nAn error occurred in the io-library ('Could not claim the USB device'): "
                 "Could not claim interface 0 (Operation timed out).\n")
PREVIEW = "/System/Applications/Preview.app/Contents/MacOS/Preview"
PHOTOS = "/System/Applications/Photos.app/Contents/MacOS/Photos"
IMAGE_CAPTURE = "/System/Applications/Image Capture.app/Contents/MacOS/Image Capture"
PREVIEW_LINE = "Quit Preview while the camera is in use: it can take the USB connection mid-capture."


class LogRunner:
    """A fake `log show`: records each argv and returns (`output`, truncated) (or raises it)."""

    def __init__(self, output, truncated=False):
        self.output, self.truncated, self.calls = output, truncated, []

    def __call__(self, argv, timeout_s):
        self.calls.append((list(argv), timeout_s))
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output, self.truncated


def _without_pytest_markers(monkeypatch):
    """Let a test call the production runner/lister against a stand-in child or a fake run."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PYTEST_VERSION", raising=False)


# --- the receipt: which client asked ptpcamerad to start around the CLI window ---------------

def test_receipt_names_preview_from_the_exact_1_oct_lines():
    runner = LogRunner(OCT1_LOG)
    result = camera_module.host_camera_clients(OCT1_CLI, runner=runner, platform="darwin")
    assert result == {"status": "ok", "clients": ["com.apple.Preview"], "request_starts": 3,
                      "window": {"start": "2026-10-02 02:02:38+0000", "end": "2026-10-02 02:02:45+0000"},
                      "output_truncated": False}
    # One bounded query, argv only (never a shell string), started_at - 5 s .. ended_at + 1 s.
    [(argv, timeout_s)] = runner.calls
    assert argv == ["/usr/bin/log", "show", "--style", "compact",
                    "--start", "2026-10-02 02:02:38+0000", "--end", "2026-10-02 02:02:45+0000",
                    "--predicate", 'process == "ptpcamerad" AND eventMessage CONTAINS "requestStart"']
    assert timeout_s == camera_module.HOST_LOG_TIMEOUT_S <= 3.0


def test_an_empty_window_is_an_empty_list():
    result = camera_module.host_camera_clients(
        OCT1_CLI, runner=LogRunner("Timestamp               Ty Process[PID:TID]\n"), platform="darwin")
    assert result["status"] == "ok" and result["clients"] == [] and result["request_starts"] == 0


def test_distinct_clients_in_first_seen_order_and_bounded():
    lines = [f"x requestStart | Process: com.example.app{i % 20}, Request Order Ascending: NO" for i in range(60)]
    lines.insert(0, "x requestStart | Process: com.apple.Photos, Request Order Ascending: NO")
    result = camera_module.host_camera_clients(OCT1_CLI, runner=LogRunner("\n".join(lines)), platform="darwin")
    assert result["clients"][0] == "com.apple.Photos"
    assert len(result["clients"]) == camera_module.HOST_LOG_MAX_CLIENTS
    assert camera_module.host_camera_clients(OCT1_CLI, runner=LogRunner(OCT1_LOG, truncated=True),
                                             platform="darwin")["output_truncated"] is True


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["/usr/bin/log"], 3.0),
                                     OSError("no such file"), RuntimeError("log show exited 64")])
def test_a_failed_query_is_unavailable_and_never_raises(failure):
    result = camera_module.host_camera_clients(OCT1_CLI, runner=LogRunner(failure), platform="darwin")
    assert result["status"] == "unavailable" and result["reason"]
    if isinstance(failure, subprocess.TimeoutExpired):
        assert "timed out" in result["reason"]


@pytest.mark.parametrize("cli", [{}, {"started_at": "2026-10-02T02:02:43.553Z", "ended_at": None},
                                 {"started_at": "not a time", "ended_at": "2026-10-02T02:02:43.905Z"}, None])
def test_an_unusable_window_is_unavailable_without_a_query(cli):
    runner = LogRunner(OCT1_LOG)
    assert camera_module.host_camera_clients(cli, runner=runner, platform="darwin")["status"] == "unavailable"
    assert runner.calls == []


def test_off_macos_is_not_applicable_without_a_query():
    runner = LogRunner(OCT1_LOG)
    assert camera_module.host_camera_clients(OCT1_CLI, runner=runner, platform="linux") == {"status": "not_applicable"}
    assert runner.calls == []


def _unpatched(module, name):
    """`module.name` as defined in its source (conftest replaces the attribute with a guard),
    compiled against the module's own globals so its subprocess.run is the patched one."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), module.__file__, "exec"), namespace)  # noqa: S102
    return namespace[name]


def _child(code):
    """argv for a stand-in child process (this interpreter), never the host's log binary."""
    return [sys.executable, "-c", code]


def test_the_production_runner_reads_a_child_and_raises_on_failure(monkeypatch):
    _without_pytest_markers(monkeypatch)
    runner = _unpatched(camera_module, "_run_host_log")
    assert runner(_child(f"import sys; sys.stdout.write({OCT1_LOG!r})"), 3.0) == (OCT1_LOG, False)
    with pytest.raises(RuntimeError, match="exited 64: bad predicate"):
        runner(_child("import sys; sys.stderr.write('bad predicate'); sys.exit(64)"), 3.0)
    # Exactly the cap is complete output, not truncated.
    cap = camera_module.HOST_LOG_MAX_OUTPUT_BYTES
    assert runner(_child(f"import sys; sys.stdout.write('z' * {cap})"), 3.0) == ("z" * cap, False)


def test_the_production_runner_bounds_memory_and_time(monkeypatch):
    import time as real_time
    _without_pytest_markers(monkeypatch)
    runner = _unpatched(camera_module, "_run_host_log")
    cap = camera_module.HOST_LOG_MAX_OUTPUT_BYTES
    # Output that never ends: only the cap is ever held, and the child is killed.
    flood = _child("import sys\nwhile True: sys.stdout.write('x' * 1000000)")
    started = real_time.monotonic()
    output, truncated = runner(flood, 3.0)
    assert len(output) == cap and truncated is True
    assert real_time.monotonic() - started < 3.0
    result = camera_module.host_camera_clients(OCT1_CLI, runner=lambda argv, timeout_s: runner(flood, timeout_s),
                                               platform="darwin")
    assert result["status"] == "ok" and result["output_truncated"] is True
    # Multibyte output past the byte cap decodes to fewer characters, yet is still truncated.
    accents = _child(f"import sys; sys.stdout.buffer.write('\\u00e9'.encode() * {cap})")
    output, truncated = runner(accents, 3.0)
    assert truncated is True and len(output) == cap // 2
    result = camera_module.host_camera_clients(OCT1_CLI, runner=lambda argv, timeout_s: runner(accents, timeout_s),
                                               platform="darwin")
    assert result["output_truncated"] is True
    # A child that hangs (silent, or writing slowly forever) is killed at the deadline.
    for code in ("import time; time.sleep(30)",
                 "import sys, time\nwhile True:\n    sys.stdout.write('y'); sys.stdout.flush(); time.sleep(0.05)"):
        started = real_time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            runner(_child(code), 0.5)
        assert real_time.monotonic() - started < 2.5


@pytest.mark.parametrize("phase", ["test", "collection"])
def test_the_production_runner_and_lister_refuse_in_a_pytest_session_even_after_a_reload(monkeypatch, phase):
    """A module reload (or an import during collection, before any autouse fixture) would bring
    back the production functions over the conftest guards; they still spawn nothing."""
    assert "PYTEST_VERSION" in __import__("os").environ  # set by pytest for the whole session
    if phase == "collection":
        monkeypatch.delenv("PYTEST_CURRENT_TEST")  # what collection looks like: no test running yet
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    with pytest.raises(RuntimeError, match="never read in a pytest session"):
        _unpatched(camera_module, "_run_host_log")(["/usr/bin/log", "show"], 3.0)
    with pytest.raises(RuntimeError, match="never read in a pytest session"):
        _unpatched(preflight, "_list_processes")()
    assert spawned == []


# --- pytest never reads the host's unified log or process list -------------------------------

def test_pytest_never_executes_the_real_log_or_ps(monkeypatch):
    spawned = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def watch_run(argv, *args, **kwargs):
        spawned.append(argv)
        return real_run(argv, *args, **kwargs)

    def watch_popen(argv, *args, **kwargs):
        spawned.append(argv)
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", watch_run)
    monkeypatch.setattr(subprocess, "Popen", watch_popen)
    # The default runner/lister under pytest are the conftest guards, on any host.
    assert camera_module._run_host_log is conftest._never_run_host_log
    assert preflight._list_processes is conftest._never_list_host_processes
    receipt = camera_module.host_camera_clients(OCT1_CLI, platform="darwin")
    assert receipt == {"status": "unavailable",
                       "reason": "RuntimeError: pytest never runs the host's /usr/bin/log (conftest.py)"}
    assert preflight.running_clients(platform="darwin")["status"] == "unavailable"
    assert spawned == []


# --- the receipt on a real uncertain CLI capture ---------------------------------------------

def _capture(tmp_path, run, *, platform="darwin", runner=None, connect=None):
    camera = Camera()
    camera._expected_serial = SERIAL
    camera.quarantine_dir = tmp_path / "store" / "quarantine"
    with patch.object(camera, "info", return_value=CameraInfo(MODEL, PORT)), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/4"}), \
         patch.object(camera, "disconnect"), \
         patch.object(camera, "connect", side_effect=connect or [CameraInfo(MODEL, PORT)]), \
         patch("sonyremote.camera.verify_serial_at_port", return_value=dict(VERIFIED)), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.sys.platform", platform), \
         patch("sonyremote.camera._run_host_log", runner or LogRunner(OCT1_LOG)), \
         patch("sonyremote.camera.subprocess.run", side_effect=run) as fake_run:
        try:
            camera._capture_sony_action(dest_dir=tmp_path / "gallery")
        except CaptureOutcomeUncertain as error:
            return camera, error, fake_run
    return camera, None, fake_run


def _claim_failure(argv, **kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr=CLAIM_FAILURE)


def test_the_1_oct_claim_failure_names_preview_in_its_receipt(tmp_path):
    runner = LogRunner(OCT1_LOG)
    camera, error, fake_run = _capture(tmp_path, _claim_failure, runner=runner)
    assert error is not None and "Could not claim the USB device" in str(error)
    assert error.may_have_fired is True and error.retry_safe is False
    fake_run.assert_called_once()  # one shutter command; the log query is not a subprocess.run here
    receipt = error.transport_receipt
    assert receipt is camera.last_capture_receipt
    assert receipt["host_camera_clients"]["clients"] == ["com.apple.Preview"]
    [(argv, _)] = runner.calls
    # The query window is this attempt's own CLI window.
    assert argv[5] <= receipt["cli"]["started_at"].replace("T", " ")[:19] + "+0000"
    assert "host_log_query_s" in receipt["timings"]


def test_a_timeout_also_names_the_clients(tmp_path):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    _, error, fake_run = _capture(tmp_path, run)
    fake_run.assert_called_once()
    assert error.transport_receipt["host_camera_clients"]["clients"] == ["com.apple.Preview"]


def test_a_failed_log_query_keeps_the_same_uncertain_outcome(tmp_path):
    _, plain, _ = _capture(tmp_path / "a", _claim_failure, platform="linux")
    _, error, fake_run = _capture(tmp_path / "b", _claim_failure,
                                  runner=LogRunner(subprocess.TimeoutExpired(["/usr/bin/log"], 3.0)))
    fake_run.assert_called_once()
    assert type(error) is type(plain) and str(error) == str(plain)
    assert error.may_have_fired is plain.may_have_fired and error.retry_safe is plain.retry_safe
    assert error.transport_receipt["host_camera_clients"]["status"] == "unavailable"
    assert error.transport_receipt["reconnect_ok"] is plain.transport_receipt["reconnect_ok"] is True
    assert plain.transport_receipt["host_camera_clients"] == {"status": "not_applicable"}


def test_an_interrupted_query_never_replaces_the_uncertain_outcome(tmp_path):
    _, error, fake_run = _capture(tmp_path, _claim_failure, runner=LogRunner(KeyboardInterrupt()))
    fake_run.assert_called_once()
    assert isinstance(error, CaptureOutcomeUncertain) and "Could not claim the USB device" in str(error)
    assert error.transport_receipt["host_camera_clients"] == {"status": "unavailable",
                                                              "reason": "interrupted: KeyboardInterrupt"}


def test_an_interrupt_anywhere_in_the_diagnostics_never_replaces_the_uncertain_outcome(tmp_path):
    def interrupted(cli, **kwargs):
        raise KeyboardInterrupt  # past the helper's own guard: the capture's block must hold

    with patch("sonyremote.camera.host_camera_clients", interrupted):
        _, error, fake_run = _capture(tmp_path, _claim_failure)
    fake_run.assert_called_once()
    assert isinstance(error, CaptureOutcomeUncertain) and "Could not claim the USB device" in str(error)
    assert error.transport_receipt["host_camera_clients"] == {"status": "unavailable", "reason": "interrupted"}


def test_a_confirmed_capture_runs_no_query(tmp_path):
    def run(argv, **kwargs):
        (kwargs["cwd"] / "DSC00042.JPG").write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr="", stdout="Saving file as DSC00042.JPG\n")

    runner = LogRunner(OCT1_LOG)
    camera, error, _ = _capture(tmp_path, run, runner=runner)
    assert error is None and "host_camera_clients" not in camera.last_capture_receipt
    assert runner.calls == []


def test_a_refusal_before_the_shutter_runs_no_query(tmp_path):
    runner = LogRunner(OCT1_LOG)
    camera = Camera()
    camera._expected_serial = SERIAL
    camera.quarantine_dir = tmp_path / "quarantine"
    with patch.object(camera, "info", return_value=CameraInfo(MODEL, PORT)), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/4"}), \
         patch.object(camera, "disconnect"), \
         patch.object(camera, "connect", return_value=CameraInfo(MODEL, PORT)), \
         patch("sonyremote.camera.verify_serial_at_port", side_effect=CameraError("bus gone")), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.sys.platform", "darwin"), \
         patch("sonyremote.camera._run_host_log", runner), \
         patch("sonyremote.camera.subprocess.run") as fake_run:
        with pytest.raises(CameraError) as refused:
            camera._capture_sony_action(dest_dir=tmp_path / "gallery")
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    fake_run.assert_not_called()
    assert runner.calls == [] and "host_camera_clients" not in camera.last_capture_receipt


# --- the preflight: running client apps -----------------------------------------------------

def _listing(*paths):
    return "\n".join(["/sbin/launchd", "/usr/libexec/ptpcamerad", *paths]) + "\n"


def test_preflight_lists_a_running_preview():
    result = preflight.running_clients(lister=lambda: _listing(PREVIEW), platform="darwin")
    assert result == {"status": "checked", "clients": ["Preview"]}
    assert preflight.summary_line(result) == PREVIEW_LINE


def test_preflight_names_every_known_client_in_one_line():
    result = preflight.running_clients(lister=lambda: _listing(IMAGE_CAPTURE, PHOTOS, PREVIEW), platform="darwin")
    assert result["clients"] == ["Preview", "Photos", "Image Capture"]
    line = preflight.summary_line(result)
    assert line == ("Quit Preview, Photos and Image Capture while the camera is in use: "
                    "they can take the USB connection mid-capture.")
    assert "\n" not in line


def test_preflight_ignores_lookalikes_and_helpers():
    listing = _listing("/Applications/Preview Pro.app/Contents/MacOS/Preview Pro",
                       "/System/Library/PrivateFrameworks/PhotoLibraryServices.framework/photolibraryd",
                       "/usr/libexec/photoanalysisd", "/Applications/MyPreview.app/Contents/MacOS/MyPreview",
                       "/System/Applications/Preview.app/Contents/XPCServices/QuickLookUIService")
    result = preflight.running_clients(lister=lambda: listing, platform="darwin")
    assert result == {"status": "checked", "clients": []} and preflight.summary_line(result) is None


def test_preflight_off_macos_and_on_failure_never_raises():
    called = []
    assert preflight.running_clients(lister=lambda: called.append(1), platform="linux") == {"status": "not_applicable"}
    assert called == []

    def fails():
        raise subprocess.TimeoutExpired(["/bin/ps"], 2.0)

    result = preflight.running_clients(lister=fails, platform="darwin")
    assert result["status"] == "unavailable" and "timed out" in result["reason"]
    assert preflight.summary_line(result) is None
    assert preflight.summary_line({"status": "not_applicable"}) is None
    assert preflight.summary_line(None) is None


def test_the_production_lister_is_bounded_and_argv_only(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout=_listing(PREVIEW), stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", run)
    _without_pytest_markers(monkeypatch)
    assert _unpatched(preflight, "_list_processes")() == _listing(PREVIEW)
    assert seen["argv"] == ["/bin/ps", "-axo", "comm="] and seen["timeout"] == 2.0 and "shell" not in seen


# --- the API: one warning line, never a refusal, never a different exposure budget ----------

@pytest.fixture
def real_product(product, monkeypatch):
    product.fake.name = "real"
    monkeypatch.setattr(product.app, "_selected_engine", lambda request, **kwargs: product.fake)
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    state = {"listing": _listing()}
    monkeypatch.setattr(preflight, "_list_processes", lambda: state["listing"])
    product.listing = state
    return product


def test_shoot_with_preview_running_adds_evidence_and_exactly_one_line(real_product):
    clean = real_product.client.post("/shoot", headers=real_product.headers, json={"intent": "sunset"})
    assert clean.status_code == 200, clean.text
    real_product.listing["listing"] = _listing(PREVIEW)
    warned = real_product.client.post("/shoot", headers=real_product.headers, json={"intent": "sunset"})
    assert warned.status_code == 200, warned.text
    clean, warned = clean.json(), warned.json()
    assert clean["evidence"]["host_camera_clients"] == {"status": "checked", "clients": []}
    assert clean["summary"] == "Fixture capture"
    assert warned["evidence"]["host_camera_clients"] == {"status": "checked", "clients": ["Preview"]}
    assert warned["summary"] == f"Fixture capture {PREVIEW_LINE}"
    assert warned["summary"].count("Quit ") == 1
    # The same request reached the engine both times: nothing refused, nothing added.
    assert len(real_product.calls) == 2 and real_product.calls[0] == real_product.calls[1]
    assert set(warned["evidence"]) == set(clean["evidence"])
    assert warned["evidence"]["source"] == clean["evidence"]["source"] == "fixture"


def test_shoot_policy_budget_is_unchanged_by_the_warning(real_product):
    policy = {"mode": "off"}
    clean = real_product.client.post("/shoot", headers=real_product.headers,
                                     json={"intent": "x", "stack_policy": policy}).json()
    real_product.listing["listing"] = _listing(PREVIEW, PHOTOS)
    warned = real_product.client.post("/shoot", headers=real_product.headers,
                                      json={"intent": "x", "stack_policy": policy}).json()
    assert warned["evidence"].get("stack_policy") == clean["evidence"].get("stack_policy")
    assert warned["summary"].count("Quit ") == 1


def test_an_unavailable_preflight_adds_no_line(real_product, monkeypatch):
    def fails():
        raise OSError("ps missing")

    monkeypatch.setattr(preflight, "_list_processes", fails)
    body = real_product.client.post("/shoot", headers=real_product.headers, json={}).json()
    assert body["evidence"]["host_camera_clients"]["status"] == "unavailable"
    assert body["summary"] == "Fixture capture"


def test_mock_engine_shoot_has_no_preflight(product):
    body = product.client.post("/shoot", headers=product.headers, json={}).json()
    assert "host_camera_clients" not in body["evidence"]


def test_uncertain_shoot_409_carries_the_warning(real_product, monkeypatch):
    def shoot(intent=None, **kwargs):
        real_product.calls.append(intent)
        failure = CaptureOutcomeUncertain("Sony action capture failed: Could not claim the USB device")
        failure.transport_receipt = {"cli": {"exit_code": 1}}
        raise failure

    monkeypatch.setattr(real_product.fake, "shoot", shoot)
    real_product.listing["listing"] = _listing(PREVIEW)
    response = real_product.client.post("/shoot", headers=real_product.headers, json={})
    assert response.status_code == 409
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    assert body["host_camera_clients"]["clients"] == ["Preview"]
    assert body["detail"].endswith(f"Sony action capture failed: Could not claim the USB device {PREVIEW_LINE}")
    assert body["detail"].count("Quit ") == 1
    assert len(real_product.calls) == 1


def test_session_create_carries_the_warning_without_refusing(real_product):
    _real_runner(real_product)
    model = sorted(real_product.engine.USB_CONTROL_MODELS)[0]
    body = {"mode": "timelapse", "frames": 200, "interval_s": 5, "camera_model": model,
            "camera_port": "usb:000,003", "camera_serial": SERIAL}
    clean = real_product.client.post("/sessions", headers=real_product.headers, json=body)
    assert clean.status_code == 200, clean.text
    real_product.listing["listing"] = _listing(PREVIEW)
    warned = real_product.client.post("/sessions", headers=real_product.headers, json=body)
    assert warned.status_code == 200, warned.text
    clean, warned = clean.json(), warned.json()
    assert "host_camera_warning" not in clean
    assert warned["host_camera_clients"] == {"status": "checked", "clients": ["Preview"]}
    assert warned["host_camera_warning"] == PREVIEW_LINE
    assert warned["frame_count"] == clean["frame_count"] == 200
    # Response only: the stored session does not carry it.
    stored = real_product.client.get(f"/sessions/{warned['id']}", headers=real_product.headers).json()
    assert "host_camera_clients" not in stored and "host_camera_warning" not in stored


def test_job_submit_carries_the_warning_without_refusing(real_product, monkeypatch):
    submitted = []

    class Jobs:
        def submit(self, session_id, *, delay_s=0):
            submitted.append((session_id, delay_s))
            return {"id": session_id, "status": "queued", "delay_s": delay_s}

    monkeypatch.setattr(real_product.app, "_session_jobs", lambda: Jobs())
    real_product.listing["listing"] = _listing(PHOTOS)
    response = real_product.client.post("/sessions/" + "a" * 32 + "/jobs", headers=real_product.headers,
                                        json={"delay_seconds": 0})
    assert response.status_code == 202, response.text
    body = response.json()
    assert submitted == [("a" * 32, 0)]
    assert body["status"] == "queued"
    assert body["host_camera_warning"] == ("Quit Photos while the camera is in use: "
                                           "it can take the USB connection mid-capture.")
