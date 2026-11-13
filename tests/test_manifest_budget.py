"""Session manifest byte budget: a 240-frame run must stay readable, recoverable and publishable.

Camera-free. The fake body returns transport receipts shaped like a real A7 III session's
(artifacts/receipts-0.3-2026-09-27, same keys and value lengths, synthetic values) and the real
Sony ladders, so the metered-timelapse records carry real-size lens and shutter ladders built by
the product code. Before evidence sidecars a real frame cost 2.8-3.8 KB in the manifest (9.6 KB for
Holy Grail) and the reader refused anything over 512 KiB, so a 240-frame run became unreadable part
way through.
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend"):
    sys.path.insert(0, str(entry))

import stack_session  # noqa: E402
from stack_session import SessionRunner  # noqa: E402

MODEL = "Sony Alpha-A7 III (PC Control)"
PORT = "usb:001,004"
SERIAL = "TESTSERIAL01"
LEGACY_READ_LIMIT = 512 * 1024

_ISO = ["50", "64", "80", "100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000",
        "1250", "1600", "2000", "2500", "3200", "4000", "5000", "6400", "8000", "10000", "12800",
        "16000", "20000", "25600", "32000", "40000", "51200"]
_APERTURE = ["f/" + (f"{value:g}") for value in (
    1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0, 9.0,
    10.0, 11.0, 13.0, 14.0, 16.0, 18.0, 20.0, 22.0, 25.0, 29.0, 32.0, 36.0, 42.0, 45.0, 50.0, 57.0, 64.0)]
_SHUTTER = ["30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10", "25/10", "2", "16/10", "13/10",
            "1", "8/10", "6/10", "5/10", "4/10", "1/3", "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15",
            "1/20", "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200",
            "1/250", "1/320", "1/400", "1/500", "1/640", "1/800", "1/1000", "1/1250", "1/1600", "1/2000",
            "1/2500", "1/3200", "1/4000", "1/5000", "1/6400", "1/8000"]


def realistic_receipt(index: int) -> dict:
    """Same keys and value lengths as a real A7 III CLI capture receipt (2026-09-27); synthetic values."""
    verification = {"serial": SERIAL, "method": "os_usb_serial_at_selected_port", "os_source": "ioreg",
                    "model": MODEL, "port": PORT}
    return {
        "model": MODEL, "port": PORT, "transport": "sony_gphoto2_cli",
        "timings": {"clock": "monotonic", "sensor_exposure_s": None,
                    "sensor_exposure_reason": "Sony CLI combines trigger, camera wait and download; "
                                              "sensor timestamps are unavailable.",
                    "configured_shutter_s": 0.008, "capture_timeout_s": 18.016,
                    "usb_release_s": 0.00815041601890698, "settle_s": 0.7050145419780165,
                    "trigger_and_download_s": 5.248333458031993, "local_copy_s": 0.0014312499552033842,
                    "reconnect_s": 0.39766954199876636, "total_s": 6.372134041041136 + index},
        "path": f"/Users/example/projects/camera-autopilot/captures/20260927-173712_capt_DSC{index:05d}.JPG",
        "capture_confirmed": True, "reconnect_ok": True, "warning": None,
        "serial": SERIAL, "serial_verification": dict(verification),
        "capture_deadline": {"confirmed_shutter_setting": "1/125", "configured_shutter_s": 0.008,
                             "possible_dark_frame_s": 0.008,
                             "dark_frame_state": "unverified; possible equal-duration noise-reduction dark frame",
                             "transfer_allowance_s": 18.0, "subprocess_timeout_s": 18.016,
                             "sensor_exposure_measured": False,
                             "policy": "confirmed configured shutter plus possible equal-duration dark frame "
                                       "and bounded driver/transfer allowance"},
        "capture_policy": {"max_enabled_shutter_s": 1.0, "status": "provisional_short_exposure_policy",
                           "reason": "Long-shutter Sony image retrieval failed physical validation; "
                                     "alternate trigger/wait transport is unverified."},
        "pre_shutter_serial_verification": dict(verification),
        "post_shutter_serial_verification": dict(verification),
        "delivery": {"source_removed": True, "directory_fsync": "ok"},
    }


def unconfirmed_receipt(index: int) -> dict:
    """A realistic receipt whose download folder could not be flushed (#61/#70's evidence)."""
    from sonyremote.camera import _delivery_durability
    receipt = realistic_receipt(index)
    receipt["delivery"] = {"source_removed": True, "directory_fsync": "OSError: [Errno 5] Input/output error"}
    receipt["delivery_durability"] = _delivery_durability(receipt["delivery"])
    return receipt


def install_fake_body(tmp_path, monkeypatch, *, on_capture=None, grey=40, receipt=realistic_receipt):
    """A fake A7 III for product_sessions.run: real Sony ladders, realistic receipts, tiny JPEGs."""
    from PIL import Image
    import engine
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    live = {"iso": "100", "aperture": "f/8", "shutter": "1/60", "white_balance": "Daylight",
            "exposure_comp": "0", "focus_mode": "Manual", "image_quality": "RAW+JPEG (Fine)"}
    shots = []

    class Camera:
        verified_serial = SERIAL
        serial_verification = {"serial": SERIAL, "method": "os_usb_serial_at_selected_port",
                               "os_source": "ioreg", "model": MODEL, "port": PORT}

        def __init__(self, **kwargs): self.last_capture_receipt = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=MODEL, port=PORT)
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key): return {"iso": _ISO, "aperture": _APERTURE, "shutter": _SHUTTER}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)

        def capture(self, **kwargs):
            shots.append(dict(live))
            self.last_capture_receipt = receipt(len(shots) - 1)
            path = tmp_path / f"source_{len(shots):04d}.jpg"
            Image.new("RGB", (64, 48), (grey, grey, grey)).save(path, format="JPEG")
            if on_capture:
                on_capture(len(shots))
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    return shots


def install_fake_encoder(monkeypatch):
    """ffmpeg stand-in, so the final render (and Holy Grail's default deflicker) runs on every host."""
    import timelapse_video

    def render(frames_dir, frame_count, fps, pending, *, preview=False, tools=None):
        assert len(list(Path(frames_dir).glob("frame_*.jpg"))) >= frame_count
        Path(pending).write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))
        return {"codec_name": "h264", "width": 1920, "height": 1080, "nb_read_frames": str(frame_count),
                "playback_duration_s": frame_count / fps}

    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})
    monkeypatch.setattr(timelapse_video, "render_h264", render)


def compact_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())


def legacy_frame_bytes(frame: dict, evidence: dict) -> int:
    """What this frame cost in a manifest written before sidecars (inline evidence, indent=2)."""
    inline = {key: value for key, value in frame.items() if key != "evidence_sidecar"}
    inline.update(evidence)
    empty = len(json.dumps({"frames": []}, indent=2))
    return len(json.dumps({"frames": [inline]}, indent=2)) - empty


def path_free_bytes(frame: dict) -> int:
    """A frame record without its two absolute paths, whose length depends on the host."""
    return compact_bytes(frame) - 2 * len(frame["destination"])


# Pinned from 2026-09-27 measurements of the compacted records (Holy Grail ~1.16 KB, others ~0.8 KB).
PATH_FREE_RECORD_PIN_BYTES = 1280
# FRAME_RECORD_BUDGET_BYTES must hold a record whose two absolute paths are this long (a
# production session path is ~115 characters). Checked path-free so no host's tmp path changes it.
BUDGETED_PATH_CHARS = 256


def within_record_budget(frame: dict) -> bool:
    return path_free_bytes(frame) + 2 * BUDGETED_PATH_CHARS <= stack_session.FRAME_RECORD_BUDGET_BYTES


def manifest_size(sessions: SessionRunner, session_id: str) -> int:
    return (sessions._directory(session_id) / "manifest.json").stat().st_size


def test_the_read_limit_is_derived_from_the_worst_case_and_still_reads_every_legacy_manifest():
    per_frame = stack_session.FRAME_RECORD_BUDGET_BYTES + stack_session.FRAME_ROLLUP_BUDGET_BYTES
    planned = (stack_session.SESSION_BASE_BUDGET_BYTES + stack_session.INLINE_FRAME_BUDGET_BYTES
               + stack_session.MAX_SEQUENCE_FRAMES * per_frame)
    assert stack_session.MAX_MANIFEST_BYTES == stack_session.MANIFEST_BUDGET_MARGIN * planned
    assert stack_session.MANIFEST_BUDGET_MARGIN >= 2
    # Every manifest the old fixed limit could read stays readable.
    assert stack_session.MAX_MANIFEST_BYTES >= LEGACY_READ_LIMIT
    # A compacted frame record plus its rollup share is pinned well below a legacy inline frame.
    assert stack_session.FRAME_RECORD_BUDGET_BYTES <= 2048
    assert stack_session.FRAME_ROLLUP_BUDGET_BYTES <= 512


def test_a_240_frame_holy_grail_completes_reads_back_recovers_after_a_restart_and_publishes(tmp_path, monkeypatch):
    import product_sessions
    frames = stack_session.MAX_SEQUENCE_FRAMES
    holder = {}

    def stop_part_way(count):
        if count == 120:  # the process stops here; a new one picks the session up from disk
            (holder["runner"]._directory(holder["id"]) / "cancel").touch()

    install_fake_body(tmp_path, monkeypatch, on_capture=stop_part_way)
    install_fake_encoder(monkeypatch)
    sessions = product_sessions.runner()
    state = sessions.create("timelapse", frames, timelapse_mode="holy_grail",
                            timelapse_options={"max_shutter_s": 1 / 60},
                            metadata={"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL})
    holder.update(runner=sessions, id=state["id"])
    first = product_sessions.run(state["id"])
    assert first["status"] == "cancelled", first.get("error")
    assert first["progress"]["accepted"] == 120

    # Simulated restart: a fresh runner reads everything back from disk and resumes the rest.
    restarted = product_sessions.runner()
    restarted.resume(state["id"])
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    assert result["progress"]["accepted"] == frames
    assert result["publication"]["status"] == "published", result.get("publication_error")
    assert result["video"]["status"] == "completed" and result["video"]["deflicker"]["frame_count"] == frames

    # The restart carried the Holy Grail meter anchor exactly as a continuous run does.
    meters = [frame["applied"]["timelapse_meter"] for frame in result["frames"]]
    for index in range(2, frames):
        assert meters[index]["measured_ev"] == pytest.approx(meters[index - 1]["actual_ev"]), index
    assert "scene_ev_estimate" in meters[119]

    # Readable, listable and within the derived limit; the old limit would have refused it.
    size = manifest_size(restarted, state["id"])
    assert size <= stack_session.MAX_MANIFEST_BYTES
    assert restarted.read(state["id"])["progress"]["accepted"] == frames
    assert [item["id"] for item in restarted.list()] == [state["id"]]
    evidence = [restarted.frame_evidence(result, index) for index in range(frames)]
    legacy_total = sum(legacy_frame_bytes(frame, evidence[frame["index"]]) for frame in result["frames"])
    assert legacy_total > LEGACY_READ_LIMIT  # the defect: frames alone overflowed the old reader

    # Per-frame manifest growth is pinned; the full evidence is one hashed sidecar per frame.
    for frame in result["frames"]:
        assert within_record_budget(frame), frame["index"]
        assert path_free_bytes(frame) <= PATH_FREE_RECORD_PIN_BYTES, frame["index"]
        assert frame["evidence_sidecar"]["file"] == f"frame_{frame['index']:04d}.evidence.json"
    rollup = compact_bytes(result["timelapse_cadence"]) + compact_bytes(result["video"])
    assert rollup / frames <= stack_session.FRAME_ROLLUP_BUDGET_BYTES

    # Nothing any reader needs was lost: the full receipt and ladders come back verified.
    last = evidence[-1]
    assert last["transport_receipt"] == realistic_receipt(frames - 1)
    assert last["applied"]["timelapse_meter"]["transport_limits"]["usable_shutter_labels"]
    assert last["applied"]["timelapse_meter"]["mounted_lens_limits"]["raw_driver_apertures"]
    assert last["applied"]["timelapse_meter"]["source_sha256"] == result["frames"][-2]["receipt"]["sha256"]
    assert last["applied"]["exposure_transition"]["after"] == last["applied"]["exposure_transition"]["requested"]
    assert result["frames"][-1]["transport_receipt"]["capture_confirmed"] is True
    assert result["frames"][-1]["transport_receipt"]["timings"]["total_s"] == last["transport_receipt"]["timings"]["total_s"]

    # The portable sequence keeps every frame's full applied settings.
    directory = restarted._directory(state["id"])
    with zipfile.ZipFile(directory / "sequence.zip") as archive:
        portable = json.loads(archive.read("sequence.json"))
    assert len(portable["frames"]) == frames
    assert portable["frames"][-1]["applied"] == last["applied"]

    # And the whole session still deletes within its entry budget.
    deleted = restarted.delete(state["id"])
    assert deleted["deleted_count"] >= 2 * frames
    assert not directory.exists()


def test_a_changed_evidence_sidecar_is_refused(tmp_path, monkeypatch):
    import product_sessions
    install_fake_body(tmp_path, monkeypatch)
    install_fake_encoder(monkeypatch)
    sessions = product_sessions.runner()
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                            metadata={"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL})
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    sidecar = sessions._directory(state["id"]) / "frame_0001.evidence.json"
    payload = json.loads(sidecar.read_text())
    payload["transport_receipt"]["capture_confirmed"] = False
    sidecar.write_text(json.dumps(payload, indent=2) + "\n")
    with pytest.raises(ValueError, match="evidence"):
        sessions.frame_evidence(sessions.read(state["id"]), 1)
    assert sessions.frame_evidence(sessions.read(state["id"]), 0)["transport_receipt"]["capture_confirmed"] is True


@pytest.mark.parametrize("mode,frames,policy", [
    ("hdr", 9, None), ("night", 16, None), ("crowd_remove", 8, None), ("panorama", 12, None),
    ("long_exposure", 16, None), ("timelapse", 6, "fixed"), ("timelapse", 6, "auto_custom"),
    ("timelapse", 6, "holy_grail"),
])
def test_per_frame_manifest_growth_is_pinned_for_every_mode(tmp_path, monkeypatch, mode, frames, policy):
    import product_sessions
    from PIL import Image
    install_fake_body(tmp_path, monkeypatch)
    install_fake_encoder(monkeypatch)

    def merge(stack_mode, paths, out_path, **kwargs):
        Image.new("RGB", (64, 48), (40, 40, 40)).save(out_path, format="JPEG")
        return {"out_path": out_path, "mode": stack_mode, "frames_merged": len(paths)}

    monkeypatch.setattr(product_sessions, "merge_session_frames", merge)
    sessions = product_sessions.runner()
    options = {"timelapse_mode": policy} if policy else {}
    metadata = {"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL}
    if mode == "panorama":
        metadata["preview_every"] = 0  # the mosaic previews are not what this measures
    state = sessions.create(mode, frames, ev_step=0.5, **options, metadata=metadata)
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    for frame in result["frames"]:
        evidence = sessions.frame_evidence(result, frame["index"])
        record = compact_bytes(frame)
        assert within_record_budget(frame), (mode, policy, record)
        assert path_free_bytes(frame) <= PATH_FREE_RECORD_PIN_BYTES, (mode, policy, record)
        # The compact record is a fraction of what the same frame cost inline before sidecars.
        assert record * 2 < legacy_frame_bytes(frame, evidence), (mode, policy)
        assert evidence["transport_receipt"] == realistic_receipt(frame["index"])
        # The manifest keeps the exposure a frame was taken at and the capture confirmation.
        for key in ("iso", "aperture", "shutter"):
            assert frame["applied"][key] == evidence["applied"][key]
        if mode != "timelapse":
            # /shoot's Fixed/Auto stacks (hdr, night, long_exposure, crowd_remove) return and save
            # Shot.applied["session_frames"] from the manifest's frames: for these modes applied is the
            # camera's flat settings readback, so the compact view is the whole record. A stack mode
            # that records nested evidence must hydrate from frame_evidence in _session_capture.
            assert frame["applied"] == evidence["applied"], (mode, frame["index"])
        assert frame["transport_receipt"]["capture_confirmed"] is True
        assert frame["transport_receipt"]["serial"] == SERIAL


def _legacy_manifest(sessions: SessionRunner, frames: int, accepted: int, *, ladder_length: int = 60,
                     full_precision: bool = False, target_bytes: int | None = None) -> str:
    """A manifest as written before sidecars: inline receipts and ladders, indent=2, no format marker.

    Its size depends on the absolute tmp path every frame embeds, so a test that needs a size band
    passes ``target_bytes``: each accepted frame's inline receipt is padded so that the file is
    exactly that many bytes on any host. The natural size must stay below the target."""
    from PIL import Image
    state = sessions.create("timelapse", frames, timelapse_mode="holy_grail")
    state.pop("evidence_format", None)
    directory = sessions._directory(state["id"])
    # Real ladders hold full-precision floats (1/3 s is 0.3333333333333333), which compact JSON cannot shrink.
    ladder = [1 / (index + 3) if full_precision else float(index) for index in range(ladder_length)]
    for frame in state["frames"][:accepted]:
        destination = Path(frame["destination"])
        Image.new("RGB", (32, 24), (50, 50, 50)).save(destination, format="JPEG")
        frame.update(status="accepted", receipt=stack_session.frame_receipt(destination),
                     capture_started_at=1790000000.0 + frame["index"], capture_s=6.8, capture_pipeline_s=6.4,
                     transport_receipt=realistic_receipt(frame["index"]),
                     applied={"iso": "100", "aperture": "f/8", "shutter": "1/60",
                              "timelapse_meter": {"requested_ev": 0.1, "actual_ev": 0.1,
                                                  "transport_limits": {"raw_driver_shutters_s": ladder,
                                                                       "usable_shutters_s": ladder},
                                                  "mounted_lens_limits": {"raw_driver_apertures": ladder}}})
    if target_bytes is not None:
        padded = state["frames"][:accepted]
        for frame in padded:
            frame["transport_receipt"]["padding"] = ""
        shortfall = target_bytes - len(json.dumps(state, indent=2))
        assert shortfall >= 0, f"natural size is {-shortfall} bytes over the {target_bytes}-byte target"
        for position, frame in enumerate(padded):  # "x" serializes as one byte, so this is exact
            share = shortfall // len(padded) + (1 if position < shortfall % len(padded) else 0)
            frame["transport_receipt"]["padding"] = "x" * share
    (directory / "manifest.json").write_text(json.dumps(state, indent=2))
    if target_bytes is not None:
        assert (directory / "manifest.json").stat().st_size == target_bytes
    return state["id"]


def test_a_legacy_manifest_with_inline_receipts_still_reads(tmp_path):
    sessions = SessionRunner(tmp_path)
    readable = _legacy_manifest(sessions, 60, 50, target_bytes=500_000)
    assert manifest_size(sessions, readable) <= LEGACY_READ_LIMIT
    state = sessions.read(readable)
    assert state["progress"]["accepted"] == 50
    evidence = sessions.frame_evidence(state, 49)
    assert {key: value for key, value in evidence["transport_receipt"].items() if key != "padding"} == realistic_receipt(49)
    assert evidence["applied"]["timelapse_meter"]["transport_limits"]["usable_shutters_s"]
    assert sessions.artifact(readable, "frame_0049.jpg").is_file()
    # One the old fixed limit refused (it could not be read, completed or recovered) now reads.
    stuck = _legacy_manifest(sessions, 120, 110, target_bytes=1_000_000)
    assert manifest_size(sessions, stuck) > LEGACY_READ_LIMIT
    assert sessions.read(stuck)["progress"]["accepted"] == 110


def test_a_plan_whose_manifest_would_exceed_the_limit_is_refused_at_creation(tmp_path):
    sessions = SessionRunner(tmp_path)
    with pytest.raises(stack_session.ManifestBudgetError, match="manifest"):
        sessions.create("timelapse", 240, metadata={"note": "x" * stack_session.MAX_MANIFEST_BYTES})
    assert list(tmp_path.iterdir()) == []  # nothing half-created


def test_a_run_whose_manifest_could_outgrow_the_limit_is_refused_before_any_shutter(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 240)
    stored = sessions.read(state["id"])
    # Grown outside the planner (an older build's inline evidence, say): still readable, but the
    # remaining frames could not all be recorded within the limit.
    stored["metadata"]["note"] = "x" * (stack_session.MAX_MANIFEST_BYTES - 200 * 1024)
    sessions.save(stored)
    calls = []

    def capture(index, spec, destination):
        calls.append(index)
        raise AssertionError("no shutter may fire")

    result = sessions.run(state["id"], capture)
    assert calls == []
    assert result["status"] == "failed" and "manifest" in result["error"]
    assert all(frame["status"] == "pending" for frame in result["frames"])


def test_save_never_writes_a_manifest_the_reader_would_refuse(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    before = (sessions._directory(state["id"]) / "manifest.json").read_bytes()
    oversized = sessions.read(state["id"])
    oversized["metadata"]["note"] = "x" * stack_session.MAX_MANIFEST_BYTES
    with pytest.raises(stack_session.ManifestBudgetError):
        sessions.save(oversized)
    assert (sessions._directory(state["id"]) / "manifest.json").read_bytes() == before
    assert hashlib.sha256(before).hexdigest()  # unchanged and still readable
    assert sessions.read(state["id"])["status"] == "ready"


def test_a_sidecar_that_cannot_be_written_still_saves_the_acceptance_and_its_evidence(tmp_path, monkeypatch):
    from PIL import Image
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    real_write = stack_session._atomic_write

    def disk_full_for_sidecars(path, data):
        if path.name.endswith(".evidence.json"):
            raise OSError(28, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(stack_session, "_atomic_write", disk_full_for_sidecars)
    oversized = {"blob": "x" * (stack_session.INLINE_FRAME_BUDGET_BYTES + 1)}

    def capture(index, spec, destination):
        spec["transport_receipt"] = {**realistic_receipt(index), **(oversized if index == 2 else {})}
        Image.new("RGB", (32, 24), (60, 60, 60)).save(destination, format="JPEG")
        return destination

    result = sessions.run(state["id"], capture,
                          prepare_frame=lambda index, spec: {"iso": "100", "aperture": "f/8", "shutter": "1/60"})
    assert result["status"] == "completed", result.get("error")
    assert all(frame["status"] == "accepted" and frame["receipt"]["sha256"] for frame in result["frames"])
    first = result["frames"][0]
    assert "No space left" in first["evidence_sidecar_error"] and "evidence_sidecar" not in first
    assert sessions.frame_evidence(result, 0)["transport_receipt"] == realistic_receipt(0)  # kept inline
    third = result["frames"][2]
    assert "too large to keep inline" in third["evidence_sidecar_error"] and third["evidence_not_stored"] is True
    assert third["transport_receipt"]["capture_confirmed"] is True and "blob" not in third["transport_receipt"]


def test_the_projection_reserves_every_frames_rollup_share_even_once_accepted(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.read(sessions.create("timelapse", 240)["id"])
    for frame in state["frames"]:
        frame["status"] = "accepted"  # the final render still adds a receipt per frame
    projected = stack_session.manifest_projection_bytes(state)
    reserved = projected - len(stack_session._manifest_bytes(state))
    assert reserved == stack_session.INLINE_FRAME_BUDGET_BYTES + 240 * stack_session.FRAME_ROLLUP_BUDGET_BYTES


def test_a_240_frame_session_with_unconfirmed_saves_still_warns_within_the_manifest_limit(tmp_path, monkeypatch):
    """#70's durability rollup and /gallery projection read the manifest's compact receipts, which
    keep delivery_durability; the sidecar holds the same record. Worst case: every frame flagged."""
    import engine
    import product_sessions
    frames = stack_session.MAX_SEQUENCE_FRAMES
    install_fake_body(tmp_path, monkeypatch, receipt=unconfirmed_receipt)
    install_fake_encoder(monkeypatch)
    sessions = product_sessions.runner()
    state = sessions.create("timelapse", frames, timelapse_mode="holy_grail",
                            timelapse_options={"max_shutter_s": 1 / 60},
                            metadata={"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL})
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    assert result["publication"]["status"] == "published", result.get("publication_error")

    rollup = result["delivery_durability"]
    assert rollup["frames"] == list(range(frames))
    assert f"{frames} of {frames} frames" in rollup["warning"]
    assert rollup["reasons"] == [unconfirmed_receipt(0)["delivery_durability"]["reason"]]
    assert manifest_size(sessions, state["id"]) <= stack_session.MAX_MANIFEST_BYTES
    for frame in result["frames"]:
        assert within_record_budget(frame), frame["index"]
        assert frame["delivery_durability"] == {"status": "unconfirmed",
                                                "reason_at": "transport_receipt.delivery_durability"}
        full = sessions.frame_evidence(result, frame["index"])["transport_receipt"]
        assert frame["transport_receipt"]["delivery_durability"] == full["delivery_durability"]
        assert full == unconfirmed_receipt(frame["index"])

    # The published record carries the warning, and /gallery's projection for a record written
    # before the rollup existed derives the same rollup from this sidecar-era manifest.
    record = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert rollup["warning"] in record["summary"]
    legacy_row = {"image": f"/shots/session_{state['id']}.jpg", "summary": "Timelapse completed.",
                  "evidence": {"source": "usb_session", "session_id": state["id"]}}
    [projected] = product_sessions.project_delivery_durability([legacy_row])
    assert projected["evidence"]["delivery_durability"] == rollup
    assert projected["summary"].endswith(rollup["warning"])


def _legacy_worst_case(sessions: SessionRunner) -> str:
    """An uninterrupted 240-frame Holy Grail written before this PR: ~9.6 KB of inline evidence per
    frame (the measured real Holy Grail frame), ~2.3 MB, which the old writer never size-checked."""
    session_id = _legacy_manifest(sessions, 240, 240, ladder_length=80, target_bytes=2_350_000)
    return session_id


def test_a_legacy_240_frame_manifest_reads_lists_compacts_on_first_write_and_deletes(tmp_path):
    sessions = SessionRunner(tmp_path)
    session_id = _legacy_worst_case(sessions)
    assert manifest_size(sessions, session_id) > stack_session.MAX_MANIFEST_BYTES
    state = sessions.read(session_id)
    assert state["progress"]["accepted"] == 240 and "evidence_format" not in state
    assert [item["id"] for item in sessions.list()] == [session_id]
    inline = [sessions.frame_evidence(state, index) for index in range(240)]
    sessions.save(state)  # the first write moves every accepted frame's evidence to its sidecar
    assert manifest_size(sessions, session_id) <= stack_session.MAX_MANIFEST_BYTES
    compacted = sessions.read(session_id)
    assert compacted["evidence_format"] == "sidecar"
    for frame in compacted["frames"]:
        assert frame["evidence_sidecar"]["file"] == f"frame_{frame['index']:04d}.evidence.json"
        assert sessions.frame_evidence(compacted, frame["index"]) == inline[frame["index"]]
    assert sessions.delete(session_id)["deleted_count"] >= 2 * 240
    assert not (tmp_path / session_id).exists()


def test_a_legacy_manifest_at_the_ceiling_still_deletes_when_compaction_fails(tmp_path, monkeypatch):
    sessions = SessionRunner(tmp_path)
    # Near the legacy ceiling: even re-serialized compactly, inline evidence would not fit the new limit.
    session_id = _legacy_manifest(sessions, 240, 240, ladder_length=90, full_precision=True,
                                  target_bytes=stack_session.LEGACY_MAX_MANIFEST_BYTES - 1024)
    assert len(stack_session._manifest_bytes(sessions.read(session_id))) > stack_session.MAX_MANIFEST_BYTES
    real_write = stack_session._atomic_write

    def no_sidecars(path, data):
        if path.name.endswith(".evidence.json"):
            raise OSError(28, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(stack_session, "_atomic_write", no_sidecars)
    before = (tmp_path / session_id / "manifest.json").read_bytes()
    with pytest.raises(stack_session.ManifestBudgetError):
        sessions.save(sessions.read(session_id))  # evidence stays inline: still too large to write
    assert (tmp_path / session_id / "manifest.json").read_bytes() == before
    assert sessions.delete(session_id)["deleted_count"] >= 240
    assert not (tmp_path / session_id).exists()


def test_manifests_over_their_format_ceiling_are_refused_with_the_existing_error(tmp_path):
    sessions = SessionRunner(tmp_path)
    legacy = _legacy_manifest(sessions, 3, 1)
    path = tmp_path / legacy / "manifest.json"
    stored = json.loads(path.read_text())
    stored["metadata"]["note"] = "x" * stack_session.LEGACY_MAX_MANIFEST_BYTES
    path.write_text(json.dumps(stored, indent=2))
    assert path.stat().st_size > stack_session.LEGACY_MAX_MANIFEST_BYTES
    with pytest.raises(ValueError, match="exceeds byte budget"):
        sessions.read(legacy)
    # A manifest already in the sidecar format keeps the new limit, even under the legacy ceiling.
    current = sessions.create("hdr", 3)["id"]
    path = tmp_path / current / "manifest.json"
    stored = json.loads(path.read_text())
    assert stored["evidence_format"] == "sidecar"
    stored["metadata"]["note"] = "x" * stack_session.MAX_MANIFEST_BYTES
    path.write_text(json.dumps(stored, separators=(",", ":")))
    assert stack_session.MAX_MANIFEST_BYTES < path.stat().st_size < stack_session.LEGACY_MAX_MANIFEST_BYTES
    with pytest.raises(ValueError, match="exceeds byte budget"):
        sessions.read(current)


def _flaky_sidecars(monkeypatch, failing: dict):
    """Sidecar writes for the frame indices in failing["frames"] raise ENOSPC while failing["on"]."""
    real_write = stack_session._atomic_write

    def write(path, data):
        if failing["on"] and any(path.name == stack_session.evidence_sidecar_name(index)
                                 for index in failing["frames"]):
            raise OSError(28, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(stack_session, "_atomic_write", write)


def test_a_transient_sidecar_failure_is_retried_on_the_next_save(tmp_path, monkeypatch):
    from PIL import Image
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    failing = {"on": False, "frames": {1}}
    _flaky_sidecars(monkeypatch, failing)
    seen = {}

    def capture(index, spec, destination):
        if index == 1:
            failing["on"] = True  # storage fails while frame 1 is accepted
        if index == 2:
            seen["before_recovery"] = sessions.read(state["id"])["frames"][1]
            failing["on"] = False  # storage recovers before frame 2's acceptance is saved
        spec["transport_receipt"] = realistic_receipt(index)
        Image.new("RGB", (32, 24), (60, 60, 60)).save(destination, format="JPEG")
        return destination

    result = sessions.run(state["id"], capture,
                          prepare_frame=lambda index, spec: {"iso": "100", "aperture": "f/8", "shutter": "1/60"})
    assert result["status"] == "completed", result.get("error")
    # While storage failed, frame 1's full evidence stayed inline and the manifest said why.
    assert "No space left" in seen["before_recovery"]["evidence_sidecar_error"]
    assert seen["before_recovery"]["transport_receipt"] == realistic_receipt(1)
    # After recovery the next save moved it to its sidecar and cleared the marker.
    frame = result["frames"][1]
    assert frame["evidence_sidecar"]["file"] == "frame_0001.evidence.json"
    assert "evidence_sidecar_error" not in frame
    assert sessions.frame_evidence(result, 1)["transport_receipt"] == realistic_receipt(1)
    assert "timings" in frame["transport_receipt"] and "capture_deadline" not in frame["transport_receipt"]


def test_a_persistent_sidecar_failure_keeps_every_frames_evidence_inline(tmp_path, monkeypatch):
    from PIL import Image
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    _flaky_sidecars(monkeypatch, {"on": True, "frames": {0, 1, 2}})

    def capture(index, spec, destination):
        spec["transport_receipt"] = realistic_receipt(index)
        Image.new("RGB", (32, 24), (60, 60, 60)).save(destination, format="JPEG")
        return destination

    result = sessions.run(state["id"], capture,
                          prepare_frame=lambda index, spec: {"iso": "100", "aperture": "f/8", "shutter": "1/60"})
    assert result["status"] == "completed", result.get("error")
    for frame in result["frames"]:
        assert "evidence_sidecar" not in frame and "No space left" in frame["evidence_sidecar_error"]
        assert sessions.frame_evidence(result, frame["index"])["transport_receipt"] == realistic_receipt(frame["index"])
    # Saves keep retrying and keep the evidence; one that would outgrow the limit is still refused.
    stored = sessions.read(state["id"])
    stored["metadata"]["note"] = "x" * stack_session.MAX_MANIFEST_BYTES
    with pytest.raises(stack_session.ManifestBudgetError):
        sessions.save(stored)
    assert sessions.frame_evidence(sessions.read(state["id"]), 2)["transport_receipt"] == realistic_receipt(2)


def test_failed_sidecar_writes_leave_no_temporary_files(tmp_path, monkeypatch):
    import os
    from PIL import Image
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    real_replace = os.replace
    failures = []

    def replace(source, destination):
        if str(destination).endswith(".evidence.json"):  # the temporary is fully written; the rename fails
            failures.append(destination)
            raise OSError(5, "Input/output error")
        return real_replace(source, destination)

    monkeypatch.setattr(stack_session.os, "replace", replace)

    def capture(index, spec, destination):
        spec["transport_receipt"] = realistic_receipt(index)
        Image.new("RGB", (32, 24), (60, 60, 60)).save(destination, format="JPEG")
        return destination

    result = sessions.run(state["id"], capture,
                          prepare_frame=lambda index, spec: {"iso": "100", "aperture": "f/8", "shutter": "1/60"})
    assert result["status"] == "completed", result.get("error")
    for _ in range(5):
        sessions.save(sessions.read(state["id"]))  # every save retries the three inline frames
    assert len(failures) >= 3 * 5
    directory = sessions._directory(state["id"])
    assert sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp")) == []
    assert sessions.delete(state["id"])["deleted_count"] > 0


def test_the_session_detail_returns_full_frames_and_the_list_stays_compact(tmp_path, monkeypatch):
    import app
    import product_sessions
    from fastapi.testclient import TestClient
    install_fake_body(tmp_path, monkeypatch)
    install_fake_encoder(monkeypatch)
    monkeypatch.setattr(app, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(app, "API_TOKEN", "fixture-credential-1234567890")
    headers = {"X-Camera-Autopilot-Token": app.API_TOKEN}
    metadata = {"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL}
    sessions = product_sessions.runner()

    # The frame shape before sidecars: the same session with its evidence left inline.
    with monkeypatch.context() as inline:
        inline.setattr(stack_session, "_store_frame_evidence", lambda *args, **kwargs: None)
        before = product_sessions.run(sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                                                      metadata=metadata)["id"])
    assert before["status"] == "completed" and "evidence_sidecar" not in before["frames"][0]
    current = product_sessions.run(sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                                                   metadata=metadata)["id"])
    assert all(frame["evidence_sidecar"] for frame in current["frames"])

    client = TestClient(app.app)
    detail = client.get(f"/sessions/{current['id']}", headers=headers).json()
    for full, old in zip(detail["frames"], before["frames"]):
        assert set(full) - {"evidence_sidecar"} == set(old)
        assert full["applied"] == old["applied"]
        # The fake body numbers its captures across both sessions: the first ran shots 0-2.
        assert old["transport_receipt"] == realistic_receipt(old["index"])
        assert full["transport_receipt"] == realistic_receipt(3 + full["index"])
    assert detail["frames"][2]["applied"]["timelapse_meter"]["transport_limits"]["usable_shutter_labels"]
    assert detail["frames"][2]["applied"]["exposure_transition"]["requested"]
    # GET /sessions is documented compact: summaries only, full evidence stays in the sidecars.
    listed = {item["id"]: item for item in client.get("/sessions", headers=headers).json()}
    compact = listed[current["id"]]["frames"][2]
    assert "transport_limits" not in compact["applied"]["timelapse_meter"]
    assert "capture_deadline" not in compact["transport_receipt"]
    assert compact["evidence_sidecar"]["file"] == "frame_0002.evidence.json"
