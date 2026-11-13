"""Background threads must not outlive their test or feed a later test's fake sleep or clock.

A leaked loop that calls the shared `time.sleep` (server.py's keep-alive did, every 8 s) spins
inside any later test that monkeypatches `time.sleep`/`time.monotonic`: it appended to the focus
sweep's recorded sleeps and advanced the long-exposure scheduler's fake clock, which made those
tests fail only in full runs. These checks pin the keep-alive's stoppable wait, the per-test
thread guard in conftest.py and the module-private `module_time` patch."""
import importlib.util
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from conftest import leaked_threads  # noqa: E402


def test_keepalive_waits_on_its_stop_event_never_the_shared_sleep(monkeypatch):
    import server
    server._stop_keepalive()  # the import-time loop; this test drives its own
    pinged = threading.Event()

    class Camera:
        reads = 0

        def get_settings(self):
            self.reads += 1
            if self.reads >= 3:
                pinged.set()
            return {}

    stop = threading.Event()
    monkeypatch.setattr(server, "_keepalive_stop", stop)
    monkeypatch.setattr(server, "_KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(server, "_camera", Camera())
    shared_sleeps = []
    monkeypatch.setattr(time, "sleep", shared_sleeps.append)  # what a later test's fake does
    loop = threading.Thread(target=server._keepalive_loop, name="keepalive-under-test", daemon=True)
    loop.start()
    try:
        assert pinged.wait(5), "keep-alive never read the camera"
    finally:
        stop.set()
        loop.join(5)
    assert not loop.is_alive(), "keep-alive ignored its stop event"
    assert shared_sleeps == [], "keep-alive called the shared time.sleep"


def test_importing_server_starts_a_keepalive_that_stops_and_joins():
    spec = importlib.util.spec_from_file_location("server_keepalive_copy", ROOT / "server.py")
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    try:
        assert copy._keepalive_thread.name == "sony-keepalive" and copy._keepalive_thread.is_alive()
    finally:
        copy._stop_keepalive()
    assert not copy._keepalive_thread.is_alive()


def test_thread_guard_names_leaked_workers_and_non_daemon_threads():
    release = threading.Event()
    before = set(threading.enumerate())
    threads = [threading.Thread(target=release.wait, name=name, daemon=daemon)
               for name, daemon in [("camera-session_0", True), ("plain-non-daemon", False),
                                    ("plain-daemon", True)]]
    for thread in threads:
        thread.start()
    try:
        fatal, stray = leaked_threads(before, grace_s=0.05)
        assert sorted(thread.name for thread in fatal) == ["camera-session_0", "plain-non-daemon"]
        assert [thread.name for thread in stray] == ["plain-daemon"]
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert leaked_threads(before) == ([], [])


def test_autouse_guard_fails_the_test_that_leaks_a_worker(pytester, monkeypatch):
    """The real conftest, in a child pytest process: a leaked worker is an error naming it."""
    pytester.makeconftest((ROOT / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_leak="""
        import threading

        def test_leaks_a_session_worker():
            threading.Thread(target=threading.Event().wait, name="camera-session_leak", daemon=True).start()

        def test_joins_its_worker():
            worker = threading.Thread(target=lambda: None, name="camera-session_joined")
            worker.start()
            worker.join()
    """)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))  # the conftest patches sonyremote.camera
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*test left worker threads running: camera-session_leak (daemon=True)*"])


def test_module_time_patches_one_module_and_leaves_the_shared_time_alone(module_time):
    import stack_session
    real_sleep = time.sleep
    fake_sleeps = []
    module_time(stack_session, sleep=fake_sleeps.append)
    stack_session.time.sleep(3)
    assert fake_sleeps == [3]
    assert time.sleep is real_sleep  # other threads keep the real sleep
    assert stack_session.time.time is time.time  # names not faked stay real
