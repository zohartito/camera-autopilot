"""Offline portable-host readiness CLI; read-only and camera-free."""
from __future__ import annotations
import sys

sys.dont_write_bytecode = True  # read-only CLI: leave no __pycache__ behind either

import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

# Run either as a module (`python -m deploy.host_readiness`, repo root already on
# sys.path) or by path (`python deploy/host_readiness.py`, what a person on a
# fresh board types). The path form starts with `deploy/` on sys.path, not the
# repo root, so bootstrap the root before importing the backend package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.device_health import portable_readiness
from deploy.cm5_nano import MODEL_PATH, board_facts, read_model
from deploy.safe_paths import path_arg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=path_arg, required=True)
    parser.add_argument("--token-file", type=path_arg, required=True)
    parser.add_argument("--manifest", type=path_arg, required=True)
    parser.add_argument("--artifact-root", type=path_arg, required=True)
    parser.add_argument("--minimum-free-gib", type=int, default=2)
    parser.add_argument("--model-file", type=path_arg, default=MODEL_PATH,
                        help="device-tree model to classify (CM5 / Pi 5 fallback / other)")
    parser.add_argument("--carrier", choices=["auto", "cm5-nano-a", "other", "none"], default="auto",
                        help="carrier assumption; auto assumes the Waveshare CM5-NANO-A on a CM5")
    args = parser.parse_args(argv)
    result = portable_readiness(args.storage, args.token_file, args.manifest,
                                args.artifact_root, args.minimum_free_gib * 1024**3)
    # Board facts are informational: they never change the readiness verdict.
    result["board"] = board_facts(read_model(args.model_file), args.carrier)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
