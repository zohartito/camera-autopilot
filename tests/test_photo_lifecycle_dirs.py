"""Directory-aware retention/deletion: isolated tmp_path galleries only.

Covers the enrolled-directory ownership model and empty-shell pruning added to
PhotoLifecycle. Never operates on the real gallery or proof directories.
"""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from photo_lifecycle import PhotoLifecycle  # noqa: E402


@pytest.fixture
def gallery(tmp_path):
    return PhotoLifecycle(tmp_path / "shots")


def photo(service, name="shot_fixture.jpg"):
    Image.new("RGB", (64, 64), "red").save(service.root / name)
    (service.root / f"{Path(name).stem}.json").write_text(json.dumps({
        "image": name, "summary": "Fixture", "why": "No camera", "cards": []}))
    return name


def test_owned_directory_tree_and_shell_removed(gallery):
    name = photo(gallery)
    job = gallery.root / "development" / "job1"
    (job / "nested").mkdir(parents=True)
    (job / "output.dng").write_bytes(b"dng")
    (job / "nested" / "layer.jpg").write_bytes(b"layer")
    gallery.enroll(name, owned_dirs=["development/job1"])
    result = gallery.delete(name)
    assert result["deleted_count"] >= 4  # image, sidecar, dng, nested layer, record
    assert not (job / "output.dng").exists()
    assert not (job / "nested").exists()
    assert not job.exists()
    # 'development' became empty as a result and is pruned too; root survives.
    assert not (gallery.root / "development").exists()
    assert gallery.root.exists()


def test_file_added_after_directory_enrollment_is_removed(gallery):
    name = photo(gallery)
    job = gallery.root / "development" / "job1"
    job.mkdir(parents=True)
    (job / "early.jpg").write_bytes(b"early")
    gallery.enroll(name, owned_dirs=["development/job1"])
    (job / "late.jpg").write_bytes(b"late")  # appears only after enrollment
    plan = gallery.delete(name, dry_run=True)
    assert "development/job1/late.jpg" in plan["files"]
    assert (job / "late.jpg").exists()  # dry run touched nothing
    gallery.delete(name)
    assert not (job / "late.jpg").exists()
    assert not job.exists()


def test_delete_all_cleans_orphan_record_and_owned_paths(gallery):
    name = photo(gallery, "gone.jpg")
    (gallery.root / "stack").mkdir()
    (gallery.root / "stack" / "frame.jpg").write_bytes(b"frame")
    gallery.enroll(name, owned_paths=["stack/frame.jpg"])
    gallery.thumbnail(name)
    record = gallery.root / ".lifecycle" / f"{name}.json"
    thumb = gallery.root / ".thumbnails" / f"{name}.jpg"
    assert record.exists() and thumb.exists()
    # Image disappears without going through delete(): a stale record is orphaned.
    (gallery.root / name).unlink()
    gallery.delete_all(confirm="delete-all-local-photos")
    assert not record.exists()
    assert not (gallery.root / "stack" / "frame.jpg").exists()
    assert not (gallery.root / "stack").exists()
    assert not thumb.exists()
    # Reserved caches themselves survive.
    assert (gallery.root / ".thumbnails").exists()
    assert (gallery.root / ".lifecycle").exists()


def test_symlink_inside_owned_dir_unlinked_target_untouched(gallery, tmp_path):
    name = photo(gallery)
    job = gallery.root / "development" / "job1"
    job.mkdir(parents=True)
    (job / "real.jpg").write_bytes(b"real")
    target = tmp_path / "secret.jpg"
    target.write_bytes(b"secret")
    (job / "link.jpg").symlink_to(target)
    gallery.enroll(name, owned_dirs=["development/job1"])
    gallery.delete(name)
    assert not job.exists()
    assert target.exists() and target.read_bytes() == b"secret"


def test_reserved_dirs_and_root_survive_delete(gallery):
    name = photo(gallery)
    gallery.enroll(name)
    gallery.thumbnail(name)
    gallery.delete(name)
    assert gallery.root.exists()
    assert (gallery.root / ".thumbnails").exists()
    assert (gallery.root / ".lifecycle").exists()
    assert not (gallery.root / name).exists()


def test_owned_file_subdir_pruned_when_empty(gallery):
    name = photo(gallery)
    (gallery.root / "stack").mkdir()
    (gallery.root / "stack" / "frame.jpg").write_bytes(b"frame")
    gallery.enroll(name, owned_paths=["stack/frame.jpg"])
    gallery.delete(name)
    assert not (gallery.root / "stack").exists()


def test_shared_parent_dir_preserved_when_nonempty(gallery):
    name = photo(gallery)
    (gallery.root / "stack").mkdir()
    (gallery.root / "stack" / "frame.jpg").write_bytes(b"frame")
    (gallery.root / "stack" / "keep.txt").write_bytes(b"keep")
    gallery.enroll(name, owned_paths=["stack/frame.jpg"])
    gallery.delete(name)
    assert (gallery.root / "stack").exists()
    assert (gallery.root / "stack" / "keep.txt").exists()


def test_enroll_rejects_symlinked_or_reserved_owned_dir(gallery, tmp_path):
    name = photo(gallery)
    outside = tmp_path / "outside"
    outside.mkdir()
    (gallery.root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        gallery.enroll(name, owned_dirs=["linkdir"])
    with pytest.raises(ValueError):
        gallery.enroll(name, owned_dirs=[".lifecycle"])
    assert (outside).exists()


def test_owned_dir_depth_budget_blocks_before_deletion(gallery):
    name = photo(gallery)
    deep = gallery.root / "d1" / "d2" / "d3" / "d4" / "d5"
    deep.mkdir(parents=True)
    (deep / "x.jpg").write_bytes(b"x")
    gallery.enroll(name, owned_dirs=["d1"])
    with pytest.raises(ValueError):
        gallery.delete(name, dry_run=True)
    assert (deep / "x.jpg").exists()  # nothing removed when the plan is refused


def test_retention_sweep_seconds_env(monkeypatch):
    import photo_lifecycle
    monkeypatch.setenv("CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS", "5")
    assert photo_lifecycle._retention_sweep_seconds() == 10  # clamped up
    monkeypatch.setenv("CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS", "99999")
    assert photo_lifecycle._retention_sweep_seconds() == 3600  # clamped down
    monkeypatch.setenv("CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS", "not-a-number")
    assert photo_lifecycle._retention_sweep_seconds() == 60  # default on garbage
    monkeypatch.setenv("CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS", "120")
    assert photo_lifecycle._retention_sweep_seconds() == 120
