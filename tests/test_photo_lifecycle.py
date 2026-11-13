"""Isolated fixtures only: never operate on real gallery or proof directories."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from photo_lifecycle import PhotoLifecycle, manage_shot
from security_bounds import RequestBudget, bounded_body_length


@pytest.fixture
def gallery(tmp_path):
    root = tmp_path / "shots"
    service = PhotoLifecycle(root)
    return service


def photo(service, name="shot_fixture.jpg"):
    Image.new("RGB", (1200, 800), "red").save(service.root / name)
    (service.root / f"{Path(name).stem}.json").write_text(json.dumps({
        "image": name, "summary": "Fixture", "why": "No camera", "cards": [{"axis": "motion"}]}))
    return name


def test_cards_and_thumbnail(gallery):
    name = photo(gallery)
    assert gallery.cards(name)["cards"] == [{"axis": "motion"}]
    thumbnail = gallery.thumbnail(name)
    with Image.open(thumbnail) as result:
        assert result.size == (640, 427)
        assert not result.getexif()


def test_delete_exact_family_and_owned_files_only(gallery):
    name = photo(gallery)
    other = photo(gallery, "shot_fixture_other.jpg")
    (gallery.root / "shot_fixture.ARW").write_bytes(b"raw")
    (gallery.root / "stack").mkdir()
    (gallery.root / "stack" / "frame.jpg").write_bytes(b"frame")
    gallery.thumbnail(name)
    gallery.enroll(name, ["stack/frame.jpg"])
    plan = gallery.delete(name, dry_run=True)
    assert "shot_fixture.ARW" in plan["files"]
    assert (gallery.root / name).exists()
    result = gallery.delete(name)
    assert result["deleted_count"] == 6
    assert (gallery.root / other).exists()
    assert not (gallery.root / "stack/frame.jpg").exists()


def test_retention_never_enrolls_old_photos(gallery):
    old = photo(gallery, "old.jpg")
    new = photo(gallery, "new.jpg")
    assert gallery.retention()["days"] is None
    gallery.enroll(new, now=100)
    gallery.set_retention(7)
    plan = gallery.enforce_retention(now=100 + 8 * 86400, dry_run=True)
    assert [item["image"] for item in plan["photos"]] == [new]
    assert (gallery.root / new).exists()
    gallery.enforce_retention(now=100 + 8 * 86400)
    assert (gallery.root / old).exists()
    assert not (gallery.root / new).exists()


def test_manage_does_not_reset_age(gallery):
    name = photo(gallery)
    gallery.enroll(name, now=123)
    assert manage_shot(gallery.root, name)["managed"]["enrolled_at"] == 123


@pytest.mark.parametrize("name", ["../outside.jpg", "/tmp/outside.jpg", "sub/photo.jpg", "x\\y.jpg", ".secret.jpg", "photo.json"])
def test_traversal_rejected(gallery, name):
    with pytest.raises(ValueError):
        gallery.delete(name)


def test_symlinks_cannot_read_or_delete_target(gallery, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")
    (gallery.root / "linked.jpg").symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        gallery.thumbnail("linked.jpg")
    with pytest.raises(ValueError):
        gallery.delete("linked.jpg")
    assert outside.read_bytes() == b"private"


def test_owned_parent_symlink_rejected(gallery, tmp_path):
    name = photo(gallery)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "frame.jpg").write_bytes(b"private")
    (gallery.root / "stack").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        gallery.enroll(name, ["stack/frame.jpg"])
    assert (outside / "frame.jpg").exists()


def test_metadata_symlink_rejected(gallery, tmp_path):
    name = photo(gallery)
    gallery.enroll(name)
    marker = gallery.root / ".lifecycle" / f"{name}.json"
    marker.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    marker.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        gallery.delete(name)
    assert (gallery.root / name).exists()


def test_delete_all_preflight_and_confirmation(gallery, tmp_path):
    name = photo(gallery)
    with pytest.raises(ValueError):
        gallery.delete_all(confirm="yes")
    assert (gallery.root / name).exists()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")
    (gallery.root / "zlinked.jpg").symlink_to(outside)
    with pytest.raises(ValueError):
        gallery.delete_all(confirm="delete-all-local-photos")
    assert (gallery.root / name).exists()
    (gallery.root / "zlinked.jpg").unlink()
    assert gallery.delete_all(confirm="delete-all-local-photos")["deleted_count"] == 2


def test_corrupt_retention_manifest_is_logged_not_deleted(gallery):
    name = photo(gallery)
    gallery.enroll(name)
    gallery.set_retention(1)
    (gallery.root / ".lifecycle" / f"{name}.json").write_text('{"image":"../private.jpg"}')
    result = gallery.enforce_retention(now=99999999999)
    assert len(result["errors"]) == 1
    assert (gallery.root / name).exists()


@pytest.mark.parametrize("days", [0, -1, 3651, True, "7", 1.5])
def test_retention_validation(gallery, days):
    with pytest.raises(ValueError):
        gallery.set_retention(days)


def test_request_budget_has_bounded_storage_and_recovers():
    budget = RequestBudget(limit=2, window_seconds=60)
    assert budget.check(now=100) == 0
    assert budget.check(now=101) == 0
    assert budget.check(now=102) == 58
    assert len(budget._events) == 2
    assert budget.check(now=160) == 0
    assert bounded_body_length("65536") == 65536
    for value in [None, "-1", "bad", "65537"]:
        with pytest.raises(ValueError):
            bounded_body_length(value)


def test_deployment_preflight_never_reports_token(tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'tools/deployment_preflight.py'
    spec = importlib.util.spec_from_file_location('deployment_preflight', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = 'test-only-credential-not-real-123456'
    result = module.report(tmp_path, {'CAMERA_AUTOPILOT_API_TOKEN': secret})
    assert result['checks']['configured_token_strong']
    assert secret not in json.dumps(result)
    assert not result['public_release_ready']


def test_thumbnail_rejects_pixel_budget_before_decode(gallery, monkeypatch):
    import image_safety
    name = photo(gallery)
    monkeypatch.setattr(image_safety, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(ValueError, match='pixel budget'):
        gallery.thumbnail(name)
    assert not (gallery.root / '.thumbnails' / f'{name}.jpg').exists()


def test_delete_waits_for_inflight_thumbnail_and_leaves_no_orphan(gallery, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    name = photo(gallery)
    source_read = threading.Event()
    finish_thumbnail = threading.Event()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    original_read = gallery._read

    def paused_read(relative, limit):
        result = original_read(relative, limit)
        if relative == name:
            source_read.set()
            assert finish_thumbnail.wait(3), 'test thumbnail barrier timed out'
        return result

    def delete_while_thumbnail_waits():
        delete_started.set()
        result = gallery.delete(name)
        delete_finished.set()
        return result

    monkeypatch.setattr(gallery, '_read', paused_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        thumbnail_job = executor.submit(gallery.thumbnail, name)
        assert source_read.wait(3)
        delete_job = executor.submit(delete_while_thumbnail_waits)
        try:
            assert delete_started.wait(3)
            # The delete request begins between source read and preview write,
            # but cannot return until cache generation releases the lifecycle lock.
            assert not delete_finished.wait(.1)
        finally:
            finish_thumbnail.set()
        thumbnail_path = thumbnail_job.result(timeout=3)
        result = delete_job.result(timeout=3)
    assert result['deleted_count'] == 3
    assert delete_finished.is_set()
    assert not thumbnail_path.exists()
    assert not (gallery.root / name).exists()
    with pytest.raises(FileNotFoundError):
        gallery.thumbnail(name)


def test_enrollment_validation_and_marker_are_serialized_with_delete(gallery, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    name = photo(gallery)
    validated = threading.Event()
    publish = threading.Event()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    regular = gallery._regular
    def paused_validation(relative):
        result = regular(relative)
        if relative == name and not validated.is_set():
            validated.set()
            assert publish.wait(3)
        return result
    def deletion():
        delete_started.set()
        result = gallery.delete(name)
        delete_finished.set()
        return result
    monkeypatch.setattr(gallery, '_regular', paused_validation)
    with ThreadPoolExecutor(max_workers=2) as executor:
        enrollment = executor.submit(gallery.enroll, name)
        assert validated.wait(3)
        removal = executor.submit(deletion)
        try:
            assert delete_started.wait(3)
            assert not delete_finished.wait(.1)
        finally:
            publish.set()
        enrollment.result(timeout=3)
        deleted = removal.result(timeout=3)
    assert f'.lifecycle/{name}.json' in deleted['files']
    assert not (gallery.root / '.lifecycle' / f'{name}.json').exists()
    assert not (gallery.root / name).exists()
