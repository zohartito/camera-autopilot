"""Read-only resource receipts. Status never downloads models or contacts a camera.

File digests are cached by stat identity, so a status poll cannot repeatedly read
the 1.3 GB artifact pair. A cache hit is invalidated by replacement or modification.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
CLIP_REPO = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
CLIP_REVISION = "1a25a446712ba5ee05982a381eed697ef9b435cf"
CLIP_SHA256 = "1bd3c7172de5b207ceac554f5ab5266166f3b9baccc9af5989bc801016d080ad"
CLIP_BYTES = 605219813
_DIGESTS: dict[tuple, str] = {}


def clip_weights_path() -> Path:
    default = REPO / "data/model-cache/huggingface" / ("models--" + CLIP_REPO.replace("/", "--")) / "snapshots" / CLIP_REVISION / "open_clip_pytorch_model.bin"
    return Path(os.environ.get("CAMERA_AUTOPILOT_CLIP_WEIGHTS", str(default))).expanduser()


def _file_receipt(path: Path, expected_bytes: int, expected_sha256: str) -> dict:
    try:
        stat = path.stat()
        if not path.is_file():
            return {"state": "missing"}
        if stat.st_size != expected_bytes:
            return {"state": "size_mismatch", "bytes": stat.st_size}
        identity = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        digest = _DIGESTS.get(identity)
        if digest is None:
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            # Keep bounded even when an operator repeatedly replaces resources.
            if len(_DIGESTS) >= 8:
                _DIGESTS.clear()
            _DIGESTS[identity] = digest
        return {"state": "verified" if digest == expected_sha256 else "hash_mismatch", "bytes": stat.st_size, "sha256": digest}
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError:
        return {"state": "unreadable"}


def verified_clip_weights() -> Path:
    path = clip_weights_path()
    receipt = _file_receipt(path, CLIP_BYTES, CLIP_SHA256)
    if receipt["state"] != "verified":
        raise RuntimeError(f"CLIP weights {receipt['state']}; run tools/prepare_taste_runtime.py before capture.")
    return path


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def report() -> dict:
    """Resource availability, distinct from whether this shot used the resource."""
    try:
        manifest = json.loads((REPO / "data/taste_index.MANIFEST.json").read_text())
        taste = _file_receipt(REPO / "data/taste_index.npz", manifest["bytes"], manifest["sha256"])
        taste["rows"] = manifest["photos_embedded"] if taste["state"] == "verified" else None
    except (OSError, ValueError, KeyError, TypeError):
        taste = {"state": "manifest_unavailable", "rows": None}
    clip = _file_receipt(clip_weights_path(), CLIP_BYTES, CLIP_SHA256)
    clip["dependencies_available"] = _module_available("torch") and _module_available("open_clip")
    module = sys.modules.get("embed_index")
    clip["loaded"] = getattr(module, "_MODEL_CACHE", None) is not None
    taste["loaded"] = getattr(module, "_INDEX_CACHE", None) is not None
    taste["storage"] = getattr(module, "_INDEX_STORAGE", "not_loaded")
    taste["compact_fallback_reason"] = getattr(module, "_INDEX_COMPACT_ERROR", None)
    compact = Path(os.environ.get("CAMERA_AUTOPILOT_TASTE_COMPACT", str(REPO / "data/taste_index.compact")))
    taste["compact_available"] = compact.is_dir()
    # Compact-only portable packages remain valid when the pinned source identity
    # is retained in the small source manifest; no 697 MB NPZ copy is required.
    if taste["state"] == "missing" and compact.is_dir():
        try:
            from compact_taste_index import load
            arrays = load(compact, manifest["sha256"])
            taste.update(state="verified", rows=len(arrays["embeddings"]), available_storage="lossless_mmap")
            for array in arrays.values():
                array._mmap.close()
        except (OSError, ValueError, KeyError, TypeError):
            taste["compact_fallback_reason"] = "compact_validation_failed"
    ready = taste["state"] == "verified" and clip["state"] == "verified" and clip["dependencies_available"]
    vision = sys.modules.get("vision_banana")
    vision_status = vision.runtime_status() if vision is not None else {"state": "not_attempted", "fallback": "physics_only"}
    return {"taste": taste, "clip": clip, "taste_ready": ready, "fallback": None if ready else "physics_only", "vision": vision_status, "intent_profile": os.environ.get("CAMERA_INTENT_PROFILE", "full"), "intent_model_requested": os.environ.get("CAMERA_INTENT_PROFILE", "full") != "compact", "essential_intent_model_required": False}
