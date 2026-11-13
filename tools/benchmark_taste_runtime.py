#!/usr/bin/env python3
"""Compare real existing-JPEG retrieval in isolated legacy and mapped processes.

No downloads, network or camera operations. macOS/Linux peak process RSS is not
power consumption or target-board performance. The legacy branch reproduces the
former full matrix score/argsort for an honest baseline.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]


def child(image, mode):
    os.environ["CAMERA_AUTOPILOT_TASTE_STORAGE"] = "legacy" if mode == "legacy" else "auto"
    sys.path.insert(0, str(REPO / "backend"))
    import numpy as np
    import compact_taste_index
    import embed_index
    original = compact_taste_index.top_k
    calls = []
    def trace(vectors, query, k):
        if mode == "legacy":
            scores = vectors @ query
            ids = np.argsort(-scores)[:k]
            selected = scores[ids]
        else:
            ids, selected = original(vectors, query, k)
        calls.append({"ids": ids.tolist(), "scores": selected.tolist(),
                      "query_sha256": hashlib.sha256(query.tobytes()).hexdigest()})
        return ids, selected
    compact_taste_index.top_k = trace
    elapsed = []
    summaries = []
    for _ in range(2):
        start = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            summaries.append(embed_index.recommend(str(image)))
        elapsed.append(time.perf_counter() - start)
    print(json.dumps({"mode": mode, "storage": embed_index._INDEX_STORAGE,
                      "rows": len(embed_index._INDEX_CACHE["embeddings"]),
                      "cold_seconds": elapsed[0], "warm_seconds": elapsed[1],
                      "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                      "device": embed_index._MODEL_CACHE[2], "summaries": summaries, "neighbors": calls}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", choices=("legacy", "mmap"))
    args = parser.parse_args()
    if args.child:
        child(args.image, args.child)
        return
    if not args.output:
        parser.error("--output is required")
    results = []
    for mode in ("legacy", "mmap"):
        completed = subprocess.run([sys.executable, __file__, "--image", str(args.image.resolve()), "--child", mode],
                                   check=True, capture_output=True, text=True, timeout=180)
        results.append(json.loads(completed.stdout))
    old, new = results
    receipt = {"image": str(args.image.resolve()), "image_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
               "platform": sys.platform, "results": results,
               "top15_identical": old["neighbors"][0]["ids"] == new["neighbors"][0]["ids"],
               "query_identical": old["neighbors"][0]["query_sha256"] == new["neighbors"][0]["query_sha256"],
               "recommendation_identical": old["summaries"] == new["summaries"],
               "peak_rss_reduction_bytes": old["peak_rss_bytes"] - new["peak_rss_bytes"],
               "limits": "Two existing-photo lookups per isolated process; no target-board, camera, power or multi-scene quality claim."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key != "results"}, indent=2))
    if not all(receipt[key] for key in ("top15_identical", "query_identical", "recommendation_identical")):
        raise SystemExit("Saved-image equivalence failed; inspect the written receipt")


if __name__ == "__main__":
    main()
