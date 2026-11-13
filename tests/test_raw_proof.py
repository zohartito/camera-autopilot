"""The 20-shot proof records each delivered file's size and SHA-256 and judges RAW transfer.

Camera-free: the capture path is exercised with a fake gphoto2 CLI (tests/test_capture_serial.py),
the engine with the fake-camera rig, and pi_bringup.sh's proof loop with a fake ``curl`` that
returns canned /shoot answers. Nothing here opens a camera, a port or the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from deploy import raw_proof  # noqa: E402
from test_final_wave_engine import rig  # noqa: E402,F401 — the fake-camera engine fixture

BRINGUP = ROOT / "deploy" / "pi_bringup.sh"
A7III = "Sony Alpha-A7 III (PC Control)"
A7II = "Sony ILCE-7M2 (Control)"
SERIAL = "A7III0SERIAL"
ARW_SIZE = 24_985_000  # inside the observed compressed band


def _digest(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def _files(index: int, *, raw=True, raw_size=ARW_SIZE, raw_sha=None, extra_raw=False):
    files = []
    if raw:
        files.append({"name": f"DSC{index:05d}.ARW", "kind": "raw", "size": raw_size,
                      "sha256": raw_sha if raw_sha is not None else _digest(f"arw-{index}"), "retained": False})
    if extra_raw:
        files.append({"name": f"DSC{index:05d}-2.ARW", "kind": "raw", "size": ARW_SIZE,
                      "sha256": _digest(f"arw2-{index}"), "retained": False})
    files.append({"name": f"DSC{index:05d}.JPG", "kind": "jpeg", "size": 9_000_000,
                  "sha256": _digest(f"jpg-{index}"), "retained": True})
    return files


def _answer(index: int, **kw) -> dict:
    """A /shoot answer shaped like the product's: the final receipt carries downloaded_files."""
    return {"ok": True, "image": f"shot_{index}.jpg",
            "evidence": {"camera_serial": SERIAL,
                         "settings_readback": {"after_final": "verified", "source": "after_final"},
                         "transport_receipt": {"transport": "sony_gphoto2_cli", "downloaded_files": _files(index, **kw)},
                         "meter_transport_receipt": {"downloaded_files": _files(900 + index)}}}


def _rows(n: int, overrides: dict | None = None) -> list[dict]:
    rows = []
    for index in range(1, n + 1):
        kw = (overrides or {}).get(index, {})
        rows.append({"index": index, "http_status": 200, "files": raw_proof.shot_files(_answer(index, **kw))})
    return rows


# ---- the verdict ----------------------------------------------------------------------------------
def test_twenty_in_band_hashed_arws_pass_raw_transfer():
    verdict = raw_proof.raw_transfer_verdict(_rows(20), A7III)
    assert verdict["verdict"] == "passed" and verdict["passed"] is True
    assert verdict["accepted_shots"] == verdict["shots_with_raw"] == 20
    assert verdict["arw_size_bounds"] == [24_000_000, 26_000_000]
    assert verdict["raw_size_min"] == verdict["raw_size_max"] == ARW_SIZE


def test_a_shot_without_an_arw_is_raw_missing_not_a_pass():
    verdict = raw_proof.raw_transfer_verdict(_rows(20, {7: {"raw": False}}), A7III)
    assert verdict["verdict"] == "raw_missing" and verdict["passed"] is False
    assert verdict["raw_missing"] == [7] and verdict["shots_with_raw"] == 19


def test_an_answer_without_downloaded_files_is_raw_missing():
    # The python-gphoto2 fallback and the JPEG-feedback fallback record no downloaded files.
    rows = _rows(3)
    rows[1]["files"] = raw_proof.shot_files({"evidence": {"transport_receipt": {"transport": "gphoto2_python"}}})
    assert rows[1]["files"] is None
    assert raw_proof.raw_transfer_verdict(rows, A7III)["raw_missing"] == [2]


@pytest.mark.parametrize("size", [0, 1_000_000, 23_999_999, 26_000_001, 48_900_000])
def test_an_empty_truncated_or_out_of_band_arw_is_implausible(size):
    verdict = raw_proof.raw_transfer_verdict(_rows(4, {2: {"raw_size": size}}), A7II)
    assert verdict["verdict"] == "raw_implausible"
    assert verdict["raw_implausible"] == [{"index": 2, "name": "DSC00002.ARW", "size": size}]


def test_the_band_edges_are_inclusive():
    rows = _rows(2, {1: {"raw_size": 24_000_000}, 2: {"raw_size": 26_000_000}})
    assert raw_proof.raw_transfer_verdict(rows, A7III)["verdict"] == "passed"


@pytest.mark.parametrize("sha", ["", "abc", "A" * 64, "g" * 64])
def test_an_arw_without_a_valid_sha256_is_unhashed(sha):
    verdict = raw_proof.raw_transfer_verdict(_rows(3, {3: {"raw_sha": sha}}), A7III)
    assert verdict["verdict"] == "raw_unhashed" and verdict["raw_unhashed"] == [3]


@pytest.mark.parametrize("field,value", [("sha256", ""), ("sha256", "not-a-sha256"), ("sha256", None),
                                         ("size", None), ("size", True), ("size", -1), ("size", "24985000")])
def test_the_judge_revalidates_rows_it_did_not_normalise(field, value):
    rows = _rows(2)
    rows[1]["files"][0][field] = value  # a row built by hand, not by shot_files()
    verdict = raw_proof.raw_transfer_verdict(rows, A7III)
    assert verdict["verdict"] == "raw_unhashed" and verdict["raw_unhashed"] == [2]


def test_the_same_arw_hash_in_two_shots_is_flagged():
    same = _digest("one frame")
    verdict = raw_proof.raw_transfer_verdict(_rows(5, {2: {"raw_sha": same}, 4: {"raw_sha": same}}), A7III)
    assert verdict["verdict"] == "duplicate_hash" and verdict["passed"] is False
    assert verdict["duplicate_hashes"] == [{"sha256": same, "files": [
        {"index": 2, "name": "DSC00002.ARW"}, {"index": 4, "name": "DSC00004.ARW"}]}]


def test_two_arws_in_one_shot_are_ambiguous():
    assert raw_proof.raw_transfer_verdict(_rows(3, {1: {"extra_raw": True}}), A7III)["verdict"] == "raw_ambiguous"


def test_an_unknown_body_has_no_bounds_and_never_passes():
    verdict = raw_proof.raw_transfer_verdict(_rows(3), "Sony ILCE-7RM4 (Control)")
    assert verdict["verdict"] == "raw_bounds_unknown" and verdict["arw_size_bounds"] is None


def test_zero_accepted_shots_never_pass():
    rows = [{"index": 1, "http_status": 409, "files": None}]
    assert raw_proof.raw_transfer_verdict(rows, A7III)["verdict"] == "no_accepted_shots"
    assert raw_proof.raw_transfer_verdict([], A7III)["verdict"] == "no_accepted_shots"


def test_only_the_final_captures_receipt_is_read():
    answer = _answer(1, raw=False)  # the meter frame's receipt still lists an ARW
    assert [f["kind"] for f in raw_proof.shot_files(answer)] == ["jpeg"]
    assert raw_proof.shot_files({"evidence": {"downloaded_files": _files(1)}}) is None


# ---- the engine hands the final capture's download record to /shoot ------------------------------
def test_shoot_evidence_carries_the_final_captures_downloaded_files(rig):
    real_capture = rig.camera.capture

    def capture(download=True):
        path = real_capture(download=download)
        rig.camera.last_capture_receipt = {"transport": "sony_gphoto2_cli",
                                           "downloaded_files": _files(rig.camera.capture_count)}
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot("natural", variant_id="natural")
    assert rig.camera.capture_count == 2
    final = raw_proof.shot_files({"evidence": shot.evidence})
    assert [f["name"] for f in final] == ["DSC00002.ARW", "DSC00002.JPG"]
    assert shot.evidence["meter_transport_receipt"]["downloaded_files"][0]["name"] == "DSC00001.ARW"


# ---- pi_bringup.sh --phase proof --apply with a fake curl ------------------------------------------
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
state = os.environ["FAKE_CURL_DIR"]
counter = os.path.join(state, "count")
n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(n))
body = json.loads(args[args.index("--data") + 1])
open(os.path.join(state, f"request-{n}.json"), "w").write(json.dumps(body))
answer = os.path.join(state, f"answer-{n}.json")
open(out, "w").write(open(answer).read())
sys.stdout.write("200 0.42")
'''


def _run_proof(tmp_path: Path, answers: list[dict], model=A7III):
    fake_bin, state, receipts = tmp_path / "bin", tmp_path / "curl-state", tmp_path / "receipts"
    fake_bin.mkdir()
    state.mkdir()
    (fake_bin / "curl").write_text(FAKE_CURL)
    (fake_bin / "curl").chmod(0o755)
    for name in ("pgrep", "ss", "vcgencmd", "gphoto2"):  # nothing may discover or touch hardware
        (fake_bin / name).write_text("#!/bin/sh\nexit 1\n")
        (fake_bin / name).chmod(0o755)
    for index, answer in enumerate(answers, 1):
        (state / f"answer-{index}.json").write_text(json.dumps(answer))
    token = tmp_path / "token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    env = dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}", FAKE_CURL_DIR=str(state),
               TMPDIR=str(tmp_path))
    result = subprocess.run(
        ["bash", str(BRINGUP), "--apply", "--phase", "proof", "--model", model, "--port", "usb:001,005",
         "--serial", SERIAL, "--token-file", str(token), "--shots", str(len(answers)),
         "--http-port", "9", "--service-pid", "999999999", "--receipt-dir", str(receipts)],
        capture_output=True, text=True, env=env, timeout=240)
    proof = json.loads((receipts / "proof.json").read_text()) if (receipts / "proof.json").exists() else None
    return result, proof, receipts


def test_board_proof_records_every_files_size_and_hash_and_passes_raw(tmp_path):
    result, proof, receipts = _run_proof(tmp_path, [_answer(i) for i in range(1, 21)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert proof["pass"] is True and proof["no_drop_pass"] is True
    raw = proof["raw_transfer"]
    assert raw["verdict"] == "passed" and raw["shots_with_raw"] == 20
    first = proof["shots"][0]["files"]
    assert first[0] == {"name": "DSC00001.ARW", "kind": "raw", "size": ARW_SIZE,
                        "sha256": _digest("arw-1"), "retained": False}
    assert first[1]["name"] == "DSC00001.JPG" and first[1]["sha256"] == _digest("jpg-1")
    # The per-shot line runs under the board's python3 (3.11 here too): it used to be a SyntaxError.
    assert (f"shot 1/20: HTTP 200 img=shot_1.jpg after_final=verified arw={ARW_SIZE}B "
            f"sha256={_digest('arw-1')[:12]} 0.42s") in result.stdout
    assert "PROOF: 20/20 verified 200s; RAW passed (20/20 ARW" in result.stdout
    # Nothing is written beside the receipts.
    assert sorted(p.name for p in receipts.iterdir()) == ["proof-shots.jsonl", "proof.json"]


@pytest.mark.parametrize("override,verdict", [
    ({"raw": False}, "raw_missing"),
    ({"raw_size": 3_145_728}, "raw_implausible"),
    ({"raw_sha": _digest("arw-1")}, "duplicate_hash"),
])
def test_board_proof_fails_on_a_raw_defect_even_when_every_shot_was_accepted(tmp_path, override, verdict):
    answers = [_answer(1), _answer(2, **override), _answer(3)]
    result, proof, _ = _run_proof(tmp_path, answers)
    assert result.returncode == 1
    assert proof["no_drop_pass"] is True and proof["pass"] is False
    assert proof["raw_transfer"]["verdict"] == verdict
    assert f"RAW {verdict}" in result.stdout


def test_board_proof_on_a_body_without_bounds_is_never_a_raw_pass(tmp_path):
    result, proof, _ = _run_proof(tmp_path, [_answer(1)], model="Sony ILCE-7RM4 (Control)")
    assert result.returncode == 1 and proof["raw_transfer"]["verdict"] == "raw_bounds_unknown"


def test_proof_dry_run_states_the_raw_criterion_and_writes_nothing(tmp_path):
    receipts = tmp_path / "receipts"
    result = subprocess.run(["bash", str(BRINGUP), "--dry-run", "--phase", "proof", "--model", A7III,
                             "--port", "usb:001,005", "--serial", SERIAL, "--receipt-dir", str(receipts)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "every accepted shot has one ARW of 24000000-26000000 bytes with a SHA-256" in result.stdout
    assert not receipts.exists()
