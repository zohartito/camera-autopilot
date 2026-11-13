"""Mock-engine captures never land in the real gallery, and demo records are distinguishable.

Regression for the 2026-09-15 live session: the iOS simulator smoke (CAMERA_ENGINE=mock) wrote
"Demo preview" shots into backend/shots/, the directory the real server's Review page lists.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))

import engine  # noqa: E402

REAL_GALLERY = BACKEND / "shots"


def _shots_dir_for(env_overrides: dict) -> Path:
    env = {k: v for k, v in os.environ.items() if k not in ("CAMERA_ENGINE", "CAMERA_AUTOPILOT_SHOTS_DIR")}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'backend'); import engine; print(engine.SHOTS_DIR)"],
        cwd=REPO, env=env, check=True, capture_output=True, text=True).stdout.strip()
    return Path(out)


def test_mock_engine_default_gallery_is_not_the_real_one() -> None:
    for mode in ({}, {"CAMERA_ENGINE": "mock"}):
        resolved = _shots_dir_for(mode)
        assert resolved != REAL_GALLERY, f"{mode or 'unset'} engine resolved to the real gallery"
        assert REAL_GALLERY not in resolved.parents
        assert resolved == REPO / "artifacts" / "demo-shots"


def test_hardware_engines_keep_the_real_gallery() -> None:
    for mode in ("real", "wifi"):
        assert _shots_dir_for({"CAMERA_ENGINE": mode}) == REAL_GALLERY


def test_env_override_wins_for_every_engine(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    for mode in ({}, {"CAMERA_ENGINE": "real"}, {"CAMERA_ENGINE": "mock"}):
        assert _shots_dir_for({**mode, "CAMERA_AUTOPILOT_SHOTS_DIR": str(scratch)}) == scratch


def test_mock_shoot_writes_only_into_the_configured_scratch_dir(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    env = dict(os.environ, CAMERA_ENGINE="mock", CAMERA_AUTOPILOT_SHOTS_DIR=str(scratch),
               OLLAMA_HOST="http://127.0.0.1:1", VISION_HOST="http://127.0.0.1:1")
    code = """
import json, sys
sys.path.insert(0, 'backend')
import engine
shot = engine.MockEngine().shoot('warm dreamy portrait')
sidecar = engine.save_shot_record(shot, 'warm dreamy portrait')
records = engine.gallery_records()
print(json.dumps({'image': str(engine.SHOTS_DIR / shot.image), 'sidecar': str(sidecar),
                  'evidence': shot.evidence, 'record': json.loads(sidecar.read_text()),
                  'gallery': records}))
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, check=True,
                         capture_output=True, text=True).stdout
    result = json.loads(out.strip().splitlines()[-1])
    for written in (Path(result["image"]), Path(result["sidecar"])):
        assert written.is_file()
        assert scratch in written.parents
        assert REAL_GALLERY not in written.parents
    assert result["evidence"]["engine"] == "mock"
    assert result["record"]["engine"] == "mock"
    assert [row["engine"] for row in result["gallery"]] == ["mock"]


def test_gallery_flags_legacy_demo_records_and_leaves_real_ones_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    legacy_demo = {"image": "shot_1.jpg", "summary": "Demo preview — kept it dreamy and warm.",
                   "settings": {"white_balance": "Demo"}, "evidence": {"source": "mock", "physical_capture": False},
                   "ts": 1.0}
    real = {"image": "shot_2.jpg", "summary": "Kept the highlights.", "settings": {"iso": "200"},
            "evidence": {"source": "after_final", "settings_readback": {"after_final": "verified"}}, "ts": 2.0}
    explicit = {"image": "shot_3.jpg", "summary": "x", "engine": "mock", "evidence": {}, "ts": 3.0}
    for name, record in (("shot_1.json", legacy_demo), ("shot_2.json", real), ("shot_3.json", explicit)):
        (tmp_path / name).write_text(json.dumps(record))
    by_image = {row["image"]: row["engine"] for row in engine.gallery_records()}
    assert by_image == {"/shots/shot_1.jpg": "mock", "/shots/shot_2.jpg": None, "/shots/shot_3.jpg": "mock"}


def test_review_page_marks_mock_records() -> None:
    html = (BACKEND / "web" / "review.html").read_text()
    assert "shot.engine === 'mock'" in html
    assert "Demo — mock engine, not a photograph" in html


def test_simulator_smoke_scopes_its_mock_backend_gallery() -> None:
    script = (REPO / "mobile" / "scripts" / "run_ios_simulator_smoke.sh").read_text()
    assert 'CAMERA_AUTOPILOT_SHOTS_DIR="$demo_shots_dir"' in script
    assert 'demo_shots_dir="${IOS_SMOKE_SHOTS_DIR:-$repo_root/artifacts/ios-simulator/demo-shots}"' in script
    launch = script[script.index("CAMERA_ENGINE=mock"):]
    assert "CAMERA_AUTOPILOT_SHOTS_DIR" in launch.split("uvicorn")[0]
    # The repo-root artifacts tree is gitignored, so simulator demo imagery never shows as untracked.
    assert "/artifacts/" in (REPO / ".gitignore").read_text().splitlines()


def test_agents_documents_the_gallery_override() -> None:
    assert "CAMERA_AUTOPILOT_SHOTS_DIR" in (REPO / "AGENTS.md").read_text()
