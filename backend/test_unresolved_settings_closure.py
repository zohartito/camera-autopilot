"""Camera-free tests for deterministic unresolved-settings evidence selection."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_unresolved_settings_closure


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_unresolved_closure_prefers_canonical_route_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        review_dir = Path(tmp)
        private = review_dir / "20991231-usb-gphoto-review.json"
        canonical = (
            review_dir / a7ii_unresolved_settings_closure.CANONICAL_REVIEW_NAME
        )
        _write_json(private, {"summary": {"source": "private"}})
        _write_json(canonical, {"summary": {"source": "canonical"}})

        selected = a7ii_unresolved_settings_closure.deterministic_latest(
            review_dir,
            ("*usb-gphoto-review.json",),
        )

    assert selected == canonical
    print("  unresolved closure canonical route-review precedence: ok")


def test_unresolved_closure_prefers_path_free_canonical_locator() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        locator_dir = Path(tmp)
        private = locator_dir / "20991231-command-package-locator.json"
        canonical = (
            locator_dir / a7ii_unresolved_settings_closure.CANONICAL_LOCATOR_NAME
        )
        _write_json(
            private,
            {
                "search": {"complete_with_policy_exclusions": True},
                "summary": {"reviewable_candidates": 1},
                "candidates": [{"local_project_material": False}],
            },
        )
        _write_json(
            canonical,
            {
                "search": {"search_root_count": 7},
                "summary": {
                    "result": "official_package_not_found_locally",
                    "reviewable_candidates": 0,
                },
            },
        )

        selected = a7ii_unresolved_settings_closure.deterministic_best_locator(
            locator_dir
        )
        canonical_text = canonical.read_text(encoding="utf-8")

    assert selected == canonical
    assert "/Users/" not in canonical_text
    print("  unresolved closure canonical locator precedence: ok")


if __name__ == "__main__":
    test_unresolved_closure_prefers_canonical_route_review()
    test_unresolved_closure_prefers_path_free_canonical_locator()
    print("unresolved_settings_closure tests: PASS")
