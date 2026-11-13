"""Runtime split (docs/RUNTIME_SPLIT_2026-09-28.md): control, perception and heavy-job roles.

Camera-free. Every camera is a fake; CLIP is a fake handler behind the real socket protocol.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import engine  # noqa: E402
import heavy_jobs  # noqa: E402
import perception_client  # noqa: E402
import product_sessions  # noqa: E402
import runtime_roles  # noqa: E402

SERIAL = "A7II00SERIAL"
MODEL = "Sony ILCE-7M2 (Control)"
PORT = "usb:001,002"


def _child_env(**extra):
    # A child must not look like a systemd unit because the test runner descends from one.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CAMERA_AUTOPILOT_")
           and key not in ("PYTHONPATH", "INVOCATION_ID", "JOURNAL_STREAM")}
    env.update(CAMERA_ENGINE="mock", OLLAMA_HOST="http://127.0.0.1:1", VISION_HOST="http://127.0.0.1:1",
               PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def _run_child(code: str, **env) -> str:
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=_child_env(**env),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout.strip().splitlines()[-1]


# --- the control import boundary ---------------------------------------------------------------

def test_control_entry_point_imports_no_ml_or_merge_code():
    """Reverting would catch: a top-level import of torch/open_clip or a merge module anywhere on
    the control import path (app, engine, product_sessions, session jobs, the MCP-free API)."""
    code = (
        "import json, sys\n"
        "import role_control\n"
        "role_control.prepare()\n"
        "import app, engine, product_sessions, stack_session, session_jobs, product_capture\n"
        "import runtime_roles\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in runtime_roles.CONTROL_ABSENT_MODULES)\n"
        "print(json.dumps({'loaded': loaded, 'role': runtime_roles.role(),\n"
        "                  'deferred': runtime_roles.heavy_jobs_deferred()}))\n")
    report = json.loads(_run_child(code))
    assert report == {"loaded": [], "role": "control", "deferred": True}


def test_control_guard_refuses_torch_and_open_clip_even_when_installed(tmp_path):
    """Reverting the import guard would let a control venv that happens to contain torch load the
    1.5 GB CLIP stack in-process on the first shot."""
    fake_site = tmp_path / "site"
    for name in ("torch", "open_clip", "torchvision"):
        (fake_site / name).mkdir(parents=True)
        (fake_site / name / "__init__.py").write_text("LOADED = True\n")
    (fake_site / "torch" / "nn").mkdir()
    (fake_site / "torch" / "nn" / "__init__.py").write_text("LOADED = True\n")
    probe = (
        "import json, sys\n"
        "{prepare}"
        "result = {{}}\n"
        "for name in ('torch', 'open_clip', 'torchvision', 'torch.nn'):\n"
        "    try:\n"
        "        __import__(name)\n"
        "        result[name] = 'imported'\n"
        "    except ModuleNotFoundError:\n"
        "        result[name] = 'blocked'\n"
        "import embed_index\n"
        "result['embed_index.open_clip'] = embed_index.open_clip is None\n"
        "print(json.dumps(result))\n")
    guarded = json.loads(_run_child(probe.format(prepare="import role_control; role_control.prepare()\n"),
                                    PYTHONPATH=str(fake_site)))
    assert guarded == {"torch": "blocked", "open_clip": "blocked", "torchvision": "blocked",
                       "torch.nn": "blocked", "embed_index.open_clip": True}
    unguarded = json.loads(_run_child(probe.format(prepare=""), PYTHONPATH=str(fake_site)))
    # The fakes (including torch.nn) are importable; only the guard refuses them.
    assert unguarded["torch"] == unguarded["torch.nn"] == unguarded["open_clip"] == "imported"


def test_deferred_capture_in_control_process_never_imports_merge_code():
    """A deferred HDR session captured in the control role must not load stacking/crowd/deflicker
    or the video encoder module; reverting deferral would merge in-process."""
    code = r'''
import json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path.cwd().parent))  # the repo-root sonyremote package, as RealEngine adds it
import role_control
role_control.prepare()
from PIL import Image
import engine, product_sessions, runtime_roles, sonyremote, camera_caps
root = Path(tempfile.mkdtemp())
engine.SHOTS_DIR = root / "shots"
engine._free_usb = lambda: None
live = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"}
class Camera:
    shots = 0
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def info(self): return SimpleNamespace(model="%(model)s", port="%(port)s")
    def wake(self): return True
    def shooting_mode(self): return "M"
    def get_settings(self): return dict(live)
    def list_options(self, key): return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
    def set_exposure(self, **kwargs): live.update(kwargs)
    def set_setting(self, key, value): live[key] = value
    def capture(self, **kwargs):
        type(self).shots += 1
        path = root / f"capture{self.shots}.jpg"
        Image.new("RGB", (64, 48), (70 + 30 * self.shots, 90, 100)).save(path)
        return str(path)
sonyremote.Camera = Camera
camera_caps.from_live_choices = lambda *a: SimpleNamespace(snap_shutter=lambda v: (v, "1/60"),
                                                          shutters=[1 / 8000, 1.0], shutter_labels=["1/8000", "1"])
sessions = product_sessions.runner()
state = sessions.create("hdr", 2, metadata={"camera_model": "%(model)s", "camera_port": "%(port)s",
                                             "camera_serial": "%(serial)s"})
result = product_sessions.run(state["id"])
loaded = sorted(m for m in sys.modules if m.split(".")[0] in runtime_roles.CONTROL_ABSENT_MODULES)
print(json.dumps({"loaded": loaded, "status": result["status"], "heavy": result["heavy_job"]["status"],
                  "result_exists": (sessions._directory(state["id"]) / "result.jpg").exists(),
                  "shots": Camera.shots, "restore": result["restore"]}))
''' % {"model": MODEL, "port": PORT, "serial": SERIAL}
    report = json.loads(_run_child(code))
    assert report == {"loaded": [], "status": "completed", "heavy": "queued", "result_exists": False,
                      "shots": 2, "restore": "completed"}


# --- queue -> worker -> publication ------------------------------------------------------------

def _jpeg(path, shade):
    Image.new("RGB", (64, 48), (shade, 90, 100)).save(path)
    return str(path)


class _FakeCamera:
    shots = 0
    root: Path

    def __init__(self, **kwargs):
        self.live = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def info(self):
        return SimpleNamespace(model=MODEL, port=PORT)

    def wake(self):
        return True

    def shooting_mode(self):
        return "M"

    def get_settings(self):
        return dict(self.live)

    def list_options(self, key):
        return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]

    def set_exposure(self, **kwargs):
        self.live.update(kwargs)

    def set_setting(self, key, value):
        self.live[key] = value

    def capture(self, **kwargs):
        type(self).shots += 1
        return _jpeg(self.root / f"capture{self.shots}.jpg", 60 + 40 * self.shots)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import camera_caps
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    camera = type("Camera", (_FakeCamera,), {"shots": 0, "root": tmp_path})
    monkeypatch.setattr(sonyremote, "Camera", camera)
    monkeypatch.setattr(camera_caps, "from_live_choices",
                        lambda *args: SimpleNamespace(snap_shutter=lambda value: (value, "1/60"),
                                                      # the live ladder's ends, which #86's precheck reads
                                                      shutters=[1 / 8000, 1.0], shutter_labels=["1/8000", "1"]))
    return SimpleNamespace(sessions=product_sessions.runner(), camera=camera)


def _create(sessions, mode="long_exposure", frames=2):
    return sessions.create(mode, frames, metadata={"camera_model": MODEL, "camera_port": PORT,
                                                   "camera_serial": SERIAL})


def test_monolith_mode_merges_inline_and_publishes_unchanged(store, monkeypatch):
    """The Mac default must stay the single process: no heavy_job, inline merge, published."""
    monkeypatch.delenv(runtime_roles.ROLE_ENV, raising=False)
    state = _create(store.sessions)
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed"
    assert "heavy_job" not in result
    assert result["publication"]["status"] == "published"


def test_deferred_session_queues_worker_merges_and_control_publishes(store, monkeypatch):
    """Reverting any leg would catch: inline merge in control, a worker that ignores the queue,
    a merge that skips input receipts, or a result that never reaches the gallery."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    queued = product_sessions.run(state["id"])
    directory = store.sessions._directory(state["id"])
    assert queued["status"] == "completed" and queued["restore"] == "completed"
    assert queued["heavy_job"]["status"] == "queued"
    assert queued["heavy_job"]["inputs"] == [f["receipt"]["sha256"] for f in queued["frames"]]
    assert not (directory / "result.jpg").exists()
    assert "publication" not in queued
    assert product_sessions.publish_deferred() == []  # nothing is ready yet

    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [state["id"]]
    merged = store.sessions.read(state["id"])
    assert merged["heavy_job"]["status"] == "completed"
    assert merged["heavy_job"]["attempts"] == 1
    assert merged["output"]["receipt"]["sha256"] == merged["heavy_job"]["outputs"]["result.jpg"]
    assert heavy_jobs.run_pending(store.sessions) == []  # idempotent: nothing left to do

    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == [state["id"]]
    published = store.sessions.read(state["id"])
    assert published["publication"]["status"] == "published"
    assert (engine.SHOTS_DIR / f"session_{state['id']}.jpg").is_file()
    assert product_sessions.publish_deferred() == []
    assert store.camera.shots == 2  # publication and merging never reopened the camera


def test_worker_refuses_changed_original_and_keeps_it_unpublished(store, monkeypatch):
    """The worker must merge only the hash-verified originals control accepted."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    frame = store.sessions._directory(state["id"]) / "frame_0001.jpg"
    _jpeg(frame, 3)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [state["id"]]
    failed = store.sessions.read(state["id"])
    assert failed["heavy_job"]["status"] == "failed"
    assert "changed" in failed["heavy_job"]["error"]
    assert failed.get("output") is None
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == []


def test_worker_skips_a_busy_session_and_recovers_a_crashed_run(store, monkeypatch):
    """A held operation lock means someone else owns the session; a 'running' job whose lock is
    free means the worker died (e.g. an OOM kill) and is recomputed, up to a bounded count."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    with store.sessions.operation(state["id"]):
        assert heavy_jobs.run_pending(store.sessions) == []
    crashed = store.sessions.read(state["id"])
    crashed["heavy_job"].update(status="running", attempts=1)
    store.sessions.save(crashed)
    assert heavy_jobs.run_pending(store.sessions) == [state["id"]]
    recovered = store.sessions.read(state["id"])
    assert recovered["heavy_job"]["status"] == "completed"
    assert recovered["heavy_job"]["attempts"] == 2

    exhausted = _create(store.sessions)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    product_sessions.run(exhausted["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    record = store.sessions.read(exhausted["id"])
    record["heavy_job"].update(status="running", attempts=heavy_jobs.MAX_ATTEMPTS)
    store.sessions.save(record)
    assert heavy_jobs.run_pending(store.sessions) == [exhausted["id"]]
    final = store.sessions.read(exhausted["id"])
    assert final["heavy_job"]["status"] == "failed"
    assert "exited" in final["heavy_job"]["error"]
    assert final.get("output") is None


def test_rerun_of_a_failed_heavy_job_requeues_without_the_camera(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    record = store.sessions.read(state["id"])
    record["heavy_job"].update(status="failed", error="RuntimeError: simulated", attempts=3)
    store.sessions.save(record)
    shots = store.camera.shots
    requeued = product_sessions.run(state["id"])
    assert requeued["heavy_job"]["status"] == "queued"
    assert requeued["heavy_job"]["attempts"] == 0
    assert store.camera.shots == shots


def test_deferred_timelapse_worker_builds_sequence_and_publishes(store, monkeypatch):
    """Timelapse encoding is heavy work too; without ffmpeg the video is recorded as unavailable
    and the JPEG sequence still publishes."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "timelapse", 2)
    queued = product_sessions.run(state["id"])
    assert queued["heavy_job"]["status"] == "queued"
    assert "sequence" not in queued and "video" not in queued
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [state["id"]]
    merged = store.sessions.read(state["id"])
    assert merged["heavy_job"]["status"] == "completed"
    assert merged["sequence"]["format"] == "zip/jpeg_sequence"
    assert merged["video"]["status"] in ("completed", "unavailable")
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    # Publication in control must not re-run deflicker/encoding, even when the video was unavailable.
    monkeypatch.setattr(product_sessions, "_video", lambda *args: pytest.fail("control re-rendered the video"))
    assert product_sessions.publish_deferred() == [state["id"]]
    assert store.sessions.read(state["id"])["publication"]["status"] == "published"


def test_jobs_entry_point_once_processes_the_queue(store, monkeypatch, tmp_path):
    """The worker is a separately startable process: `role_jobs.py --once` picks up the queue."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    result = subprocess.run([sys.executable, str(BACKEND / "role_jobs.py"), "--once"], cwd=BACKEND,
                            env=_child_env(CAMERA_AUTOPILOT_SHOTS_DIR=str(engine.SHOTS_DIR)),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"role": "jobs", "processed": [state["id"]]}
    assert store.sessions.read(state["id"])["heavy_job"]["status"] == "completed"


def test_jobs_role_merges_without_loading_camera_or_clip_modules(store, monkeypatch):
    """The worker may be OOM-killed at any time; it must never hold the camera or the CLIP stack."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr")
    product_sessions.run(state["id"])
    code = ("import json, sys\n"
            "import role_jobs\n"
            "role_jobs.main(['--once'])\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in\n"
            "      ('sonyremote', 'gphoto2', 'torch', 'open_clip', 'embed_index'))))\n")
    assert json.loads(_run_child(code, CAMERA_AUTOPILOT_SHOTS_DIR=str(engine.SHOTS_DIR))) == []
    assert store.sessions.read(state["id"])["heavy_job"]["status"] == "completed"


# --- perception socket ---------------------------------------------------------------------------

@pytest.fixture
def short_dir():
    # AF_UNIX paths are limited to ~104 bytes on macOS; keep the socket path short.
    directory = Path(tempfile.mkdtemp(prefix="ps-"))
    directory.chmod(0o700)
    yield directory
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()


def _serve(socket_path, handlers):
    import role_perception
    ready = threading.Event()
    server = role_perception.PerceptionServer(socket_path, handlers)
    thread = threading.Thread(target=server.serve_forever, kwargs={"ready": ready}, daemon=True)
    thread.start()
    assert ready.wait(10)
    return server, thread


def test_perception_socket_round_trip_and_private_permissions(short_dir, tmp_path, monkeypatch):
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    calls = []
    handlers = {"recommend": lambda path, k: calls.append(("recommend", path, k)) or
                {"f_number": 5.6, "iso": 200, "focal_mm": 35},
                "classify": lambda path: calls.append(("classify", path)) or {"bird": 0.9, "person": 0.1}}
    sock = short_dir / "p.sock"
    server, thread = _serve(sock, handlers)
    try:
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(sock))
        monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
        assert perception_client.recommend(str(image), 15) == {"f_number": 5.6, "iso": 200, "focal_mm": 35}
        assert perception_client.classify(str(image)) == {"bird": 0.9, "person": 0.1}
        # The real seams forward in the control role instead of loading CLIP locally.
        import embed_index
        import subject_recognition
        monkeypatch.setattr(embed_index, "_get_index", lambda: pytest.fail("control must not load the index"))
        assert embed_index.recommend(str(image)) == {"f_number": 5.6, "iso": 200, "focal_mm": 35}
        evidence = subject_recognition.subject_evidence(str(image), focal_mm=400)
        assert (evidence["status"], evidence["class"]) == ("applied", "bird")
        assert calls[0] == ("recommend", str(image.resolve()), 15)
        with pytest.raises(perception_client.PerceptionUnavailable, match="absolute"):
            perception_client.recommend("relative.jpg", 15)
    finally:
        server.shutdown()
        thread.join(10)


def test_perception_errors_degrade_to_the_labelled_unavailable_path(short_dir, tmp_path, monkeypatch):
    """A crashed or failing perception service must surface as 'unavailable', never as an answer."""
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    import embed_index
    import subject_recognition
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.delenv(runtime_roles.PERCEPTION_SOCKET_ENV, raising=False)
    monkeypatch.setattr(embed_index, "_get_index", lambda: pytest.fail("control must not load the index"))
    with pytest.raises(perception_client.PerceptionUnavailable, match="not configured"):
        embed_index.recommend(str(image))
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "missing.sock"))
    assert subject_recognition.subject_evidence(str(image))["status"] == "unavailable"

    def broken(path, k):
        raise RuntimeError("CLIP weights missing")
    sock = short_dir / "p.sock"
    server, thread = _serve(sock, {"recommend": broken, "classify": lambda path: {"x": "nan"}})
    try:
        monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(sock))
        with pytest.raises(perception_client.PerceptionUnavailable, match="CLIP weights missing"):
            embed_index.recommend(str(image))
        assert subject_recognition.subject_evidence(str(image))["status"] == "unavailable"
    finally:
        server.shutdown()
        thread.join(10)


def test_perception_role_never_forwards_to_itself(monkeypatch, short_dir):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "perception")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "p.sock"))
    assert perception_client.remote() is False
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert perception_client.remote() is True
    monkeypatch.delenv(runtime_roles.ROLE_ENV)
    assert perception_client.remote() is False  # a Mac sourcing the puck env still uses in-process CLIP
    monkeypatch.delenv(runtime_roles.PERCEPTION_SOCKET_ENV)
    assert perception_client.remote() is False


def test_perception_server_refuses_an_unsafe_socket_directory(tmp_path):
    import role_perception
    loose = Path(tempfile.mkdtemp(prefix="pl-"))
    try:
        loose.chmod(0o777)
        with pytest.raises(ValueError, match="0700"):
            role_perception.PerceptionServer(loose / "p.sock", {})
    finally:
        loose.rmdir()


# --- deploy: units, launcher and locks ---------------------------------------------------------

def test_worker_units_are_capped_and_prefer_to_be_killed_before_control():
    sys.path.insert(0, str(ROOT))
    from deploy.provision import render_unit
    repo, env = Path("/srv/camera autopilot"), Path("/home/pi/.config/camera-autopilot/runtime.env")
    control = render_unit(repo, env)
    assert 'ExecStart="/srv/camera autopilot/run.sh"' in control
    for role, venv in (("perception", ".venv-perception"), ("jobs", ".venv-jobs")):
        unit = render_unit(repo, env, role=role)
        assert f'ExecStart="/srv/camera autopilot/backend/{venv}/bin/python" ' \
               f'"/srv/camera autopilot/backend/role_{role}.py"' in unit
        assert re.search(r"^MemoryMax=\d+[MG]$", unit, re.M)
        assert "OOMScoreAdjust=500" in unit
        assert "NoNewPrivileges=yes" in unit and "UMask=0077" in unit
        assert "@" not in unit.replace("user@", "")


def test_launcher_dry_run_names_the_split_control_role(tmp_path):
    env = _child_env(CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN="1", CAMERA_AUTOPILOT_RUNTIME="split",
                     CAMERA_AUTOPILOT_API_TOKEN="t" * 32, TMPDIR=str(tmp_path))
    result = subprocess.run(["bash", str(ROOT / "run.sh")], env=env, capture_output=True, text=True, timeout=60)
    if "No virtualenv found" in result.stderr:
        pytest.skip("run.sh needs backend/.venv (link it as in CI)")
    assert result.returncode == 0, result.stderr
    assert "Runtime:         split (camera-control role)" in result.stdout
    env.pop("CAMERA_AUTOPILOT_RUNTIME")
    plain = subprocess.run(["bash", str(ROOT / "run.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert plain.returncode == 0 and "Runtime:" not in plain.stdout


def test_launcher_dispatches_the_control_role_only_when_split(tmp_path):
    """Run the real launcher against stub executables: split starts role_control.py with the same
    Uvicorn arguments, the default still starts uvicorn itself."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    log = tmp_path / "launched.txt"
    for name in ("python", "uvicorn"):
        stub = venv / "bin" / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    base = _child_env(CAMERA_AUTOPILOT_VENV=str(venv), CAMERA_AUTOPILOT_API_TOKEN="t" * 32,
                      CAMERA_AUTOPILOT_PORT="18765", TMPDIR=str(tmp_path))
    for runtime, expected in (("split", "python role_control.py app:app --host 127.0.0.1 --port 18765 --workers 1"),
                              (None, "uvicorn app:app --host 127.0.0.1 --port 18765 --workers 1")):
        env = dict(base, **({"CAMERA_AUTOPILOT_RUNTIME": runtime} if runtime else {}))
        log.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(ROOT / "run.sh")], env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert log.read_text().strip() == expected


LOCKS = ROOT / "deploy" / "locks"
# mcp is the MCP server's dependency; server.py opens the camera itself and does not run on the puck.
ROLE_FORBIDDEN = {"control": {"torch", "torchvision", "open-clip-torch", "mcp"},
                  "jobs": {"torch", "torchvision", "open-clip-torch", "fastapi", "gphoto2", "rawpy"}}


def _names(lines):
    names = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?\s*(==\s*([^\s;]+))?\s*(;.*)?", line)
        assert match, f"unparseable requirement line: {line!r}"
        names[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(4)
    return names


@pytest.mark.parametrize("role", ["control", "perception", "jobs"])
def test_role_lock_parses_pins_every_package_and_covers_direct_deps(role):
    """Reverting or hand-editing a lock would drop a direct dependency or leave a range unpinned."""
    direct = _names((LOCKS / f"requirements-{role}.in").read_text().splitlines())
    lock_text = (LOCKS / f"requirements-{role}.linux-aarch64.lock").read_text()
    # Bookworm: Python 3.11, glibc 2.36; binary-only resolution means no pin builds from source.
    assert "--python-platform aarch64-manylinux_2_36" in lock_text
    assert "--python-version 3.11" in lock_text
    assert "--only-binary :all:" in lock_text
    assert "--exclude-newer" in lock_text
    pinned = _names(lock_text.splitlines())
    assert pinned and all(version for version in pinned.values()), "every lock line must be ==pinned"
    assert set(direct) <= set(pinned)
    for name, version in direct.items():
        if version:
            assert pinned[name] == version
    assert not (ROLE_FORBIDDEN.get(role, set()) & set(pinned))


def test_perception_lock_carries_the_cpu_clip_stack():
    pinned = _names((LOCKS / "requirements-perception.linux-aarch64.lock").read_text().splitlines())
    assert {"torch", "open-clip-torch", "numpy", "pillow"} <= set(pinned)
    assert not any(name.startswith("nvidia-") for name in pinned)  # aarch64 CPU wheels, no CUDA
    assert pinned["torch"].endswith("+cpu")


# --- review follow-ups (Codex cross-review, 2026-09-28) ------------------------------------------

def test_a_failed_timelapse_encode_is_retried_by_the_worker(store, monkeypatch):
    """A failed encode is not a finished job: the worker re-queues it (bounded, see N3), and a later
    successful encode completes it. Nothing is published in between."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "timelapse", 2)
    product_sessions.run(state["id"])
    real_video = product_sessions._video

    def failing(sessions, live):
        live["video"] = {"status": "failed", "error": "OSError: encoder died"}
        sessions.save(live)
        return live
    monkeypatch.setattr(product_sessions, "_video", failing)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    heavy_jobs.run_pending(store.sessions)
    retrying = store.sessions.read(state["id"])
    assert retrying["heavy_job"]["status"] == "queued" and "encoder died" in retrying["heavy_job"]["last_error"]
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == []
    monkeypatch.setattr(product_sessions, "_video", real_video)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [state["id"]]
    done = store.sessions.read(state["id"])
    assert done["heavy_job"]["status"] == "completed" and done["heavy_job"]["attempts"] == 2
    assert done["video"]["status"] in ("completed", "unavailable")


def test_fixed_stack_shot_reports_a_queued_merge_not_a_failure(store, monkeypatch):
    import capture_policy
    import product_capture
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setattr(capture_policy, "exposure_budget", lambda policy: {})
    camera = SimpleNamespace(name="real", camera_model=MODEL, camera_port=PORT, camera_serial=SERIAL)
    shot = product_capture._session_capture(camera, {"mode": "long_exposure", "frames": 2}, {}, None)
    assert shot.ok is False and shot.image == ""
    assert "merge is queued" in shot.summary
    assert shot.evidence["stack_policy"]["heavy_job"] == "queued"


def test_malformed_and_hostile_requests_never_stop_the_perception_service(short_dir, tmp_path):
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    sock = short_dir / "p.sock"
    server, thread = _serve(sock, {"recommend": lambda path, k: {"f_number": 10 ** 400, "iso": 100, "focal_mm": 35},
                                   "classify": lambda path: {"bird": float("nan")}})

    def raw(payload: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(sock))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            return json.loads(connection.recv(65536))
    try:
        for payload in (b'{"op": [], "path": "/x"}', b'{"op": {}, "path": 1}', b"[" * 50000, b"not json"):
            assert raw(payload)["ok"] is False
        assert raw(json.dumps({"op": "classify", "path": str(image)}).encode())["ok"] is False  # NaN refused
        assert thread.is_alive()
        os.environ[runtime_roles.PERCEPTION_SOCKET_ENV] = str(sock)
        try:
            with pytest.raises(perception_client.PerceptionUnavailable, match="non-finite"):
                perception_client.recommend(str(image), 15)  # an int too large for a float
        finally:
            os.environ.pop(runtime_roles.PERCEPTION_SOCKET_ENV)
    finally:
        server.shutdown()
        thread.join(10)


def test_client_deadline_covers_the_whole_request(short_dir, tmp_path, monkeypatch):
    """A server that trickles bytes must not hold control past the configured deadline."""
    import time as time_module
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    sock_path = short_dir / "slow.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)

    def trickle():
        connection, _ = listener.accept()
        with connection:
            for _ in range(40):
                try:
                    connection.sendall(b" ")
                except OSError:
                    return
                time_module.sleep(0.1)
    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(sock_path))
    monkeypatch.setenv("CAMERA_AUTOPILOT_PERCEPTION_TIMEOUT_S", "0.5")
    started = time_module.monotonic()
    with pytest.raises(perception_client.PerceptionUnavailable):
        perception_client.recommend(str(image), 15)
    assert time_module.monotonic() - started < 2.0
    listener.close()
    thread.join(10)


@pytest.mark.parametrize("filename, ok", [
    ("example-1-cp311-cp311-any.whl", False),
    ("x-1-py3-none-any.whl", True),
    ("x-1-cp37-abi3-manylinux2014_aarch64.whl", True),
    ("x-1-cp312-abi3-manylinux_2_28_aarch64.whl", False),
    ("x-1-cp311-cp311-manylinux_2_39_aarch64.whl", False),
    ("x-1-cp311-cp311-manylinux_2_28_x86_64.whl", False),
    ("x-1-cp311-cp311-linux_aarch64.whl", False),
    ("x-1-cp311-cp311-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl", True),
])
def test_wheel_check_accepts_only_bookworm_installable_tags(filename, ok):
    sys.path.insert(0, str(LOCKS))
    from check_wheels import wheel_ok
    assert wheel_ok(filename) is ok


# --- panel review follow-ups (PR #77 panel, 2026-09-27) --------------------------------------------

def _session_with_job(sessions, status):
    import stack_session
    state = _create(sessions)
    job = {"id": state["id"], "operation_id": "a" * 32, "status": status, "submitted_at": 0.0}
    stack_session._atomic_json(sessions._directory(state["id"]) / "job.json", job)
    return state


def test_f1_control_scheduler_starts_while_the_worker_holds_a_session_lock(store):
    """Control must come up while role_jobs merges a session whose job receipt is still 'running';
    the receipt is recovered once the worker lets go."""
    from session_jobs import SessionJobs
    state = _session_with_job(store.sessions, "running")
    with store.sessions.operation(state["id"]):  # the worker's lease, held from another descriptor
        jobs = SessionJobs(product_sessions.runner, lambda session_id: None)
        busy = jobs.status(state["id"])
        # Not "running": the UI must not lock camera controls for a receipt that only awaits a lock.
        assert busy["status"] == "recovery_pending" and busy["previous_status"] == "running"
        with pytest.raises(RuntimeError, match="another process"):
            jobs.submit(state["id"])  # no replay while the receipt is unrecovered
    recovered = jobs.status(state["id"])
    assert recovered["status"] == "recovery_required"
    assert json.loads((store.sessions._directory(state["id"]) / "job.json").read_text())["status"] == "recovery_required"
    jobs.close()


def test_f1_final_job_receipt_waits_for_a_held_lock_instead_of_raising(store, monkeypatch):
    import contextlib
    from session_jobs import SessionJobs
    state = _create(store.sessions)
    held = contextlib.ExitStack()

    def run_session(session_id):
        held.enter_context(store.sessions.operation(session_id))  # the worker grabs it right after capture
        return store.sessions.read(session_id)
    jobs = SessionJobs(product_sessions.runner, run_session)
    jobs.submit(state["id"])
    jobs._futures[state["id"]].result(timeout=30)  # _work must not raise out of the worker thread
    assert jobs.status(state["id"])["metadata_status"] == "write_failed"  # visible, not raised
    held.close()
    repaired = jobs.status(state["id"])  # repaired from the session manifest, then saved
    assert repaired["metadata_reconciled"] is True and repaired["status"] == "recovery_required"
    assert json.loads((store.sessions._directory(state["id"]) / "job.json").read_text())["status"] == "recovery_required"
    jobs.close()


def test_f2_calibrated_recommendation_survives_a_dead_perception_service(tmp_path, monkeypatch, short_dir):
    """The A7 II path (recommend_settings) must take the labelled fallback, never abort the shot."""
    import brain
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "dead.sock"))
    rec = brain.recommend_settings(str(image), 10.0, focal_mm=50.0, fallback_aperture=5.6)
    assert any(note.startswith("Taste unavailable") for note in rec.notes)
    assert rec.aperture > 0 and rec.shutter_s > 0


def _silent_listener(path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)  # accepts into the backlog, never answers
    return listener


def test_f3_one_perception_deadline_covers_the_whole_shot(short_dir, tmp_path, monkeypatch):
    import time as time_module
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    listener = _silent_listener(short_dir / "hung.sock")
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "hung.sock"))
    monkeypatch.setenv("CAMERA_AUTOPILOT_PERCEPTION_TIMEOUT_S", "0.4")
    started = time_module.monotonic()
    with perception_client.shot_deadline():
        for call in (lambda: perception_client.classify(str(image)),
                     lambda: perception_client.recommend(str(image), 15)):
            with pytest.raises(perception_client.PerceptionUnavailable):
                call()
    assert time_module.monotonic() - started < 0.6  # one 0.4 s budget, not two
    listener.close()
    assert "perception_client.shot_deadline()" in (BACKEND / "engine.py").read_text()


def test_f3_a_slow_first_answer_leaves_only_the_rest_of_the_budget(short_dir, tmp_path, monkeypatch):
    """classify answers after 0.3 s, recommend never answers: the shot still ends near 0.5 s."""
    import time as time_module
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(short_dir / "slow.sock"))
    listener.listen(8)
    held = []

    def serve():
        first, _ = listener.accept()
        first.recv(65536)
        time_module.sleep(0.3)
        first.sendall(json.dumps({"ok": True, "result": {"bird": 0.9}}).encode())
        first.close()
        second, _ = listener.accept()
        held.append(second)  # never answers
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "slow.sock"))
    monkeypatch.setenv("CAMERA_AUTOPILOT_PERCEPTION_TIMEOUT_S", "0.5")
    started = time_module.monotonic()
    with perception_client.shot_deadline():
        assert perception_client.classify(str(image)) == {"bird": 0.9}
        with pytest.raises(perception_client.PerceptionUnavailable):
            perception_client.recommend(str(image), 15)
    assert time_module.monotonic() - started < 0.7  # 0.3 + the remaining 0.2, not 0.3 + 0.5
    thread.join(5)
    for connection in held:
        connection.close()
    listener.close()


def test_f4_socket_appears_only_after_clip_has_warmed(short_dir):
    import role_perception
    sock = short_dir / "p.sock"
    seen = []
    server = role_perception.open_server(sock, {}, warm=lambda: seen.append(sock.exists()))
    try:
        assert seen == [False] and sock.exists()
    finally:
        server.shutdown()
        server.close()


def test_f5_a_hung_inference_answers_an_error_and_asks_for_a_restart(short_dir, tmp_path):
    import time as time_module
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    hung = []
    server, thread = _serve(short_dir / "p.sock", {"classify": lambda path: time_module.sleep(3)})
    server.inference_timeout_s = 0.3
    server.on_hang = lambda: hung.append(True)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(short_dir / "p.sock"))
            connection.sendall(json.dumps({"op": "classify", "path": str(image)}).encode())
            connection.shutdown(socket.SHUT_WR)
            reply = json.loads(connection.recv(65536))
        assert reply["ok"] is False and "exceeded" in reply["error"]
        deadline = time_module.monotonic() + 5
        while not hung and time_module.monotonic() < deadline:  # the exit follows the reply
            time_module.sleep(0.02)
        assert hung == [True]
    finally:
        server.shutdown()
        thread.join(10)


def test_f6_publish_sweep_scans_without_the_capture_lock(store, monkeypatch):
    from photo_lifecycle import _LOCK
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    waiting = _create(store.sessions)
    product_sessions.run(waiting["id"])  # queued, not ready: only scanned
    held_during_scan = []
    real_read = store.sessions.__class__.read

    def spy(self, session_id):
        if session_id == waiting["id"]:
            held_during_scan.append(_LOCK._is_owned())
        return real_read(self, session_id)
    monkeypatch.setattr(store.sessions.__class__, "read", spy)
    assert product_sessions.publish_deferred() == []
    assert held_during_scan and not any(held_during_scan)


def test_f7_session_view_shows_processing_and_offers_a_no_recapture_retry():
    import subprocess as sp
    source = (BACKEND / "web" / "index.html").read_text()
    body = re.search(r"  function showSession\(state\) \{.*?\n  \}", source, re.S).group()
    # showSession asks sessionResumable() (a refused fixed ladder never offers Resume).
    body += "\n" + re.search(r"  function sessionResumable\(state\) \{.*?\n  \}", source, re.S).group()
    harness = body + """
      const elements={};const $=id=>elements[id] || (elements[id]={textContent:'',hidden:false});
      const Autopilot={deviceStore:{write(){},ACTIVE_SESSION_KEY:'k'},deliveryWarning:()=>''};
      let activeSession=null,selectedSessionState=null,sessionRunning=false;
      const renderPanoramaPreview=()=>{};const renderCrowdPreview=()=>{};const executionBudgetText=()=>'';
      const renderSessionCameraWarning=()=>{};
      $('session-history').options=[];
      const base={id:'s',mode:'hdr',status:'completed',frame_count:2,frames:[{status:'accepted'},{status:'accepted'}],restore:'completed'};
      showSession({...base,heavy_job:{status:'queued'}});
      assert.match(elements['session-status'].textContent,/captured, processing queued/);
      assert.equal(elements['session-resume'].hidden,true);
      showSession({...base,heavy_job:{status:'failed',error:'MemoryError: x'}});
      assert.match(elements['session-status'].textContent,/Processing failed: MemoryError/);
      assert.equal(elements['session-resume'].hidden,false);
      assert.equal(elements['session-resume'].textContent,'Retry processing (no recapture)');
      showSession(base);
      assert.match(elements['session-status'].textContent,/^hdr: completed/);
    """
    result = sp.run(["node", "-e", "const assert=require('node:assert/strict');\n" + harness],
                    capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "['queued','running'].includes(state.heavy_job?.status)" in source  # the poll keeps watching
    assert "if(selectedSessionState?.heavy_job?.status==='failed'){await runSession(activeSession,0);return;}" in source


def test_f8_worker_stops_between_jobs_and_units_do_not_signal_children(store, monkeypatch):
    import threading as threading_module
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    first, second = _create(store.sessions), _create(store.sessions)
    product_sessions.run(first["id"])
    product_sessions.run(second["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    stop = threading_module.Event()
    real = heavy_jobs.process_locked

    def one_then_stop(sessions, session_id):
        result = real(sessions, session_id)
        stop.set()  # SIGTERM arrives during the first job
        return result
    monkeypatch.setattr(heavy_jobs, "process_locked", one_then_stop)
    assert len(heavy_jobs.run_pending(store.sessions, stop=stop)) == 1
    sys.path.insert(0, str(ROOT))
    from deploy.provision import render_unit
    unit = render_unit(Path("/srv/ca"), Path("/srv/env"), role="jobs")
    assert "KillMode=mixed" in unit and "KillMode=control-group" not in unit
    assert "StartLimitIntervalSec=0" in unit  # poison jobs are bounded by MAX_ATTEMPTS, not the unit


def _fake_cgroup(tmp_path, controllers, memory_max):
    root = tmp_path / "cg"
    group = root / "user.slice/user-1000.slice/user@1000.service/app.slice/camera-autopilot-jobs.service"
    group.mkdir(parents=True)
    (root / "cgroup.controllers").write_text(controllers + "\n")
    if memory_max is not None:
        (group / "memory.max").write_text(memory_max + "\n")
    proc = tmp_path / "self-cgroup"
    proc.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/camera-autopilot-jobs.service\n")
    return root, proc


@pytest.mark.parametrize("controllers, memory_max, enforced", [
    ("cpuset cpu io memory pids", "2147483648", True),
    ("cpuset cpu io memory pids", "max", False),
    ("cpuset cpu io pids", None, False),
])
def test_f9_memory_cap_is_detected_and_a_systemd_worker_refuses_without_it(
        tmp_path, monkeypatch, controllers, memory_max, enforced):
    root, proc = _fake_cgroup(tmp_path, controllers, memory_max)
    monkeypatch.setattr(runtime_roles, "CGROUP_ROOT", root)
    monkeypatch.setattr(runtime_roles, "PROC_SELF_CGROUP", proc)
    status = runtime_roles.memory_cap_status(platform_name="Linux")
    assert status["enforced"] is enforced
    monkeypatch.setenv(runtime_roles.ENFORCE_CAP_ENV, "1")  # set only by the worker unit templates
    monkeypatch.delenv("CAMERA_AUTOPILOT_ALLOW_UNCAPPED", raising=False)
    if enforced:
        runtime_roles.require_memory_cap("jobs", platform_name="Linux")
    else:
        with pytest.raises(SystemExit) as exited:
            runtime_roles.require_memory_cap("jobs", platform_name="Linux")
        assert exited.value.code == runtime_roles.EXIT_UNCAPPED
        monkeypatch.setenv("CAMERA_AUTOPILOT_ALLOW_UNCAPPED", "1")
        runtime_roles.require_memory_cap("jobs", platform_name="Linux")  # explicit, logged override


def test_f9_provisioning_reports_a_missing_memory_controller(tmp_path):
    sys.path.insert(0, str(ROOT))
    from deploy.provision import memory_cgroup_blockers
    root = tmp_path / "cg"
    delegated = root / "user.slice/user-1000.slice/user@1000.service"
    delegated.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu io pids\n")
    (delegated / "cgroup.controllers").write_text("cpu pids\n")
    assert any("cgroup_enable=memory" in item for item in memory_cgroup_blockers(root, 1000))
    (root / "cgroup.controllers").write_text("cpu io memory pids\n")
    assert any("delegate" in item for item in memory_cgroup_blockers(root, 1000))
    (delegated / "cgroup.controllers").write_text("cpu memory pids\n")
    assert memory_cgroup_blockers(root, 1000) == []


def test_f11_job_watchdog_flags_an_overdue_in_process_merge(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(heavy_jobs.time, "monotonic", lambda: clock[0])
    assert heavy_jobs.overdue(60) is None
    heavy_jobs.CURRENT.update(session_id="a" * 32, started=100.0)
    try:
        clock[0] = 150.0
        assert heavy_jobs.overdue(60) is None
        clock[0] = 161.0
        assert heavy_jobs.overdue(60) == "a" * 32
    finally:
        heavy_jobs.CURRENT.update(session_id=None, started=None)


def test_liveness_reports_worker_and_perception_heartbeats(tmp_path, monkeypatch):
    import time as time_module
    store_root = tmp_path / "sessions"
    store_root.mkdir()
    sock = tmp_path / "p.sock"
    report = runtime_roles.liveness(store_root, str(sock))
    assert report["jobs_worker"]["alive"] is False and report["perception"]["alive"] is False
    runtime_roles.touch_heartbeat(store_root / runtime_roles.JOBS_HEARTBEAT)
    runtime_roles.touch_heartbeat(Path(str(sock) + ".alive"))
    report = runtime_roles.liveness(store_root, str(sock))
    assert report["jobs_worker"]["alive"] is True and report["perception"]["alive"] is True
    assert report["jobs_worker"]["heartbeat_age_s"] < 5
    assert "runtime_roles.liveness(" in (BACKEND / "app.py").read_text()


def test_split_then_single_process_finishes_a_queued_merge(store, monkeypatch):
    """A session queued by the puck runtime and later opened by the single Mac process is merged
    inline rather than left unpublishable."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    monkeypatch.delenv(runtime_roles.ROLE_ENV)
    shots = store.camera.shots
    result = product_sessions.run(state["id"])
    assert result["heavy_job"]["status"] == "completed"
    assert result["publication"]["status"] == "published"
    assert store.camera.shots == shots


def test_worker_scan_survives_a_manifest_write_error(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    first, second = _create(store.sessions), _create(store.sessions)
    product_sessions.run(first["id"])
    product_sessions.run(second["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    real = heavy_jobs.process_locked
    broken = sorted([first["id"], second["id"]])[0]

    def flaky(sessions, session_id):
        if session_id == broken:
            raise OSError("disk full")
        return real(sessions, session_id)
    monkeypatch.setattr(heavy_jobs, "process_locked", flaky)
    assert heavy_jobs.run_pending(store.sessions) == [sorted([first["id"], second["id"]])[1]]


def test_perception_and_jobs_units_restart_without_a_start_limit_and_stop_on_config_errors():
    sys.path.insert(0, str(ROOT))
    from deploy.provision import render_unit
    for role in ("perception", "jobs"):
        unit = render_unit(Path("/srv/ca"), Path("/srv/env"), role=role)
        assert "StartLimitIntervalSec=0" in unit
        assert f"RestartPreventExitStatus={runtime_roles.EXIT_UNCAPPED}" in unit


# --- targeted gpt-6-astra review of the panel fixes ----------------------------------------------

def test_a_malformed_first_reply_latches_the_shot_without_a_second_request(short_dir, tmp_path, monkeypatch):
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    connections = []
    server, thread = _serve(short_dir / "p.sock", {
        "classify": lambda path: connections.append("classify") or {"bird": "not a number"},
        "recommend": lambda path, k: connections.append("recommend") or {"f_number": 4, "iso": 100, "focal_mm": 35}})
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "p.sock"))
    try:
        with perception_client.shot_deadline():
            with pytest.raises(perception_client.PerceptionUnavailable, match="non-finite"):
                perception_client.classify(str(image))
            with perception_client.shot_deadline():  # nested: the same shot, still latched
                with pytest.raises(perception_client.PerceptionUnavailable, match="already unavailable"):
                    perception_client.recommend(str(image), 15)
        assert connections == ["classify"]
    finally:
        server.shutdown()
        thread.join(10)


def test_hang_exit_happens_even_when_the_caller_already_left(short_dir, tmp_path):
    import time as time_module
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    hung = []
    server, thread = _serve(short_dir / "p.sock", {"classify": lambda path: time_module.sleep(3)})
    server.inference_timeout_s = 0.5
    server.on_hang = lambda: hung.append(True)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(short_dir / "p.sock"))
            connection.sendall(json.dumps({"op": "classify", "path": str(image)}).encode())
            connection.shutdown(socket.SHUT_WR)
        # the caller closed before the reply
        deadline = time_module.monotonic() + 5
        while not hung and time_module.monotonic() < deadline:
            time_module.sleep(0.02)
        assert hung == [True]
    finally:
        server.shutdown()
        thread.join(10)


def test_ui_poll_keeps_watching_an_unpublished_merge_without_locking_the_camera():
    import subprocess as sp
    source = (BACKEND / "web" / "index.html").read_text()
    body = re.search(r"  async function pollSessionJob\(id\) \{.*?\n  \}", source, re.S).group()
    harness = body + """
      let activeSession='s',jobPollInFlight=false,jobPollTimer=null;const activeJobStates=['queued','running'];
      const elements={};const $=id=>elements[id] || (elements[id]={textContent:'',hidden:false});
      const locked=[],timers=[];const sessionBusy=v=>locked.push(v);const showSession=()=>{};
      const clearTimeout=()=>{};const setTimeout=(cb,ms)=>{timers.push(ms);return 1;};
      let session;const sessionRequest=async()=>({status:'completed',session});
      (async()=>{
        for (const [heavy, publication, expected] of [['queued',undefined,[3000]],['running',undefined,[3000]],
                                                       ['completed','pending',[3000]],['completed','published',[]],
                                                       ['failed',undefined,[]]]) {
          session={id:'s',heavy_job:{status:heavy},publication:publication?{status:publication}:undefined};
          timers.length=0;locked.length=0;
          await pollSessionJob('s');
          assert.deepEqual(timers,expected,heavy+'/'+publication);
          assert.deepEqual(locked,[false]);
        }
      })().catch(e=>{console.error(e);process.exitCode=1;});
    """
    result = sp.run(["node", "-e", "const assert=require('node:assert/strict');\n" + harness],
                    capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]


def test_processing_retry_after_a_reconciled_receipt_requeues_without_the_camera(store, monkeypatch):
    import stack_session
    from session_jobs import SessionJobs
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    record = store.sessions.read(state["id"])
    record["heavy_job"].update(status="failed", error="MemoryError")
    store.sessions.save(record)
    stack_session._atomic_json(store.sessions._directory(state["id"]) / "job.json",
                               {"id": state["id"], "status": "completed", "metadata_reconciled": True})
    shots = store.camera.shots
    jobs = SessionJobs(product_sessions.runner, product_sessions.run)
    submitted = jobs.submit(state["id"])
    assert submitted["status"] == "queued"
    jobs._futures[state["id"]].result(timeout=30)
    assert store.sessions.read(state["id"])["heavy_job"]["status"] == "queued"
    assert store.camera.shots == shots
    jobs.close()


def test_jobs_watchdog_heartbeats_and_exits_on_an_overdue_job(store, monkeypatch, tmp_path):
    import threading as threading_module
    import role_jobs
    exits = []

    def fake_exit(code):
        exits.append(code)
        stop.set()
    monkeypatch.setattr(role_jobs.os, "_exit", fake_exit)
    stop = threading_module.Event()
    overdue = SimpleNamespace(overdue=lambda limit: "b" * 32)
    role_jobs._watchdog(store.sessions, stop, 10.0, overdue, every_s=0.01)
    assert exits == [role_jobs.EXIT_OVERDUE]
    assert (store.sessions.root / runtime_roles.JOBS_HEARTBEAT).is_file()
    assert "target=_watchdog" in (BACKEND / "role_jobs.py").read_text()


# --- round-2 panel follow-ups (PR #77, 2026-09-28) ------------------------------------------------

def test_n1_an_inherited_invocation_id_is_not_the_worker_unit(tmp_path, monkeypatch, capsys):
    """CI runners and systemd-descended shells carry INVOCATION_ID; only the unit's own marker
    makes a worker refuse to run uncapped."""
    root, proc = _fake_cgroup(tmp_path, "cpu io pids", None)
    monkeypatch.setattr(runtime_roles, "CGROUP_ROOT", root)
    monkeypatch.setattr(runtime_roles, "PROC_SELF_CGROUP", proc)
    monkeypatch.setenv("INVOCATION_ID", "inherited")
    monkeypatch.delenv(runtime_roles.ENFORCE_CAP_ENV, raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_ALLOW_UNCAPPED", raising=False)
    assert runtime_roles.require_memory_cap("jobs", platform_name="Linux")["enforced"] is False
    assert "memory cap not enforced" in capsys.readouterr().err  # the verdict is always printed
    sys.path.insert(0, str(ROOT))
    from deploy.provision import render_unit
    for role in ("perception", "jobs"):
        assert f"Environment={runtime_roles.ENFORCE_CAP_ENV}=1" in render_unit(Path("/srv/ca"), Path("/srv/env"), role=role)
    assert runtime_roles.ENFORCE_CAP_ENV not in render_unit(Path("/srv/ca"), Path("/srv/env"))


def test_n1_jobs_once_runs_from_a_systemd_descended_shell(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    env = _child_env(CAMERA_AUTOPILOT_SHOTS_DIR=str(engine.SHOTS_DIR), INVOCATION_ID="from-a-user-service")
    result = subprocess.run([sys.executable, str(BACKEND / "role_jobs.py"), "--once"], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


def test_n2_an_unknown_class_from_perception_is_unavailable_not_an_aborted_shot(short_dir, tmp_path, monkeypatch):
    import subject_recognition
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    calls = []
    server, thread = _serve(short_dir / "p.sock", {
        "classify": lambda path: {"drone": 0.97},
        "recommend": lambda path, k: calls.append("recommend") or {"f_number": 4, "iso": 100, "focal_mm": 35}})
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "p.sock"))
    try:
        with perception_client.shot_deadline():
            evidence = subject_recognition.subject_evidence(str(image), focal_mm=50.0)
            assert evidence["status"] == "unavailable" and "drone" in evidence["reason"]
            with pytest.raises(perception_client.PerceptionUnavailable):
                perception_client.recommend(str(image), 15)  # latched by the rejected reply
        assert calls == []
    finally:
        server.shutdown()
        thread.join(10)
    # A local classifier naming an unknown class also degrades instead of raising.
    assert subject_recognition.subject_evidence(str(image), classifier=lambda p: {"drone": 0.97})["status"] == "unavailable"


def test_n3_a_timelapse_whose_encode_keeps_failing_publishes_like_the_single_process(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "timelapse", 2)
    product_sessions.run(state["id"])
    encodes = []

    def failing(sessions, live):
        encodes.append(1)
        live["video"] = {"status": "failed", "error": "OSError: encoder died"}
        sessions.save(live)
        return live
    monkeypatch.setattr(product_sessions, "_video", failing)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    for _ in range(heavy_jobs.MAX_ATTEMPTS + 2):
        heavy_jobs.run_pending(store.sessions)
    assert len(encodes) == heavy_jobs.MAX_ATTEMPTS  # the same failing encode is not retried forever
    done = store.sessions.read(state["id"])
    assert done["heavy_job"]["status"] == "completed"
    assert done["video"]["status"] == "failed" and done["sequence"]["format"] == "zip/jpeg_sequence"
    assert "encode" in done["heavy_job"]["note"]
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == [state["id"]]
    assert store.sessions.read(state["id"])["publication"]["status"] == "published"


def test_n4_a_failed_publication_after_a_reconciled_receipt_is_retryable(store, monkeypatch):
    import stack_session
    from session_jobs import SessionJobs
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    heavy_jobs.run_pending(store.sessions)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    record = store.sessions.read(state["id"])
    record["publication"] = {"status": "failed"}
    store.sessions.save(record)
    stack_session._atomic_json(store.sessions._directory(state["id"]) / "job.json",
                               {"id": state["id"], "status": "completed", "metadata_reconciled": True,
                                "metadata_status": "write_failed"})
    shots = store.camera.shots
    jobs = SessionJobs(product_sessions.runner, product_sessions.run)
    assert jobs.submit(state["id"])["status"] == "queued"
    jobs._futures[state["id"]].result(timeout=30)
    assert store.sessions.read(state["id"])["publication"]["status"] == "published"
    assert store.camera.shots == shots
    jobs.close()


def test_n5_the_sequence_zip_is_hashed_outside_the_capture_lock(store, monkeypatch):
    from photo_lifecycle import _LOCK
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "timelapse", 2)
    product_sessions.run(state["id"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    heavy_jobs.run_pending(store.sessions)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    owned = []
    real = store.sessions.__class__.artifact

    def spy(self, session_id, name):
        if name == "sequence.zip":
            owned.append(_LOCK._is_owned())
        return real(self, session_id, name)
    monkeypatch.setattr(store.sessions.__class__, "artifact", spy)
    assert product_sessions.publish_deferred() == [state["id"]]
    assert owned and not any(owned)


def test_n6_a_hung_warm_up_exits_for_a_restart_without_binding(short_dir):
    import time as time_module
    import role_perception
    exits = []
    sock = short_dir / "p.sock"

    def on_hang():
        exits.append(True)
        raise SystemExit(role_perception.EXIT_HUNG)
    with pytest.raises(SystemExit):
        role_perception.open_server(sock, {}, warm=lambda: time_module.sleep(5), warm_timeout_s=0.2, on_hang=on_hang)
    assert exits == [True] and not sock.exists()


@pytest.mark.parametrize("raw, expected", [("abc", 60.0), ("0", 60.0), ("nan", 60.0), ("1e9", 60.0), ("45", 45.0)])
def test_env_floats_fall_back_instead_of_crash_looping(monkeypatch, raw, expected):
    monkeypatch.setenv("CAMERA_AUTOPILOT_TEST_FLOAT", raw)
    assert runtime_roles.env_float("CAMERA_AUTOPILOT_TEST_FLOAT", 60.0, 1.0, 3600.0) == expected


def test_perception_heartbeat_stays_fresh_during_a_long_inference(short_dir, tmp_path, monkeypatch):
    import time as time_module
    import role_perception
    monkeypatch.setattr(role_perception, "HEARTBEAT_EVERY_S", 0.05)
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    server, thread = _serve(short_dir / "p.sock", {"classify": lambda path: time_module.sleep(1.0) or {"bird": 1.0}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(short_dir / "p.sock"))
            connection.sendall(json.dumps({"op": "classify", "path": str(image)}).encode())
            connection.shutdown(socket.SHUT_WR)
            time_module.sleep(0.6)  # mid-inference
            age = time_module.time() - server.heartbeat.stat().st_mtime
            assert age < 0.4
            connection.recv(65536)
    finally:
        server.shutdown()
        thread.join(10)


def test_ui_retries_are_immediate_and_the_panel_names_a_stopped_worker():
    source = (BACKEND / "web" / "index.html").read_text()
    assert "if(selectedSessionState?.heavy_job?.status==='failed'){await runSession(activeSession,0);return;}" in source
    assert "await runSession(activeSession,selectedSessionState?.publication?.status==='failed'?0:undefined)" in source
    assert "delaySeconds ?? Number($('session-delay').value || 0)" in source
    assert "job.runtime?.jobs_worker?.alive===false" in source
    assert "runtime_roles.liveness(" in (BACKEND / "app.py").read_text().split("def photo_session_job", 1)[1][:1200]


def test_handheld_and_wifi_pass_the_current_aperture_and_label_a_taste_fallback():
    import brain
    for name in ("handheld_recommend.py", "wifi_autopilot.py", "engine.py"):
        text = (BACKEND / name).read_text()
        assert "fallback_aperture=" in text, name
    assert brain.taste_fell_back(SimpleNamespace(notes=["Taste unavailable (X); retained f/4"])) is True
    assert brain.taste_fell_back(SimpleNamespace(notes=[])) is False
    assert "a7ii_physics_taste_unavailable" in (BACKEND / "engine.py").read_text()


def test_corrupt_heavy_job_does_not_stop_either_sweep(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    bad, good = _create(store.sessions), _create(store.sessions)
    product_sessions.run(bad["id"])
    product_sessions.run(good["id"])
    record = store.sessions.read(bad["id"])
    record["heavy_job"] = "corrupt"
    store.sessions.save(record)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [good["id"]]
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == [good["id"]]


def test_cancel_never_persists_a_pending_recovery_receipt(store):
    from session_jobs import SessionJobs
    state = _session_with_job(store.sessions, "running")
    with store.sessions.operation(state["id"]):
        jobs = SessionJobs(product_sessions.runner, lambda session_id: None)
        with pytest.raises(RuntimeError, match="another process"):
            jobs.cancel(state["id"])
    assert json.loads((store.sessions._directory(state["id"]) / "job.json").read_text())["status"] == "running"
    assert jobs.status(state["id"])["status"] == "recovery_required"
    jobs.close()


# --- the subject tile pass over the perception socket ---------------------------------------------

def test_the_tile_pass_forwards_to_perception_and_validates_every_tile(short_dir, tmp_path, monkeypatch):
    import subject_recognition
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    person = {"person": 0.9, "person_in_motion": 0.05, "static_object": 0.04, "artwork": 0.01}
    still = {"static_object": 0.97, "person": 0.01, "artwork": 0.02}
    replies = {"tiles": [{"box": [0.0, 0.0, 0.4, 0.6], "probabilities": person}]
               + [{"box": [0.3, 0.0, 0.7, 0.6], "probabilities": still}] * 6}
    calls = []
    server, thread = _serve(short_dir / "p.sock", {
        "classify": lambda path: calls.append("classify") or {"static_object": 0.51, "person": 0.2, "bird": 0.29},
        "classify_tiles": lambda path: calls.append("classify_tiles") or replies["tiles"]})
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "p.sock"))
    try:
        with perception_client.shot_deadline():
            evidence = subject_recognition.subject_evidence(str(image), focal_mm=24.0)
        assert calls == ["classify", "classify_tiles"]
        assert (evidence["status"], evidence["decided_by"], evidence["floor_s"]) == ("applied", "tile_0", 1 / 125)
        for bad in ([], [{"box": [0, 0, 1.5, 1], "probabilities": still}],
                    [{"box": [0, 0, 1, 1], "probabilities": {"drone": 0.9}}],
                    [{"box": [0, 0, 1, 1], "probabilities": {"person": float("nan")}}],
                    [{"box": [0, 0, 1], "probabilities": still}], [still] * 17,
                    [{"box": [1, 1, 0, 0], "probabilities": still}],
                    [{"box": [0, 0, 1, 1], "probabilities": {"person": 2.0, "static_object": -1.0}}],
                    [{"box": [0, 0, 1, 1], "probabilities": {"person": 0.9, "static_object": 0.9}}]):
            replies["tiles"] = bad
            with pytest.raises(perception_client.PerceptionUnavailable):
                perception_client.classify_tiles(str(image))
        # A rejected tile reply keeps the whole-frame answer and says the tile pass was unavailable.
        replies["tiles"] = [{"box": [0, 0, 1, 1], "probabilities": {"drone": 0.9}}]
        with perception_client.shot_deadline():
            evidence = subject_recognition.subject_evidence(str(image), focal_mm=24.0)
        assert (evidence["status"], evidence["class"]) == ("no_floor", "static_object")
        assert evidence["tiles"]["status"] == "unavailable"
    finally:
        server.shutdown()
        thread.join(10)
    import role_perception
    assert role_perception.DEFAULT_HANDLERS["classify_tiles"] is role_perception._classify_tiles


def test_an_older_perception_without_the_tile_op_keeps_the_shot_usable(short_dir, tmp_path, monkeypatch):
    """A service that does not know classify_tiles answers promptly; that must not latch the shot,
    or taste retrieval later in the same shot would be refused."""
    import subject_recognition
    image = Path(_jpeg(tmp_path / "meter.jpg", 120))
    calls = []
    server, thread = _serve(short_dir / "p.sock", {
        "classify": lambda path: calls.append("classify") or {"static_object": 0.9, "person": 0.1},
        "recommend": lambda path, k: calls.append("recommend") or {"f_number": 4, "iso": 100, "focal_mm": 35}})
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setenv(runtime_roles.PERCEPTION_SOCKET_ENV, str(short_dir / "p.sock"))
    try:
        with perception_client.shot_deadline():
            evidence = subject_recognition.subject_evidence(str(image), focal_mm=24.0)
            assert (evidence["status"], evidence["tiles"]["status"]) == ("no_floor", "unavailable")
            assert "does not serve" in evidence["tiles"]["reason"]
            assert perception_client.recommend(str(image), 15) == {"f_number": 4.0, "iso": 100, "focal_mm": 35}
        assert calls == ["classify", "recommend"]
        # Only the tile op is optional: a service that does not know "classify" still latches the shot.
        with perception_client.shot_deadline():
            with pytest.raises(perception_client.PerceptionUnavailable) as missing:
                perception_client._call({"op": "no_such_op", "path": str(image.resolve())}, lambda r: r)
            assert not isinstance(missing.value, perception_client.PerceptionOpUnsupported)
            with pytest.raises(perception_client.PerceptionUnavailable, match="already unavailable"):
                perception_client.recommend(str(image), 15)
    finally:
        server.shutdown()
        thread.join(10)
