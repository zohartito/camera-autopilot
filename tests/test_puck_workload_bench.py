"""Camera-free input and process-evidence checks for the portable benchmark."""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from PIL import Image

SPEC = importlib.util.spec_from_file_location("puck_workload_bench", Path(__file__).parents[1] / "tools/puck_workload_bench.py")
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


@pytest.fixture
def frames(tmp_path):
    paths = [tmp_path / f"frame_{index}.jpg" for index in range(3)]
    for index, path in enumerate(paths):
        Image.new("RGB", (48, 32), (80 + index * 10, 100, 120)).save(path)
    return [str(path) for path in paths]


def test_fresh_process_metrics_and_originals(frames):
    import cv2
    report = bench.run_benchmark(frames, ["hdr", "night", "crowd"])
    assert report["schema"] == 1
    assert report["originals_unchanged"] is True
    # Every backend module a mode runs is hashed, including crowd_auto's estimator and recovery.
    assert set(report["source_hashes"]) == {
        "tools/puck_workload_bench.py", "backend/stacking.py", "backend/image_safety.py",
        "backend/stack_session.py", "backend/crowd_auto_mask.py", "backend/crowd_recovery.py"}
    assert all(len(digest) == 64 for digest in report["source_hashes"].values())
    assert report["versions"]["cv2"] == cv2.__version__
    assert report["opencv_build"]["module_path"] == cv2.__file__
    assert len(report["opencv_build"]["build_info_sha256"]) == 64
    for distribution in ("opencv-python", "opencv-python-headless",
                         "opencv-contrib-python", "opencv-contrib-python-headless"):
        try:
            installed = bench.importlib.metadata.version(distribution)
        except bench.importlib.metadata.PackageNotFoundError:
            continue
        assert report["versions"][distribution] == installed
    assert "power and battery life" in report["scope"]["excluded"]
    assert len({run["pid"] for run in report["runs"]}) == 3
    for run in report["runs"]:
        assert run["pid"] != os.getpid()
        assert run["opencv_version"] == cv2.__version__
        assert run["status"] == "completed"
        assert run["process_wall_s"] >= run["workload_wall_s"] >= run["merge_wall_s"] > 0
        assert run["peak_rss_bytes"] > 0
        assert run["output"]["width"] == 48
        assert run["output"]["retained"] is False
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("kind", ["one", "duplicate", "symlink", "non_jpeg", "mismatch", "truncated"])
def test_rejects_invalid_sources_before_process(frames, tmp_path, monkeypatch, kind):
    def no_process(*args, **kwargs):
        pytest.fail("invalid inputs reached child process")
    monkeypatch.setattr(bench.subprocess, "run", no_process)
    if kind == "one":
        frames = frames[:1]
    elif kind == "duplicate":
        frames[1] = frames[0]
    elif kind == "symlink":
        link = tmp_path / "link.jpg"
        link.symlink_to(frames[0])
        frames[1] = str(link)
    elif kind == "non_jpeg":
        Image.new("RGB", (48, 32)).save(frames[1], format="PNG")
    elif kind == "mismatch":
        Image.new("RGB", (49, 32)).save(frames[1])
    else:
        path = Path(frames[1])
        path.write_bytes(path.read_bytes()[:100])
    with pytest.raises((ValueError, OSError)):
        bench.run_benchmark(frames, ["hdr"])


def test_timeout_report_has_no_invented_metrics(frames, monkeypatch):
    def timeout(*args, **kwargs):
        raise bench.subprocess.TimeoutExpired("test", 1)
    monkeypatch.setattr(bench.subprocess, "run", timeout)
    run = bench.run_benchmark(frames, ["hdr"], timeout_s=1)["runs"][0]
    assert run["status"] == "timeout"
    assert "peak_rss_bytes" not in run
    assert "output" not in run


def test_existing_output_never_overwritten(frames):
    before = Path(frames[0]).read_bytes()
    with pytest.raises(SystemExit):
        bench.main([*frames, "--output", frames[0]])
    assert Path(frames[0]).read_bytes() == before


def test_cli_receipt_private_and_valid(frames, tmp_path):
    destination = tmp_path / "receipt.json"
    assert bench.main([*frames, "--mode", "hdr", "--output", str(destination)]) == 0
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text())["originals_unchanged"]


def test_existing_backend_memory_rejection_reported(monkeypatch, frames):
    import stacking
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES", 1)
    result = bench.worker("hdr", frames)
    assert result["status"] == "rejected"
    assert "working-memory budget" in result["error"]
    assert "output" not in result


def test_duplicate_modes_rejected_before_child(frames, monkeypatch):
    def no_process(*args, **kwargs):
        pytest.fail("duplicate modes reached child process")
    monkeypatch.setattr(bench.subprocess, "run", no_process)
    with pytest.raises(ValueError, match="distinct modes"):
        bench.run_benchmark(frames, ["hdr"] * 4)
    with pytest.raises(ValueError, match="distinct modes"):
        bench.run_benchmark(frames, ["hdr", "hdr"])


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_postrun_source_replacement_fails_bounded_integrity_check(frames, monkeypatch, replacement):
    def replace_source(*args, **kwargs):
        source = Path(frames[0])
        source.unlink()
        if replacement == "symlink":
            source.symlink_to(frames[1])
        else:
            os.mkfifo(source)
        return bench.subprocess.CompletedProcess(args[0], 0, json.dumps({"mode": "hdr", "status": "completed"}), "")
    monkeypatch.setattr(bench.subprocess, "run", replace_source)
    report = bench.run_benchmark(frames, ["hdr"])
    assert report["originals_unchanged"] is False
    assert len(report["original_integrity_errors"]) == 1


@pytest.mark.parametrize("mode", ["long", "focus", "crowd_auto"])
def test_every_merge_mode_is_benchmarkable(frames, mode):
    result = bench.worker(mode, frames)
    assert result["status"] == "completed", result
    assert result["output"]["width"] == 48 and result["peak_rss_bytes"] > 0
    if mode == "crowd_auto":
        # The shipping session merge, not the bare median: it reports its estimator verdict.
        assert result["crowd_auto"]["background_estimate"] in ("ok", "insufficient_background_visibility")


def test_crowd_auto_merge_time_excludes_the_session_merges_own_file_encodes(frames, monkeypatch):
    import time
    import cv2
    original = cv2.imwrite
    delay = 0.05

    def slow_imwrite(*args, **kwargs):
        time.sleep(delay)
        return original(*args, **kwargs)

    monkeypatch.setattr(cv2, "imwrite", slow_imwrite)
    result = bench.worker("crowd_auto", frames)
    assert result["status"] == "completed", result
    timing = result["crowd_auto"]
    # 3 registered JPEGs, 3 mask PNGs, the result and 3 maps, all inside the session merge.
    assert timing["files_encoded"] >= 10
    assert timing["file_encode_wall_s"] >= delay * timing["files_encoded"]
    # Were the encodes counted, merge_wall_s would equal the whole call.
    assert result["merge_wall_s"] == pytest.approx(
        timing["session_merge_wall_s"] - timing["file_encode_wall_s"])
    assert 0 < result["merge_wall_s"] < timing["session_merge_wall_s"] - delay * timing["files_encoded"] + 1e-6
    assert cv2.imwrite is slow_imwrite  # the timing wrapper is removed after the call
