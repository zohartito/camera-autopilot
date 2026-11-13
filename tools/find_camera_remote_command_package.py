#!/usr/bin/env python3
"""Find local Sony Camera Remote Command package candidates.

This tool does not download, copy, or redistribute Sony files. It only records local candidate
paths, lightweight metadata, hashes for files, and the exact package-review command to run next.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "r4_camera_remote_command"
CANONICAL_SUMMARY_NAME = "final-package-locator-summary.json"
DEFAULT_ENV_VARS = (
    "CAMERA_REMOTE_COMMAND_PACKAGE",
    "SONY_CAMERA_REMOTE_COMMAND_PACKAGE",
    "SONY_CAMERA_REMOTE_COMMAND_PATH",
)
OFFICIAL_SOURCES = (
    {
        "label": "Sony Camera Remote Command",
        "url": "https://support.d-imaging.sony.co.jp/app/cameraremotecommand/en/index.html",
        "purpose": "Official command documentation and supported-model entry point.",
    },
    {
        "label": "Sony Camera Remote Command download",
        "url": "https://pro.sony/ue_US/digital-imaging/camera-remote-command-download",
        "purpose": "Approved package acquisition path; package must stay local and unredistributed.",
    },
)
NAME_PATTERNS = (
    re.compile(r"camera[\s_-]*remote[\s_-]*command", re.IGNORECASE),
    re.compile(r"remote[\s_-]*command", re.IGNORECASE),
)
SDK_NAME_PATTERNS = (
    re.compile(r"crsdk", re.IGNORECASE),
    re.compile(r"camera[\s_-]*remote[\s_-]*sdk", re.IGNORECASE),
    re.compile(r"remote[\s_-]*sdk", re.IGNORECASE),
)
PACKAGE_FILE_SUFFIXES = {
    ".zip",
    ".dmg",
    ".pkg",
    ".tar",
    ".tgz",
    ".gz",
    ".7z",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".htm",
    ".html",
    ".xml",
    ".json",
    ".xlsx",
    ".docx",
}
ARCHIVE_SUFFIXES = {".zip"}
MANUAL_EXTRACTION_SUFFIXES = {".7z", ".dmg", ".gz", ".pkg", ".tar", ".tgz"}
TEXT_PROBE_SUFFIXES = {".csv", ".htm", ".html", ".json", ".md", ".rst", ".txt", ".xml", ".yaml", ".yml"}
TEXT_PROBE_BASENAMES = {"compatibility", "license", "readme", "release", "releasenotes"}
IGNORED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "artifacts",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}
IGNORED_DIR_SUFFIXES = {".app"}
MAX_PROBE_BYTES = 2 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_TEXT_MEMBERS = 100


def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_probe(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "utf-16-le", "cp932", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _pattern_matches(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def _matches_name(path: Path) -> bool:
    return _pattern_matches(path.name, NAME_PATTERNS)


def _matches_sdk_name(path: Path) -> bool:
    return _pattern_matches(path.name, SDK_NAME_PATTERNS)


def _should_probe_text(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_PROBE_SUFFIXES:
        return False
    stem = path.stem.lower().replace("_", "").replace("-", "").replace(" ", "")
    if stem in TEXT_PROBE_BASENAMES:
        return True
    lowered = path.name.lower()
    return any(token in lowered for token in ("camera", "command", "ptp", "remote", "sony"))


def _content_signals(path: Path) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "inspected": False,
        "inspection_kind": None,
        "command_marker": False,
        "sdk_marker": False,
        "ilce_7m2_marker": False,
        "marker_members": [],
        "member_count": None,
        "inspection_truncated": False,
        "inspection_error": None,
    }
    if not path.is_file():
        return signals

    try:
        if path.suffix.lower() in ARCHIVE_SUFFIXES and zipfile.is_zipfile(path):
            signals["inspected"] = True
            signals["inspection_kind"] = "zip_manifest_and_bounded_text"
            with zipfile.ZipFile(path) as archive:
                members = [info for info in archive.infolist() if not info.is_dir()]
                signals["member_count"] = len(members)
                if len(members) > MAX_ZIP_MEMBERS:
                    signals["inspection_truncated"] = True
                text_members = 0
                for info in members[:MAX_ZIP_MEMBERS]:
                    marker_text = info.filename
                    should_read = (
                        Path(info.filename).suffix.lower() in TEXT_PROBE_SUFFIXES
                        and info.file_size <= MAX_PROBE_BYTES
                        and text_members < MAX_ZIP_TEXT_MEMBERS
                    )
                    if should_read:
                        marker_text += "\n" + _decode_probe(archive.read(info))
                        text_members += 1
                    command = _pattern_matches(marker_text, NAME_PATTERNS)
                    sdk = _pattern_matches(marker_text, SDK_NAME_PATTERNS)
                    model = "ilce-7m2" in marker_text.lower()
                    if command or sdk or model:
                        signals["marker_members"].append(info.filename)
                    signals["command_marker"] = signals["command_marker"] or command
                    signals["sdk_marker"] = signals["sdk_marker"] or sdk
                    signals["ilce_7m2_marker"] = signals["ilce_7m2_marker"] or model
                if text_members >= MAX_ZIP_TEXT_MEMBERS and len(members) > text_members:
                    signals["inspection_truncated"] = True
            signals["marker_members"] = signals["marker_members"][:50]
            return signals

        if _should_probe_text(path) and path.stat().st_size <= MAX_PROBE_BYTES:
            signals["inspected"] = True
            signals["inspection_kind"] = "bounded_text"
            text = _decode_probe(path.read_bytes())
            signals["command_marker"] = _pattern_matches(text, NAME_PATTERNS)
            signals["sdk_marker"] = _pattern_matches(text, SDK_NAME_PATTERNS)
            signals["ilce_7m2_marker"] = "ilce-7m2" in text.lower()
            if signals["command_marker"] or signals["sdk_marker"] or signals["ilce_7m2_marker"]:
                signals["marker_members"] = [path.name]
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        signals["inspection_error"] = f"{type(exc).__name__}: {exc}"
    return signals


def _looks_like_local_project_material(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in TEXT_PROBE_SUFFIXES:
        return False
    parts = [part.lower() for part in path.parts]
    if any(part == "sony-camera-mcp" or part.startswith("camera-autopilot") for part in parts):
        return True
    lowered = str(path).lower()
    return "knowledge/camera-autopilot/" in lowered


def _default_search_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Downloads",
        home / "Desktop",
    ]


def _common_search_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        home / "Development",
        home / "Code",
        home / "SynologyDrive",
        home / "Library" / "CloudStorage",
    ]


def _path_key(path: Path) -> str:
    expanded = path.expanduser()
    try:
        return str(expanded.resolve(strict=False))
    except OSError:
        return str(expanded)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _candidate_from_path(path: Path, *, source: str) -> dict[str, Any]:
    exists = path.exists()
    is_file = path.is_file()
    is_dir = path.is_dir()
    suffix = path.suffix.lower()
    plausible_package = bool(is_dir or (is_file and suffix in PACKAGE_FILE_SUFFIXES))
    name_matches = _matches_name(path)
    sdk_name_matches = _matches_sdk_name(path)
    content = _content_signals(path)
    command_signal = bool(name_matches or content["command_marker"])
    sdk_signal = bool(sdk_name_matches or content["sdk_marker"])
    sdk_only = bool(sdk_signal and not command_signal)
    local_project_material = _looks_like_local_project_material(path)
    is_zip = zipfile.is_zipfile(path) if is_file else False
    size = path.stat().st_size if is_file else None
    reviewable = False
    excluded_reason = None
    next_action = None
    if not exists:
        candidate_status = "missing"
    elif local_project_material:
        candidate_status = "local_project_material_not_official_package"
        excluded_reason = candidate_status
    elif sdk_only:
        candidate_status = "camera_remote_sdk_not_command"
        excluded_reason = candidate_status
    elif not plausible_package:
        candidate_status = "not_package_like"
    elif is_file and suffix == ".zip" and not is_zip:
        candidate_status = "invalid_zip"
        excluded_reason = candidate_status
    elif not command_signal:
        candidate_status = "no_camera_remote_command_identity"
    elif is_file and suffix in MANUAL_EXTRACTION_SUFFIXES:
        candidate_status = "manual_extraction_required"
        next_action = (
            f"Obtain the publisher's regular ZIP package for {shlex.quote(str(path.resolve()))}; "
            "the reviewer accepts ZIP files only."
        )
    elif is_file and suffix != ".zip":
        candidate_status = "reference_candidate_requires_manual_validation"
        next_action = (
            "Validate this file's Sony provenance and obtain its regular ZIP distribution; "
            "the reviewer accepts ZIP files only."
        )
    else:
        candidate_status = "reviewable"
        reviewable = True
        next_action = (
            "python3 tools/review_camera_remote_command_package.py "
            f"{shlex.quote(str(path.resolve()))} "
            "--out-dir artifacts/falsification/r4_camera_remote_command"
        )
    return {
        "path": _repo_path(path),
        "absolute_path": str(path.resolve()) if exists else str(path.expanduser()),
        "source": source,
        "exists": exists,
        "kind": "directory" if is_dir else "file" if is_file else "missing",
        "name_matches": name_matches,
        "sdk_name_matches": sdk_name_matches,
        "excluded_reason": excluded_reason,
        "candidate_status": candidate_status,
        "command_identity_signal": command_signal,
        "sdk_identity_signal": sdk_signal,
        "local_project_material": local_project_material,
        "content_inspection": content,
        "plausible_package": plausible_package,
        "is_zip": is_zip,
        "size_bytes": size,
        "sha256": _sha256_file(path) if is_file else None,
        "reviewable": reviewable,
        "review_command": next_action if reviewable else None,
        "next_action": next_action,
    }


def _is_discoverable_candidate(path: Path) -> bool:
    if _matches_name(path) or _matches_sdk_name(path):
        return True
    if not path.is_file():
        return False
    if path.suffix.lower() not in ARCHIVE_SUFFIXES and not _should_probe_text(path):
        return False
    signals = _content_signals(path)
    return bool(signals["command_marker"] or signals["sdk_marker"])


def _iter_search_dir(
    search_dir: Path,
    *,
    max_depth: int | None,
    max_candidates: int,
) -> tuple[list[Path], dict[str, Any]]:
    search_dir = search_dir.expanduser()
    coverage: dict[str, Any] = {
        "root": str(search_dir),
        "exists": search_dir.exists(),
        "paths_visited": 0,
        "candidate_matches": 0,
        "returned_candidates": 0,
        "policy_pruned_dirs": 0,
        "depth_pruned_dirs": 0,
        "errors": [],
        "candidate_limit_reached": False,
        "complete": False,
    }
    if not search_dir.exists() or not search_dir.is_dir():
        return [], coverage
    found: list[Path] = []

    def onerror(error: OSError) -> None:
        coverage["errors"].append(f"{type(error).__name__}: {error}")

    root_depth = len(search_dir.resolve().parts)
    for current, dirs, files in os.walk(search_dir, topdown=True, followlinks=False, onerror=onerror):
        current_path = Path(current)
        try:
            depth = len(current_path.resolve().parts) - root_depth
        except OSError as exc:
            coverage["errors"].append(f"{type(exc).__name__}: {exc}")
            dirs[:] = []
            continue
        kept_dirs = []
        for name in sorted(dirs):
            if (
                name.startswith(".")
                or name in IGNORED_DIR_NAMES
                or Path(name).suffix.lower() in IGNORED_DIR_SUFFIXES
            ):
                coverage["policy_pruned_dirs"] += 1
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        if max_depth is not None and depth >= max_depth:
            coverage["depth_pruned_dirs"] += len(dirs)
            dirs[:] = []

        for name in sorted(files):
            path = current_path / name
            coverage["paths_visited"] += 1
            try:
                if not _is_discoverable_candidate(path):
                    continue
            except OSError as exc:
                coverage["errors"].append(f"{type(exc).__name__}: {exc}")
                continue
            coverage["candidate_matches"] += 1
            if len(found) < max_candidates:
                found.append(path)
            else:
                coverage["candidate_limit_reached"] = True

        for name in dirs:
            path = current_path / name
            if _matches_name(path) or _matches_sdk_name(path):
                coverage["candidate_matches"] += 1
                if len(found) < max_candidates:
                    found.append(path)
                else:
                    coverage["candidate_limit_reached"] = True

    coverage["returned_candidates"] = len(found)
    coverage["complete"] = bool(
        coverage["exists"]
        and not coverage["errors"]
        and not coverage["candidate_limit_reached"]
        and coverage["depth_pruned_dirs"] == 0
    )
    return found, coverage


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a commit-safe package-search summary without local filesystem paths."""
    summary = report.get("summary") or {}
    search = report.get("search") or {}
    result = summary.get("result")
    safe_summary_keys = (
        "result",
        "candidates",
        "reviewable_candidates",
        "manual_extraction_candidates",
        "manual_reference_candidates",
        "name_matched_reviewable_candidates",
        "external_blocker",
    )
    if result == "found_reviewable_package":
        next_action = (
            "Review the approved local package with "
            "python3 tools/review_camera_remote_command_package.py "
            "/path/to/CameraRemoteCommand-package.zip "
            "--out-dir artifacts/falsification/r4_camera_remote_command."
        )
    elif result == "candidate_requires_manual_extraction":
        next_action = (
            "Extract the candidate locally without committing Sony package contents, then rerun "
            "the package locator and reviewer."
        )
    elif result == "reference_candidates_require_manual_validation":
        next_action = (
            "Validate candidate provenance against Sony's official delivery, then rerun the "
            "package locator with the approved local package."
        )
    else:
        next_action = (
            "Apply through Sony's eligible corporate-customer download path, keep the delivered "
            "package local, then rerun the locator with --candidate /path/to/package."
        )
    return {
        "schema_version": 1,
        "route": "camera_remote_command",
        "generated_at": report.get("generated_at"),
        "policy": report.get("policy") or {},
        "official_sources": report.get("official_sources") or [],
        "search": {
            "search_root_count": len(search.get("search_dirs") or []),
            "existing_search_root_count": len(search.get("existing_search_dirs") or []),
            "missing_search_root_count": len(search.get("missing_search_dirs") or []),
            "max_depth": search.get("max_depth"),
            "max_candidates": search.get("max_candidates"),
            "complete_with_policy_exclusions": bool(
                search.get("complete_with_policy_exclusions")
            ),
        },
        "summary": {
            **{key: summary.get(key) for key in safe_summary_keys},
            "best_candidate_present": bool(summary.get("best_candidate")),
            "review_command_available": bool(summary.get("next_review_command")),
            "next_action": next_action,
        },
        "raw_locator_commit_policy": (
            "Timestamped locator detail stays local because it contains filesystem paths; "
            "this path-free summary is the canonical committed search result."
        ),
    }


def build_report(
    *,
    candidates: list[Path] | None = None,
    search_dirs: list[Path] | None = None,
    env_vars: tuple[str, ...] = DEFAULT_ENV_VARS,
    max_depth: int | None = 6,
    max_candidates: int = 100,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> tuple[dict[str, Any], Path]:
    explicit_candidates = candidates or []
    search_dirs = _dedupe_paths(search_dirs if search_dirs is not None else _default_search_dirs())
    all_items: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        key = str(path.expanduser())
        if key in seen:
            return
        seen.add(key)
        all_items.append(_candidate_from_path(path.expanduser(), source=source))

    for name in env_vars:
        value = os.environ.get(name)
        if value:
            add(Path(value), f"env:{name}")
    for path in explicit_candidates:
        add(path, "explicit")
    for directory in search_dirs:
        remaining = max(0, max_candidates - len(all_items))
        paths, directory_coverage = _iter_search_dir(
            directory,
            max_depth=max_depth,
            max_candidates=remaining,
        )
        coverage.append(directory_coverage)
        for path in paths:
            add(path, f"search_dir:{directory.expanduser()}")

    reviewable = [item for item in all_items if item["reviewable"]]
    manual = [item for item in all_items if item["candidate_status"] == "manual_extraction_required"]
    reference_candidates = [
        item for item in all_items if item["candidate_status"] == "reference_candidate_requires_manual_validation"
    ]
    name_matched = [item for item in reviewable if item["name_matches"]]
    best = (name_matched or reviewable or manual or reference_candidates or [None])[0]
    scan_complete = all(item["complete"] for item in coverage) if coverage else True
    if reviewable:
        result = "found_reviewable_package"
    elif manual:
        result = "candidate_requires_manual_extraction"
    elif reference_candidates:
        result = "reference_candidates_require_manual_validation"
    elif not scan_complete:
        result = "package_not_found_scan_incomplete"
    else:
        result = "official_package_not_found_locally"
    stamp = _stamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{stamp}-command-package-locator.json"
    report = {
        "generated_at": stamp,
        "tool": "find_camera_remote_command_package",
        "policy": {
            "downloads_package": False,
            "copies_package": False,
            "redistributes_package": False,
            "records_metadata_only": True,
            "policy_excluded_dirs": sorted(IGNORED_DIR_NAMES),
            "policy_excluded_dir_suffixes": sorted(IGNORED_DIR_SUFFIXES),
        },
        "official_sources": list(OFFICIAL_SOURCES),
        "search": {
            "env_vars": list(env_vars),
            "explicit_candidates": [str(path.expanduser()) for path in explicit_candidates],
            "search_dirs": [str(path.expanduser()) for path in search_dirs],
            "existing_search_dirs": [str(path.expanduser()) for path in search_dirs if path.expanduser().exists()],
            "missing_search_dirs": [str(path.expanduser()) for path in search_dirs if not path.expanduser().exists()],
            "max_depth": max_depth,
            "max_candidates": max_candidates,
            "coverage": coverage,
            "complete_with_policy_exclusions": scan_complete,
        },
        "summary": {
            "result": result,
            "candidates": len(all_items),
            "reviewable_candidates": len(reviewable),
            "manual_extraction_candidates": len(manual),
            "manual_reference_candidates": len(reference_candidates),
            "name_matched_reviewable_candidates": len(name_matched),
            "best_candidate": best["absolute_path"] if best else None,
            "next_review_command": best["review_command"] if best and best.get("reviewable") else None,
            "next_action": (
                best.get("next_action")
                if best and best.get("next_action")
                else (
                    "Apply as an eligible corporate customer at "
                    "https://pro.sony/ue_US/digital-imaging/camera-remote-command-download, "
                    "save the delivered package locally, then rerun this locator with --candidate /path/to/package."
                )
            ),
            "external_blocker": None
            if reviewable
            else "Sony states Camera Remote Command is available only to corporate customers; no verified official package was found locally.",
        },
        "candidates": all_items,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    canonical_path = out_dir / CANONICAL_SUMMARY_NAME
    canonical_path.write_text(
        json.dumps(compact_report(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="append", default=[], help="explicit local package path to check")
    parser.add_argument("--search-dir", action="append", default=[], help="directory to scan for package-like names")
    parser.add_argument("--no-default-search", action="store_true", help="only use env vars and explicit paths")
    parser.add_argument(
        "--include-common-search",
        action="store_true",
        help="also scan common project/cloud folders such as ~/Development, ~/Code, and ~/SynologyDrive",
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="scan all common approved roots without a depth limit (policy-excluded generated/dependency trees remain excluded)",
    )
    parser.add_argument("--max-depth", type=int, default=6, help="directory depth; use -1 for no depth limit")
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    search_dirs = [Path(item) for item in args.search_dir]
    if args.exhaustive or args.include_common_search:
        search_dirs.extend(_common_search_dirs())
    elif not args.no_default_search:
        search_dirs.extend(_default_search_dirs())
    max_depth = None if args.exhaustive or args.max_depth < 0 else args.max_depth
    report, report_path = build_report(
        candidates=[Path(item) for item in args.candidate],
        search_dirs=search_dirs,
        max_depth=max_depth,
        max_candidates=max(0, args.max_candidates),
        out_dir=Path(args.out_dir),
    )
    payload = {
        "report": str(report_path),
        "summary": report["summary"],
        "policy": report["policy"],
    }
    print(json.dumps(report if args.json else payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
