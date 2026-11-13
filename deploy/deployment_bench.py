#!/usr/bin/env python3
"""Camera-free local JPEG-meter deployment measurements; no hardware or model downloads."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import resource
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def benchmark(iterations=5):
    if not 1 <= iterations <= 50:
        raise ValueError("iterations must be between 1 and 50")
    from PIL import Image
    from sonyremote.autoexpose import measure_jpeg
    samples = []
    with tempfile.TemporaryDirectory(prefix="camera-meter-bench-") as temporary:
        path = Path(temporary) / "synthetic-24mp.jpg"
        fixture = Image.new("RGB", (6000, 4000), (112, 112, 112))
        fixture.save(path, quality=95)
        fixture.close()
        for _ in range(iterations):
            start = time.perf_counter()
            result = measure_jpeg(path)
            samples.append((time.perf_counter() - start) * 1000)
        assert abs(result["mean"] - 112) <= 1
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = memory if platform.system() == "Darwin" else memory * 1024
    versions = {}
    for name in ["pillow", "gphoto2", "numpy", "fastapi", "uvicorn"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"scope": "synthetic 24MP JPEG decode and brightness meter only",
            "camera_accessed": False, "physical_portability_verified": False,
            "system": platform.system(), "machine": platform.machine(),
            "python": platform.python_version(), "packages": versions,
            "iterations": iterations, "latency_ms": samples,
            "median_ms": statistics.median(samples), "peak_process_rss_bytes": rss_bytes,
            "measurement_mean": result["mean"],
            "excluded": ["capture latency", "taste/model residency", "USB recovery",
                         "power", "thermal soak", "battery endurance", "phone pairing"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = benchmark(args.iterations)
    content = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end="")
