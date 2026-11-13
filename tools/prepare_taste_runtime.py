#!/usr/bin/env python3
"""Provision the pinned CLIP encoder explicitly; no camera or server operations."""
from pathlib import Path
import argparse
import os
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import runtime_resources as resources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Verify existing pinned weights; never download")
    parser.add_argument("--compact", action="store_true", help="Build lossless mapped corpus from the existing NPZ")
    args = parser.parse_args()
    if args.offline:
        print(f"Verified pinned CLIP encoder: {resources.verified_clip_weights()}")
        if args.compact:
            import json
            from compact_taste_index import prepare
            manifest = json.loads((REPO / "data/taste_index.MANIFEST.json").read_text())
            result = prepare(REPO / "data/taste_index.npz", REPO / "data/taste_index.compact", manifest["sha256"])
            print(f"Built lossless mapped corpus: {result['rows']} rows")
        return
    if args.compact:
        parser.error("--compact requires --offline; corpus preparation never downloads")
    # Do not inherit a broken global cache or write outside this project's cache.
    cache = REPO / "data/model-cache/huggingface"
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import hf_hub_download
    downloaded = Path(hf_hub_download(resources.CLIP_REPO, "open_clip_pytorch_model.bin", revision=resources.CLIP_REVISION, cache_dir=cache))
    os.environ["CAMERA_AUTOPILOT_CLIP_WEIGHTS"] = str(downloaded)
    resources.verified_clip_weights()
    print(f"Verified pinned CLIP encoder: {downloaded}")


if __name__ == "__main__":
    main()
