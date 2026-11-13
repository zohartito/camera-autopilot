"""Photo-source inventory for the photographed A7 II settings surface.

The registry owns actual settings rows. This module accounts for every file in the
user-provided photo folder by reconciling registry `photo_sources` with a small inventory of
non-setting context photos such as pairing screens and capture samples.
"""

from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Any

import settings_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY_PATH = REPO_ROOT / "data" / "a7ii_photo_inventory.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".dng", ".arw"}
# Per-process randomness is deliberately the safe default: opaque API IDs need
# only be stable within one response. Deployments that require cross-request
# correlation may provide an independent secret through their configuration.
_INVENTORY_ID_SECRET = os.environ.get("A7II_INVENTORY_ID_SECRET", "").encode() or secrets.token_bytes(32)


class PhotoInventoryError(RuntimeError):
    """Raised when the photo inventory is internally inconsistent."""


def load_inventory(path: str | Path | None = None) -> dict[str, Any]:
    inventory_path = Path(path) if path is not None else DEFAULT_INVENTORY_PATH
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    validate_inventory(inventory)
    return inventory


def validate_inventory(inventory: dict[str, Any]) -> None:
    if not isinstance(inventory, dict):
        raise PhotoInventoryError("photo inventory root must be an object")
    if not isinstance(inventory.get("source_dir"), str) or not inventory["source_dir"].strip():
        raise PhotoInventoryError("photo inventory source_dir must be a non-empty string")
    sources = inventory.get("non_registry_sources")
    if not isinstance(sources, list):
        raise PhotoInventoryError("photo inventory non_registry_sources must be a list")

    seen: set[str] = set()
    errors: list[str] = []
    required = {
        "filename",
        "classification",
        "role",
        "description",
        "setting_ids",
        "routes",
        "sensitive",
        "next_action",
    }
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            errors.append(f"non_registry_sources[{index}] must be an object")
            continue
        missing = sorted(required - set(item))
        if missing:
            errors.append(f"non_registry_sources[{index}] missing fields: {missing}")
            continue
        filename = item["filename"]
        if not isinstance(filename, str) or not filename:
            errors.append(f"non_registry_sources[{index}] filename must be a non-empty string")
        if filename in seen:
            errors.append(f"duplicate non-registry photo source: {filename}")
        seen.add(filename)
        if Path(filename).name != filename:
            errors.append(f"{filename} must be a bare filename, not a path")
        if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            errors.append(f"{filename} has unsupported image extension")
        for field in ("classification", "role", "description", "next_action"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(f"{filename} {field} must be a non-empty string")
        for field in ("setting_ids", "routes"):
            if not isinstance(item[field], list) or not all(isinstance(value, str) for value in item[field]):
                errors.append(f"{filename} {field} must be a list of strings")
        if item["sensitive"] is not True and item["sensitive"] is not False:
            errors.append(f"{filename} sensitive must be boolean")

    if errors:
        raise PhotoInventoryError("\n".join(errors))


def _source_dir(inventory: dict[str, Any], source_dir: str | Path | None = None) -> Path:
    override = source_dir or os.environ.get("A7II_PHOTO_SOURCE_DIR") or inventory["source_dir"]
    return Path(override).expanduser()


def _source_files(source_dir: Path) -> set[str]:
    if not source_dir.exists():
        return set()
    return {
        path.name
        for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    }


def registry_photo_source_index(registry: dict[str, Any] | None = None) -> dict[str, list[str]]:
    registry = registry or settings_registry.load_registry()
    index: dict[str, list[str]] = {}
    for row in registry["settings"]:
        for filename in row.get("photo_sources", []):
            index.setdefault(filename, []).append(row["setting_id"])
    return {filename: sorted(setting_ids) for filename, setting_ids in sorted(index.items())}


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    source_dir: str | Path | None = None,
    include_registry_sources: bool = True,
    redact_identifiers: bool = False,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    inventory = inventory or load_inventory()
    validate_inventory(inventory)

    source_root = _source_dir(inventory, source_dir)
    source_dir_exists = source_root.exists()
    source_files = _source_files(source_root)
    registry_index = registry_photo_source_index(registry)
    non_registry = {item["filename"]: item for item in inventory["non_registry_sources"]}
    registry_files = set(registry_index)
    non_registry_files = set(non_registry)
    accounted_files = registry_files | non_registry_files
    setting_ids = set(settings_registry.settings_by_id(registry))

    unaccounted_source_files = sorted(source_files - accounted_files) if source_dir_exists else []
    missing_registry_photo_files = sorted(registry_files - source_files) if source_dir_exists else []
    missing_non_registry_files = sorted(non_registry_files - source_files) if source_dir_exists else []
    inventory_overlaps_registry = sorted(non_registry_files & registry_files)
    invalid_setting_links = sorted(
        {
            setting_id
            for item in non_registry.values()
            for setting_id in item.get("setting_ids", [])
            if setting_id not in setting_ids
        }
    )

    records: list[dict[str, Any]] = []
    if include_registry_sources:
        for filename, linked_setting_ids in registry_index.items():
            records.append(
                {
                    "filename": filename,
                    "role": "registry_source",
                    "classification": "photographed_setting",
                    "setting_ids": linked_setting_ids,
                    "routes": sorted(
                        {
                            route
                            for setting_id in linked_setting_ids
                            for route in settings_registry.get_setting(setting_id, registry)["routes"]
                        }
                    ),
                    "present": filename in source_files if source_dir_exists else None,
                    "sensitive": False,
                    "next_action": "Controlled by the canonical settings registry.",
                }
            )
    for item in inventory["non_registry_sources"]:
        records.append(
            {
                **item,
                "present": item["filename"] in source_files if source_dir_exists else None,
            }
        )

    class_counts: dict[str, int] = {}
    for item in records:
        classification = item["classification"]
        class_counts[classification] = class_counts.get(classification, 0) + 1

    coverage_complete = (
        source_dir_exists
        and not unaccounted_source_files
        and not missing_registry_photo_files
        and not missing_non_registry_files
        and not inventory_overlaps_registry
        and not invalid_setting_links
    )
    status = "complete" if coverage_complete else "incomplete"
    if not source_dir_exists:
        status = "source_dir_missing"

    if redact_identifiers:
        # The API is for progress state, not for exfiltrating private photo
        # names or the operator's filesystem layout. Stable opaque IDs let a
        # client correlate records within a report without receiving either.
        records = [
            {
                key: value
                for key, value in record.items()
                if key != "filename"
            }
            | {"source_id": hmac.new(_INVENTORY_ID_SECRET, record["filename"].encode("utf-8"), hashlib.sha256).hexdigest()[:16]}
            for record in records
        ]

    summary = {
        "status": status,
        "coverage_complete": coverage_complete,
        "source_dir": None if redact_identifiers else str(source_root),
        "source_dir_exists": source_dir_exists,
        "source_files": len(source_files) if source_dir_exists else None,
        "registry_photo_sources": len(registry_files),
        "non_registry_sources": len(non_registry_files),
        "accounted_source_files": len(source_files & accounted_files) if source_dir_exists else None,
        "unaccounted_source_files": unaccounted_source_files,
        "missing_registry_photo_files": missing_registry_photo_files,
        "missing_non_registry_files": missing_non_registry_files,
        "inventory_overlaps_registry": inventory_overlaps_registry,
        "invalid_setting_links": invalid_setting_links,
        "class_counts": dict(sorted(class_counts.items())),
    }
    if redact_identifiers:
        # These lists contain source filenames just as surely as a record does.
        # The API exposes only their operational impact.
        for key in (
            "unaccounted_source_files", "missing_registry_photo_files",
            "missing_non_registry_files", "inventory_overlaps_registry",
        ):
            summary[f"{key}_count"] = len(summary.pop(key))

    return {
        "summary": summary,
        "records": sorted(records, key=lambda item: item.get("filename") or item["source_id"]),
    }


def to_markdown(report: dict[str, Any], *, limit_records: int = 80) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Photo Inventory",
        "",
        f"- status: `{summary['status']}`",
        f"- coverage_complete: `{summary['coverage_complete']}`",
        f"- source_dir: `{summary['source_dir']}`",
        f"- source_dir_exists: `{summary['source_dir_exists']}`",
        f"- source_files: `{summary['source_files']}`",
        f"- registry_photo_sources: `{summary['registry_photo_sources']}`",
        f"- non_registry_sources: `{summary['non_registry_sources']}`",
        f"- accounted_source_files: `{summary['accounted_source_files']}`",
        f"- unaccounted_source_files: `{', '.join(summary['unaccounted_source_files']) or 'none'}`",
        f"- missing_registry_photo_files: `{', '.join(summary['missing_registry_photo_files']) or 'none'}`",
        f"- missing_non_registry_files: `{', '.join(summary['missing_non_registry_files']) or 'none'}`",
        "",
        "## Class Counts",
        "",
    ]
    for classification, count in summary["class_counts"].items():
        lines.append(f"- `{classification}`: {count}")

    lines.extend(["", "## Records", ""])
    records = report["records"] if limit_records <= 0 else report["records"][:limit_records]
    for item in records:
        setting_ids = ", ".join(item.get("setting_ids") or []) or "none"
        routes = ", ".join(item.get("routes") or []) or "none"
        lines.append(f"### {item['filename']}")
        lines.append("")
        lines.append(f"- role: `{item['role']}`")
        lines.append(f"- classification: `{item['classification']}`")
        lines.append(f"- present: `{item['present']}`")
        lines.append(f"- setting_ids: `{setting_ids}`")
        lines.append(f"- routes: `{routes}`")
        lines.append(f"- sensitive: `{item.get('sensitive', False)}`")
        lines.append(f"- next_action: {item['next_action']}")
        lines.append("")
    hidden = len(report["records"]) - len(records)
    if hidden > 0:
        lines.append(f"_Plus {hidden} more record(s); rerun with --limit 0._")
    return "\n".join(lines).rstrip() + "\n"
