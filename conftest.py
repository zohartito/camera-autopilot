"""Shared isolation for standalone contract suites collected by pytest."""
import importlib.util
import os
import sys
import threading
from pathlib import Path
import time
import warnings

import pytest

pytest_plugins = ["pytester"]  # tests/test_thread_hygiene.py runs the guard in a child session

# Product worker threads by name prefix: server.py keep-alive, the session-job executor and its
# timers, the IMU reader, the web handheld listener and the handheld recommendation worker.
WORKER_THREAD_PREFIXES = ("sony-keepalive", "camera-session", "imu-read", "handheld-")
THREAD_GRACE_S = 2.0
_real_monotonic = time.monotonic  # tests may patch the shared time module; the guard must not care


def leaked_threads(before, grace_s=THREAD_GRACE_S):
    """Threads started since `before` that are still alive after a short grace join.

    Returns (fatal, stray): fatal are non-daemon threads and named product workers, which a test
    must stop and join; stray are other daemon threads. A leaked loop keeps running into later
    tests, where a monkeypatched `time.sleep`/`time.monotonic` turns it into a spin loop that
    feeds the later test's fake."""
    candidates = [thread for thread in threading.enumerate() if thread not in before and thread.is_alive()]
    deadline = _real_monotonic() + grace_s
    for thread in candidates:
        thread.join(max(0.0, deadline - _real_monotonic()))  # Thread.join never calls time.sleep
    alive = [thread for thread in candidates if thread.is_alive()]
    fatal = [thread for thread in alive
             if not thread.daemon or thread.name.startswith(WORKER_THREAD_PREFIXES)]
    stray = [thread for thread in alive if thread not in fatal]
    return fatal, stray


@pytest.fixture(autouse=True)
def no_leaked_threads():
    """Fail a test that leaves a product worker or non-daemon thread running; warn on other daemons."""
    before = set(threading.enumerate())
    yield
    fatal, stray = leaked_threads(before)
    if stray:
        warnings.warn(f"test left daemon threads running: {', '.join(t.name for t in stray)}", pytest.PytestWarning)
    if fatal:
        names = ", ".join(f"{t.name} (daemon={t.daemon})" for t in fatal)
        pytest.fail(f"test left worker threads running: {names}; stop and join them at teardown", pytrace=False)


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_STORES = {REPO_ROOT / "artifacts" / "demo-shots", REPO_ROOT / "backend" / "shots"}  # engine's defaults


def _backend_module(name):
    """backend/<name>.py under its import name, loaded once so a later `import <name>`
    (after backend joins sys.path) finds this same module."""
    module = sys.modules.get(name)
    source = REPO_ROOT / "backend" / f"{name}.py"
    if module is None and source.is_file():  # absent when this conftest is copied (pytester)
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]  # never leave a half-loaded module behind
            raise
    return module


def _uncertain_receipts_module():
    return _backend_module("uncertain_receipts")


def _never_run_host_log(argv, timeout_s):
    """Under pytest the host's unified log is never read (tests inject their own runner)."""
    raise RuntimeError("pytest never runs the host's /usr/bin/log (conftest.py)")


def _never_list_host_processes():
    """Under pytest the host's process list is never read (tests inject their own lister)."""
    raise RuntimeError("pytest never lists the host's processes (conftest.py)")


class _ModuleTime:
    """A module-private stand-in for `time`: the given functions are fakes, the rest are real."""

    def __init__(self, **fakes):
        self.__dict__.update(fakes)

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def module_time(monkeypatch):
    """Patch one module's `time` reference, never the shared time module another thread uses:
    `module_time(stack_session, sleep=fake_sleep, monotonic=fake_clock)`."""
    def patch(module, **fakes):
        fake = _ModuleTime(**fakes)
        monkeypatch.setattr(module, "time", fake)
        return fake
    return patch


@pytest.fixture(autouse=True)
def isolate_macos_camera_service(monkeypatch, tmp_path_factory):
    """Camera-free tests must not terminate the host's PTP service or read its unified log or
    process list, and a fake CLI capture must stage its download in a temporary shot store,
    never the repository's backend/shots."""
    monkeypatch.setattr("sonyremote.camera._release_macos_camera_service", lambda: None)
    monkeypatch.setattr("sonyremote.camera._run_host_log", _never_run_host_log)
    host_clients = _backend_module("host_camera_clients")
    if host_clients is not None:
        monkeypatch.setattr(host_clients, "_list_processes", _never_list_host_processes)
    staging_store = tmp_path_factory.getbasetemp() / "default-shot-store"
    monkeypatch.setattr("sonyremote.camera._default_quarantine_dir", lambda: staging_store / "quarantine")
    # An uncertain /shoot keeps its receipt beside the store; a test that left the store at one of
    # the repository's default stores must not write there. Any other store is used as given.
    receipts = _uncertain_receipts_module()
    if receipts is not None:
        real_persist = receipts.persist

        def persist(store, content, **options):
            if Path(os.path.normpath(Path(store).absolute())) in DEFAULT_STORES:  # symlinks not followed
                staging_store.mkdir(mode=0o700, exist_ok=True)
                store = staging_store
            return real_persist(store, content, **options)

        monkeypatch.setattr(receipts, "persist", persist)
    engine = sys.modules.get("engine")  # backend's sibling helper, when a test imported it
    if engine is not None:
        monkeypatch.setattr(engine, "_free_usb", lambda: None, raising=False)


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    """Supply legacy Path arguments and restore persistence globals they replace."""
    for name, attributes in {
        "engine": ("SHOTS_DIR",),
        "app": ("SHOTS_DIR",),
        "feedback": ("FEEDBACK_FILE",),
    }.items():
        module = sys.modules.get(name)
        if module is not None:
            for attribute in attributes:
                if hasattr(module, attribute):
                    monkeypatch.setattr(module, attribute, getattr(module, attribute))
    return tmp_path
