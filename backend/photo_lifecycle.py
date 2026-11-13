"""Bounded local-gallery lifecycle; no camera operations or historical auto-enrollment.

Only files beneath an explicit trusted gallery root can be managed. All path walks
use directory descriptors and O_NOFOLLOW, including generated metadata/cache files.
Explicit deletion may remove existing gallery photos; automatic retention only
removes captures registered by manage_shot after successful creation.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path, PurePosixPath

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
RELATED_SUFFIXES = IMAGE_SUFFIXES | {".arw", ".dng", ".raw", ".json", ".xmp"}
MAX_JSON_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_FILES = 10000
MAX_OWNED_FILES = 2048
MAX_OWNED_DIR_DEPTH = 4
RESERVED_DIRS = {".thumbnails", ".lifecycle"}
# Camera downloads the gallery never lists or serves (sonyremote quarantine): staging-<id>/ while a
# capture downloads, unproven_<time>_<id>/ (bytes + receipt.json) when its body could not be proven,
# or a receipt-less staging-<id>/ that kept bytes when even the receipt could not be written.
# Delete-all removes all of it; retention expires each entry per item, never aborting the sweep.
QUARANTINE_DIR = "quarantine"
# Receipts of uncertain capture outcomes (backend/uncertain_receipts.py): diagnostics, never listed
# or served. Delete-all removes them.
UNCERTAIN_DIR = "uncertain"
DELETE_ALL_CONFIRMATION = "delete-all-local-photos"
_LOCK = threading.RLock()


def _retention_sweep_seconds() -> int:
    """Retention sweep interval in seconds; env override clamped to a safe band."""
    try:
        value = int(os.getenv("CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS", "60"))
    except (TypeError, ValueError):
        return 60
    return max(10, min(3600, value))


RETENTION_SWEEP_SECONDS = _retention_sweep_seconds()


def _parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("invalid gallery path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(relative).is_absolute():
        raise ValueError("gallery path must be relative without traversal")
    return tuple(parts)


def _image_name(image: str) -> str:
    parts = _parts(image)
    if len(parts) != 1 or Path(image).suffix.lower() not in IMAGE_SUFFIXES or image.startswith("."):
        raise ValueError("expected a gallery image filename")
    return image


class PhotoLifecycle:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("gallery root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False):
        parts = _parts(relative)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def _read(self, relative: str, limit: int) -> bytes:
        with self._parent(relative) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ValueError("gallery file exceeds byte budget or is not regular")
                data = handle.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("gallery file exceeds byte budget")
                return data

    def _json(self, relative: str, default: dict | None = None) -> dict:
        try:
            data = json.loads(self._read(relative, MAX_JSON_BYTES))
        except FileNotFoundError:
            return {} if default is None else default
        if not isinstance(data, dict):
            raise ValueError("gallery metadata must be an object")
        return data

    def _write(self, relative: str, data: bytes):
        with self._parent(relative, create=True) as (parent, name):
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Replacing a symlink would be safe, but reject it explicitly.
                try:
                    if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        raise ValueError("gallery metadata must not be a symlink")
                except FileNotFoundError:
                    pass
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _write_json(self, relative: str, value: dict):
        self._write(relative, json.dumps(value, allow_nan=False).encode())

    def _regular(self, relative: str) -> bool:
        try:
            with self._parent(relative) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("gallery paths must be regular files, not symlinks")
                return True
        except FileNotFoundError:
            return False

    def _names(self, relative: str = "") -> list[str]:
        # Opening root followed by a single trusted child avoids recursive traversal.
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if relative:
                _parts(relative)
                child = os.open(relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            names = []
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > MAX_FILES:
                        raise ValueError("gallery file count exceeds operation budget")
            return sorted(names)
        except FileNotFoundError:
            return []
        finally:
            os.close(descriptor)

    def _open_dir_nofollow(self, relative: str) -> int | None:
        # Descend to a directory without ever following a symlink component.
        # Returns an owned descriptor, or None when the path is missing, is not a
        # directory, or crosses a symlink (O_NOFOLLOW raises ELOOP in that case).
        parts = _parts(relative)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            return descriptor
        except OSError:
            os.close(descriptor)
            return None

    def _is_owned_dir(self, relative: str) -> bool:
        descriptor = self._open_dir_nofollow(relative)
        if descriptor is None:
            return False
        os.close(descriptor)
        return True

    def _walk_owned_dir(self, owned_dir: str, depth: int = 1) -> tuple[list[str], list[str]]:
        # Enumerate everything beneath an enrolled directory. Files (including
        # symlinks, which are recorded so the link itself is unlinked, never its
        # target) and subdirectories are returned as relative paths. Subdirectory
        # descent is bounded and never follows a symlink.
        files: list[str] = []
        dirs: list[str] = []
        descriptor = self._open_dir_nofollow(owned_dir)
        if descriptor is None:
            return files, dirs
        try:
            for name in sorted(os.listdir(descriptor)):
                child = f"{owned_dir}/{name}"
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_OWNED_DIR_DEPTH:
                        raise ValueError("owned directory exceeds depth budget")
                    sub_files, sub_dirs = self._walk_owned_dir(child, depth + 1)
                    files.extend(sub_files)
                    dirs.extend(sub_dirs)
                else:
                    files.append(child)
                if len(files) + len(dirs) > MAX_OWNED_FILES:
                    raise ValueError("owned directory exceeds file budget")
        finally:
            os.close(descriptor)
        dirs.append(owned_dir)  # append self after children: deepest-first ordering
        return files, dirs

    def _ordered_removable_dirs(self, files, walk_dirs) -> list[str]:
        # Directories to prune, deepest first: enrolled directory trees plus any
        # ancestor directory of a deleted file that may now be empty. Reserved
        # areas and the gallery root are never candidates.
        candidates = set(walk_dirs)
        for path in files:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                candidates.add("/".join(parts[:depth]))
        removable = [directory for directory in candidates
                     if directory and directory.split("/")[0] not in RESERVED_DIRS]
        removable.sort(key=lambda path: (-len(path.split("/")), path))
        return removable

    def cards(self, image: str) -> dict:
        image = _image_name(image)
        if not self._regular(image):
            raise FileNotFoundError(image)
        record = self._json(f"{Path(image).stem}.json")
        cards = record.get("cards", [])
        if not isinstance(cards, list):
            cards = []
        return {"image": image, "cards": cards[:32], "summary": record.get("summary", ""),
                "why": record.get("why", ""), "settings": record.get("settings", {}),
                "source": record.get("source", "legacy_record"),
                "applied": record.get("applied", {}),
                "evidence": "saved_record" if record else "no_saved_explanation"}

    def thumbnail(self, image: str) -> Path:
        from io import BytesIO
        from PIL import ImageOps
        from image_safety import open_rgb_from_bytes

        image = _image_name(image)
        destination = f".thumbnails/{image}.jpg"
        # Keep deletion serialized with the entire cache generation. Otherwise a
        # request that read just before deletion could recreate an orphan preview.
        with _LOCK:
            data = self._read(image, MAX_IMAGE_BYTES)
            with open_rgb_from_bytes(data) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("gallery image exceeds pixel budget")
                source.thumbnail((640, 640))
                thumb = ImageOps.exif_transpose(source).convert("RGB")
                output = BytesIO()
                thumb.save(output, format="JPEG", quality=82)
            self._write(destination, output.getvalue())
        return self.root / destination

    def retention(self) -> dict:
        policy = self._json(".lifecycle/retention.json", {"days": None})
        days = policy.get("days")
        if days is not None and (type(days) is not int or not 1 <= days <= 3650):
            raise ValueError("invalid saved retention policy")
        return {"days": days, "automatic": days is not None,
                "scope": "newly_managed_gallery_photos_only",
                "preserves_existing_unenrolled_photos": True,
                "excluded": ["camera_card", "external_capture_sources", "private_evidence",
                             "backups", "feedback_and_diagnostics"]}

    def set_retention(self, days: int | None) -> dict:
        if days is not None and (type(days) is not int or not 1 <= days <= 3650):
            raise ValueError("retention days must be null or an integer from 1 to 3650")
        with _LOCK:
            self._write_json(".lifecycle/retention.json", {"days": days})
        return self.retention()

    def enroll(self, image: str, owned_paths=(), owned_dirs=(), *,
               now: float | None = None) -> dict:
        image = _image_name(image)
        timestamp = time.time() if now is None else now
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("invalid enrollment timestamp")
        paths = list(owned_paths)
        directories = list(owned_dirs)
        if len(paths) > MAX_OWNED_FILES or len(directories) > MAX_OWNED_FILES:
            raise ValueError("owned file count exceeds budget")
        with _LOCK:
            # Validation and publication are one transaction with deletion.
            for path in [image, *paths]:
                _parts(path)
                if path.startswith(".lifecycle/") or not self._regular(path):
                    raise ValueError("owned file must be an existing gallery file")
            for directory in directories:
                parts = _parts(directory)
                if parts[0] in RESERVED_DIRS or not self._is_owned_dir(directory):
                    raise ValueError("owned directory must be an existing gallery directory")
            previous = self._json(f".lifecycle/{image}.json")
            record = {"image": image, "enrolled_at": previous.get("enrolled_at", timestamp),
                      "owned_paths": sorted(set(previous.get("owned_paths", []) + paths))}
            merged_dirs = sorted(set(previous.get("owned_dirs", []) + directories))
            if merged_dirs:
                record["owned_dirs"] = merged_dirs
            if len(record["owned_paths"]) > MAX_OWNED_FILES or len(merged_dirs) > MAX_OWNED_FILES:
                raise ValueError("owned file count exceeds budget")
            self._write_json(f".lifecycle/{image}.json", record)
        return record

    def _deletion_plan(self, image: str) -> tuple[list[str], list[str]]:
        image = _image_name(image)
        record = self._json(f".lifecycle/{image}.json")
        owned = record.get("owned_paths", [])
        owned_dirs = record.get("owned_dirs", [])
        if not isinstance(owned, list) or len(owned) > MAX_OWNED_FILES:
            raise ValueError("invalid owned file manifest")
        if not isinstance(owned_dirs, list) or len(owned_dirs) > MAX_OWNED_FILES:
            raise ValueError("invalid owned directory manifest")
        stem = Path(image).stem
        # Case insensitive RAW/JPEG extensions while keeping an exact stem boundary.
        candidates = [name for name in self._names()
                      if Path(name).stem == stem and Path(name).suffix.lower() in RELATED_SUFFIXES]
        candidates += [f"{stem}.json.tmp", f".thumbnails/{image}.jpg", *owned,
                       f".lifecycle/{image}.json"]
        files = []
        for path in sorted(set(candidates)):
            _parts(path)
            if path.startswith(".lifecycle/") and path != f".lifecycle/{image}.json":
                raise ValueError("ownership cannot reference another lifecycle record")
            if self._regular(path):
                files.append(path)
        # Enrolled directories: everything added under them since enrollment is
        # removed, and the now-empty directory shells are pruned afterwards.
        walk_dirs = []
        for owned_dir in sorted(set(owned_dirs)):
            parts = _parts(owned_dir)
            if parts[0] in RESERVED_DIRS:
                raise ValueError("owned directory cannot reference reserved gallery area")
            walked_files, walked_dirs = self._walk_owned_dir(owned_dir)
            files.extend(walked_files)
            walk_dirs.extend(walked_dirs)
        files = sorted(set(files))
        return files, self._ordered_removable_dirs(files, walk_dirs)

    def _deletion_paths(self, image: str) -> list[str]:
        # Backward-compatible flat file listing (still used as a session preflight).
        return self._deletion_plan(image)[0]

    @contextmanager
    def _guard_sessions(self, paths):
        from stack_session import SessionRunner
        ids = sorted({parts[1] for path in paths if len(parts := _parts(path)) > 2
                      and parts[0] == 'sessions'})
        with ExitStack() as locks:
            for session_id in ids:
                runner = SessionRunner(self.root / 'sessions')
                try:
                    locks.enter_context(runner.operation(session_id))
                except RuntimeError as exc:
                    raise ValueError('Cannot delete a photo while its session is active') from exc
                job = self._json(f'sessions/{session_id}/job.json')
                if job.get('status') in {'queued', 'running'}:
                    raise ValueError('Cannot delete a photo while its session job is active')
            yield

    def _delete_files(self, image, files, dirs, dry_run):
        if not files and not dirs:
            raise FileNotFoundError(image)
        removed_dirs = 0
        if not dry_run:
            for path in files:
                with self._parent(path) as (parent, name):
                    # unlink never follows even a concurrently replaced final symlink.
                    os.unlink(name, dir_fd=parent)
            for path in dirs:
                try:
                    with self._parent(path) as (parent, name):
                        # rmdir only removes an empty real directory; a symlink or a
                        # still-populated shared directory is left untouched.
                        os.rmdir(name, dir_fd=parent)
                    removed_dirs += 1
                except OSError:
                    pass
        return {"image": image, "dry_run": dry_run, "files": files, "dirs": dirs,
                "deleted_count": 0 if dry_run else len(files),
                "removed_dir_count": 0 if dry_run else removed_dirs,
                "scope": "local_gallery_and_enrolled_owned_files_only"}

    def _orphan_plan(self, image: str) -> tuple[list[str], list[str]]:
        # A lifecycle record whose image is already gone: remove the record, any
        # still-present owned files/thumbnail, and any enrolled directory trees.
        record = self._json(f".lifecycle/{image}.json")
        owned = record.get("owned_paths", [])
        owned_dirs = record.get("owned_dirs", [])
        owned = owned if isinstance(owned, list) else []
        owned_dirs = owned_dirs if isinstance(owned_dirs, list) else []
        files = []
        for path in [*owned[:MAX_OWNED_FILES], f".thumbnails/{image}.jpg",
                     f".lifecycle/{image}.json"]:
            try:
                _parts(path)
            except ValueError:
                continue
            if path.startswith(".lifecycle/") and path != f".lifecycle/{image}.json":
                continue
            if self._regular(path):
                files.append(path)
        walk_dirs = []
        for owned_dir in sorted(set(owned_dirs[:MAX_OWNED_FILES])):
            try:
                parts = _parts(owned_dir)
            except ValueError:
                continue
            if parts[0] in RESERVED_DIRS:
                continue
            walked_files, walked_dirs = self._walk_owned_dir(owned_dir)
            files.extend(walked_files)
            walk_dirs.extend(walked_dirs)
        files = sorted(set(files))
        return files, self._ordered_removable_dirs(files, walk_dirs)

    def _quarantine_plan(self, event: str | None = None) -> tuple[list[str], list[str]]:
        """Every file and directory under quarantine/ (or one entry in it). A non-directory entry
        (a stray file or link) is unlinked itself; nothing is ever followed."""
        target = QUARANTINE_DIR if event is None else f"{QUARANTINE_DIR}/{event}"
        _parts(target)
        if not self._is_owned_dir(target):
            if event is not None:
                with self._parent(target) as (parent, name):
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        return [], []
                return [target], []
            return [], []
        files, dirs = self._walk_owned_dir(target)
        return sorted(set(files)), self._ordered_removable_dirs(files, dirs)

    def _uncertain_plan(self) -> tuple[list[str], list[str]]:
        """Every file and directory under uncertain/; nothing is ever followed."""
        if not self._is_owned_dir(UNCERTAIN_DIR):
            return [], []
        files, dirs = self._walk_owned_dir(UNCERTAIN_DIR)
        return sorted(set(files)), self._ordered_removable_dirs(files, dirs)

    def _expired_quarantine_events(self, now: float, days: int) -> tuple[list[str], list[dict]]:
        """Entries past retention. An entry expires only when its own mtime is past retention and,
        if it has a readable receipt, the receipt's quarantined_at is too (so {"quarantined_at": 0}
        cannot expire a new capture). A receipt-less staging-<id> expires by mtime alone. Every
        failure is recorded per item and the sweep continues."""
        expired, errors = [], []
        descriptor = self._open_dir_nofollow(QUARANTINE_DIR)
        if descriptor is None:
            return expired, errors
        try:
            names = sorted(os.listdir(descriptor))[:MAX_FILES]
            limit = days * 86400
            for name in names:
                try:
                    _parts(f"{QUARANTINE_DIR}/{name}")
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if now - info.st_mtime < limit:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            receipt = self._json(f"{QUARANTINE_DIR}/{name}/receipt.json")
                            stamp = receipt.get("quarantined_at") if receipt else None
                            if receipt and (type(stamp) not in {int, float} or not math.isfinite(stamp)
                                            or stamp < 0):
                                raise ValueError("invalid quarantine receipt")
                        except Exception as exc:  # noqa: BLE001 — an unreadable receipt falls back to mtime
                            errors.append({"record": f"{QUARANTINE_DIR}/{name}/receipt.json",
                                           "error": type(exc).__name__})
                            stamp = None
                        if stamp is not None and now - stamp < limit:
                            continue
                    expired.append(name)
                except Exception as exc:  # noqa: BLE001 — one bad entry never aborts the sweep
                    errors.append({"record": f"{QUARANTINE_DIR}/{name}", "error": type(exc).__name__})
        finally:
            os.close(descriptor)
        return expired, errors

    def delete(self, image: str, *, dry_run: bool = False) -> dict:
        with _LOCK:
            files, dirs = self._deletion_plan(image)
            with self._guard_sessions(files + dirs):
                return self._delete_files(image, files, dirs, dry_run)

    def delete_all(self, *, confirm: str, dry_run: bool = False) -> dict:
        if confirm != DELETE_ALL_CONFIRMATION:
            raise ValueError("explicit delete-all-local-photos confirmation is required")
        with _LOCK:
            images = [name for name in self._names()
                      if not name.startswith(".") and Path(name).suffix.lower() in IMAGE_SUFFIXES]
            image_set = set(images)
            # Preflight the entire operation before any deletion.
            planned = {image: self._deletion_plan(image) for image in images}
            # Orphan lifecycle records whose image file no longer exists.
            orphans = {}
            for name in self._names(".lifecycle"):
                if name == "retention.json" or not name.endswith(".json"):
                    continue
                record_image = name[:-5]
                if record_image in image_set:
                    continue
                try:
                    _image_name(record_image)
                except ValueError:
                    continue
                orphans[record_image] = self._orphan_plan(record_image)
            guarded = [path for files, dirs in planned.values() for path in files + dirs]
            guarded += [path for files, dirs in orphans.values() for path in files + dirs]
            quarantine_files, quarantine_dirs = self._quarantine_plan()
            uncertain_files, uncertain_dirs = self._uncertain_plan()
            with self._guard_sessions(guarded):
                results = [self._delete_files(image, *planned[image], dry_run)
                           for image in images if self._regular(image)]
                results += [self._delete_files(record_image, files, dirs, dry_run)
                            for record_image, (files, dirs) in orphans.items()
                            if files or dirs]
                if quarantine_files or quarantine_dirs:
                    results.append(self._delete_files(QUARANTINE_DIR, quarantine_files, quarantine_dirs, dry_run))
                if uncertain_files or uncertain_dirs:
                    results.append(self._delete_files(UNCERTAIN_DIR, uncertain_files, uncertain_dirs, dry_run))
        return {"dry_run": dry_run, "photos": results,
                "deleted_count": sum(item["deleted_count"] for item in results),
                "removed_dir_count": sum(item.get("removed_dir_count", 0) for item in results),
                "scope": "local_gallery_and_enrolled_owned_files_only"}

    def enforce_retention(self, *, now: float | None = None, dry_run: bool = False) -> dict:
        now = time.time() if now is None else now
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid retention clock")
        with _LOCK:
            days = self.retention()["days"]
            results, errors = [], []
            if days is not None:
                for name in self._names(".lifecycle"):
                    if name == "retention.json" or not name.endswith(".json"):
                        continue
                    try:
                        image = _image_name(name[:-5])
                        record = self._json(f".lifecycle/{name}")
                        enrolled = record.get("enrolled_at")
                        if (record.get("image") != image or type(enrolled) not in {int, float}
                                or not math.isfinite(enrolled) or enrolled < 0):
                            raise ValueError("invalid enrollment record")
                        if now - enrolled >= days * 86400:
                            results.append(self.delete(image, dry_run=dry_run))
                    except Exception as exc:  # noqa: BLE001 — per item: one bad record never aborts the sweep
                        errors.append({"record": name, "error": type(exc).__name__})
                # Quarantined downloads follow the same policy, by their receipt's quarantined_at.
                expired, quarantine_errors = self._expired_quarantine_events(now, days)
                errors.extend(quarantine_errors)
                for event in expired:
                    try:
                        files, dirs = self._quarantine_plan(event)
                        if files or dirs:
                            results.append(self._delete_files(f"{QUARANTINE_DIR}/{event}", files, dirs, dry_run))
                    except Exception as exc:  # noqa: BLE001 — per item
                        errors.append({"record": f"{QUARANTINE_DIR}/{event}", "error": type(exc).__name__})
        return {"days": days, "dry_run": dry_run, "photos": results, "errors": errors,
                "deleted_count": sum(item["deleted_count"] for item in results)}


def manage_shot(root: Path | str, image: str, owned_paths=(), owned_dirs=()) -> dict:
    """Call only after saving a newly created photo and its sidecar successfully."""
    lifecycle = PhotoLifecycle(root)
    record = lifecycle.enroll(image, owned_paths, owned_dirs)
    return {"managed": record, "retention": lifecycle.enforce_retention()}
