#!/usr/bin/env python3
"""Create a lossless mapped taste corpus locally; never download or use a camera."""
import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from compact_taste_index import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPO / "data/taste_index.npz")
    parser.add_argument("--destination", type=Path, default=REPO / "data/taste_index.compact")
    parser.add_argument("--source-manifest", type=Path, default=REPO / "data/taste_index.MANIFEST.json")
    parser.add_argument("--manifest-copy", type=Path)
    args = parser.parse_args()
    original = json.loads(args.source_manifest.read_text())
    manifest = prepare(args.source, args.destination, original["sha256"])
    if manifest["rows"] != original["photos_embedded"]:
        raise ValueError("converted corpus count differs from source manifest")
    if args.manifest_copy:
        args.manifest_copy.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"destination": str(args.destination), "rows": manifest["rows"],
                      "dimensions": manifest["dimensions"], "quantization": "none"}))


if __name__ == "__main__":
    main()
