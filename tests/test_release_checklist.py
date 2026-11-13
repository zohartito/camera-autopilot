"""Static guards for the release-candidate deliverables.

These tests do not run the checklist or touch a camera/network. They assert the
release checklist script exists, is executable, and still contains each required
check, and that THIRD_PARTY_NOTICES.md accounts for every top-level package in
requirements-test.txt.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKLIST = REPO / "scripts" / "release_checklist.sh"
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"
REQ_TEST = REPO / "requirements-test.txt"


def test_checklist_exists_and_executable():
    assert CHECKLIST.is_file(), f"missing {CHECKLIST}"
    assert os.access(CHECKLIST, os.X_OK), "release_checklist.sh must be executable (chmod +x)"


@pytest.mark.parametrize(
    "needle",
    [
        # Python version gate
        r"REQUIRED_MINOR|python3\.14",
        # virtualenv
        r"\.venv",
        # installs the verified lock
        r"requirements-test\.lock",
        # runs the camera-free gate
        r"scripts/test_safe\.sh",
        # read-only inspectors
        r"deployment_preflight\.py",
        r"host_readiness",
        # clean tree + pushed
        r"git fetch",
        r"git status|git rev-parse @\{u\}|--abbrev-ref",
        # sensitive-artifact scan
        r"captures/",
        r"token",
        r"\\.pt\$",
        r"\\.onnx\$",
        # PASS/FAIL summary
        r"RELEASE CHECKLIST: (PASS|FAIL)|passed, .*failed",
    ],
)
def test_checklist_contains_required_check(needle):
    text = CHECKLIST.read_text()
    assert re.search(needle, text), f"release_checklist.sh missing required check matching /{needle}/"


def test_checklist_is_non_destructive():
    """The checklist must never hard-delete data."""
    text = CHECKLIST.read_text()
    assert "rm -rf" not in text, "release_checklist.sh must not contain rm -rf"


def _top_level_packages(req_path: Path, _seen: set | None = None) -> set[str]:
    """Collect distribution names declared directly in a requirements file,
    following `-r other.txt` includes. Strips version specifiers and extras."""
    seen = _seen if _seen is not None else set()
    names: set[str] = set()
    for raw in req_path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "--requirement")):
            ref = line.split(maxsplit=1)[1].strip()
            ref_path = (req_path.parent / ref).resolve()
            if ref_path not in seen:
                seen.add(ref_path)
                names |= _top_level_packages(ref_path, seen)
            continue
        if line.startswith("-"):
            continue
        # strip extras "[...]" and any version/marker specifier
        name = re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name)
    return names


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_notices_cover_every_top_level_test_package():
    assert NOTICES.is_file(), f"missing {NOTICES}"
    notice_text = _normalize(NOTICES.read_text())
    packages = _top_level_packages(REQ_TEST)
    # sanity: the direct test deps we expect to enumerate
    assert {"fastapi", "uvicorn", "pytest", "httpx", "numpy", "pillow"} <= {
        _normalize(p) for p in packages
    }, f"unexpected top-level parse: {sorted(packages)}"
    missing = [p for p in sorted(packages) if _normalize(p) not in notice_text]
    assert not missing, f"THIRD_PARTY_NOTICES.md does not list: {missing}"


def test_notices_do_not_claim_the_repo_is_unlicensed():
    """LICENSE (AGPL-3.0-or-later) exists; the notices must name it and link it."""
    text = NOTICES.read_text()
    assert (REPO / "LICENSE").is_file()
    assert "no owner-selected project license" not in text
    assert "has no project license" not in text.lower()
    assert re.search(r"\[`LICENSE`\]\(LICENSE\)", text), "notices must link the project LICENSE"
    assert "AGPL-3.0-or-later" in text and "GNU AFFERO GENERAL PUBLIC LICENSE" in (REPO / "LICENSE").read_text()


def test_notices_record_the_loaded_clip_weights_licence():
    """The notices must name the exact CLIP weights the runtime loads, with their licence and source.

    Every identity field is read from the code that loads the weights, so changing the model,
    pretrained tag, repository, revision, file or digest without a new notice fails here.
    """
    resources = (REPO / "backend" / "runtime_resources.py").read_text()
    embed = (REPO / "backend" / "embed_index.py").read_text()
    repo_id = re.search(r'^CLIP_REPO = "([^"]+)"', resources, re.M).group(1)
    revision = re.search(r'^CLIP_REVISION = "([0-9a-f]{40})"', resources, re.M).group(1)
    digest = re.search(r'^CLIP_SHA256 = "([0-9a-f]{64})"', resources, re.M).group(1)
    filename = re.search(r'/ CLIP_REVISION / "([^"/]+)"', resources).group(1)
    model_name, pretrained = re.search(
        r'^MODEL_NAME, PRETRAINED = "([^"]+)", "([^"]+)"', embed, re.M).groups()
    text = NOTICES.read_text()
    section = text.split("## Model weights", 1)[1].split("\n## ", 1)[0]
    for label, value in (("repository", repo_id), ("revision", revision), ("SHA-256", digest),
                         ("weights file", filename), ("model name", model_name),
                         ("pretrained tag", pretrained)):
        assert value in section, f"notices' Model weights section does not name the loaded CLIP {label} {value}"
    assert f"https://huggingface.co/{repo_id}" in section, "notices must link the model card"
    assert f"https://huggingface.co/{repo_id}/blob/{revision}/" in section, "notices must link the card at the pinned revision"
    assert "MIT" in section and "checked 2026-09-27" in section
    assert "out of scope" in section.lower(), "the model card's deployed-use caveat must be recorded"
