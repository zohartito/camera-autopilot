"""Contract checks for the A7 II falsification capability matrix."""

from __future__ import annotations

import re
from pathlib import Path

ALLOWED_STATUSES = {"PROVEN-YES", "PROVEN-NO", "NEEDS-HARDWARE", "NEEDS-PACKAGE"}
PATH_REF_RE = re.compile(r"`([^`]+)`")
REPO_PATH_PREFIXES = (
    "artifacts/",
    "backend/",
    "data/",
    "docs/",
    "scripts/",
    "tests/",
    "tools/",
)


def _strip_status(value: str) -> str:
    return value.strip().strip("`").strip()


def _parse_matrix_rows(matrix_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in matrix_text.splitlines():
        if not (line.startswith("| R") or line.startswith("| Cross-route")):
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) < 5 or parts[2] == "Current Status":
            continue
        rows.append(
            {
                "route": parts[0],
                "capability": parts[1],
                "status": _strip_status(parts[2]),
                "evidence": parts[3],
                "next": parts[4],
            }
        )
    return rows


def _row_ref(row: dict[str, str]) -> dict[str, str]:
    return {
        "route": row["route"],
        "capability": row["capability"],
    }


def _repo_relative_path_refs(text: str) -> list[str]:
    refs = []
    for match in PATH_REF_RE.finditer(text):
        value = match.group(1).strip().strip(".,:;")
        if any(value.startswith(prefix) for prefix in REPO_PATH_PREFIXES):
            refs.append(value)
    return refs


def validate_matrix_contract(
    matrix_path: str | Path,
    *,
    recommendation_path: str | Path,
    repo_root: str | Path | None = None,
) -> dict[str, object]:
    """Return a machine-checkable summary of the falsification matrix contract."""
    matrix_path = Path(matrix_path)
    recommendation_path = Path(recommendation_path)
    repo_root_path = Path(repo_root) if repo_root is not None else matrix_path.parent.parent.parent
    matrix_text = matrix_path.read_text()
    rows = _parse_matrix_rows(matrix_text)
    matrix_tombstoned = "HISTORICAL / ARCHIVED" in "\n".join(matrix_text.splitlines()[:8])

    bad_status_rows = [
        {
            "route": row["route"],
            "capability": row["capability"],
            "status": row["status"],
        }
        for row in rows
        if row["status"] not in ALLOWED_STATUSES
    ]
    unknown_status_rows = [
        {
            "route": row["route"],
            "capability": row["capability"],
            "status": row["status"],
        }
        for row in rows
        if "UNKNOWN" in row["status"]
    ]
    proven_no_without_repeated_artifact = [
        _row_ref(row)
        for row in rows
        if row["status"] == "PROVEN-NO"
        and (
            "artifact" not in row["evidence"].lower()
            or ("repeat" not in row["evidence"].lower() and "3x" not in row["evidence"].lower())
        )
    ]
    proven_yes_without_artifact = [
        _row_ref(row)
        for row in rows
        if row["status"] == "PROVEN-YES" and "artifact" not in row["evidence"].lower()
    ]
    needs_hardware_without_specific_gate = [
        _row_ref(row)
        for row in rows
        if row["status"] == "NEEDS-HARDWARE"
        and (
            not row["next"].strip()
            or (
                "run:" not in row["next"].lower()
                and "same as" not in row["next"].lower()
                and "put camera" not in row["next"].lower()
                and "with usb" not in row["next"].lower()
                and "re-enter" not in row["next"].lower()
            )
        )
    ]
    needs_package_without_review_gate = [
        _row_ref(row)
        for row in rows
        if row["status"] == "NEEDS-PACKAGE"
        and (
            "package" not in row["evidence"].lower()
            or "package" not in row["next"].lower()
            or "review" not in row["next"].lower()
        )
    ]
    evidence_artifact_paths_checked = []
    missing_evidence_artifacts = []
    for row in rows:
        for path_ref in _repo_relative_path_refs(row["evidence"]):
            evidence_artifact_paths_checked.append(path_ref)
            if not (repo_root_path / path_ref).exists():
                missing_evidence_artifacts.append(
                    {
                        "route": row["route"],
                        "capability": row["capability"],
                        "path": path_ref,
                    }
                )

    recommendation_exists = (not matrix_tombstoned) and recommendation_path.exists() and recommendation_path.stat().st_size > 0
    recommendation_text = recommendation_path.read_text() if recommendation_exists else ""
    recommendation_rel = recommendation_path.as_posix()
    try:
        recommendation_rel = recommendation_path.relative_to(repo_root_path).as_posix()
    except ValueError:
        pass

    return {
        "matrix_tombstoned": matrix_tombstoned,
        "rows": len(rows),
        "statuses": sorted({row["status"] for row in rows}),
        "bad_status_rows": bad_status_rows,
        "unknown_status_rows": unknown_status_rows,
        "proven_yes_without_artifact": proven_yes_without_artifact,
        "needs_hardware_without_specific_gate": needs_hardware_without_specific_gate,
        "needs_package_without_review_gate": needs_package_without_review_gate,
        "proven_no_without_repeated_artifact": proven_no_without_repeated_artifact,
        "evidence_artifact_paths_checked": sorted(set(evidence_artifact_paths_checked)),
        "missing_evidence_artifacts": missing_evidence_artifacts,
        "recommendation_linked": recommendation_rel in matrix_text,
        "recommendation_exists": recommendation_exists,
        "recommendation_mentions_no_final_hardware": "Do not buy final product hardware yet" in recommendation_text,
        "recommendation_mentions_route_gates": (
            "Gate A: Official Command Package" in recommendation_text
            and "Gate B: Smart Remote Current Live Surface" in recommendation_text
            and "Gate C: USB Behavior, Not Just Inventory" in recommendation_text
            and "Gate D: Raw PTP Focus Point" in recommendation_text
            and "Gate E: OpenMemories Proof App" in recommendation_text
        ),
        "recommendation_uses_runnable_camera_remote_command_examples": (
            not matrix_tombstoned
            and "$CAMERA_REMOTE_COMMAND_PACKAGE" not in recommendation_text
            and "CAMERA_REMOTE_COMMAND_PACKAGE=/path" not in recommendation_text
        ),
    }
