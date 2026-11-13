# Portable runtime package contract

The essential profile uses `CAMERA_INTENT_PROFILE=compact`: deterministic supported phrases and explicit recipes execute without any text-model request. It preserves metering, exposure constraints, capture recovery and JPEG sessions. This does not claim arbitrary-language parity with the 14B model. The existing full profile stays the default on the Mac; vision refinement is an optional separate workload.

`requirements-base-candidate.lock` pins the currently tested Mac base versions as a **candidate constraint set**, not a verified Linux environment. Resolve and install in a new Linux virtualenv, verify target wheel availability and system libgphoto2/USB permissions, and run the camera-free aggregate plus actual target capture tests before shipping. Do not copy a Mac virtualenv. No Linux environment was installed or tested by this artifact. CLIP retrieval additionally requires the separately provisioned torch/open_clip environment and pinned encoder; no model download occurs during capture.

For the complete taste corpus, run `.venv/bin/python tools/prepare_compact_taste.py --manifest-copy data/taste_index.compact.MANIFEST.json`. It verifies the original archive SHA-256, preserves every original NPY byte and all 233,785 rows, validates dimensions and finite numeric values, and creates an immutable directory. It never overwrites an existing compact artifact. All six arrays are read-only memory maps; titles and numeric metadata are loaded only at selected rows. Chunked exact float32 cosine search scores the whole corpus and retains only bounded candidates. No quantization, training or corpus reduction is used.

Ship `data/taste_index.compact/`, its manifest, and the small canonical `data/taste_index.MANIFEST.json`; the original 697 MB NPZ is optional for a compact-only package. The runtime validates each member hash and source identity before use. Corrupt compact data falls back to the original NPZ when available, with an explicit health reason; missing usable data leaves the existing physics fallback. Set `CAMERA_AUTOPILOT_TASTE_STORAGE=legacy` to compare the old resident loader. The generated data directory is ignored by Git; only the manifest and preparation code are tracked.

The package manifest uses schema version 1, format `lossless-float32-npy-mmap`, `rows`, `dimensions`, `quantization: none`, `source: {bytes, sha256}`, and six constant `files` names. Each file carries `bytes`, `sha256`, `shape`, and `dtype`. A portable validator must inspect these locally; a manifest alone is not an installed artifact or successful inference. The CLIP encoder identity is defined in `backend/runtime_resources.py`.

Clean mapped pages may be released through `madvise` on supporting systems after each 4,096-row block. This bounds scratch allocation but does not cap total process memory: model weights, framework buffers, image decoding, and OS page-cache behavior remain part of measurements. Results on a Mac do not establish Pi/Jetson power, heat, battery endurance, throughput or board suitability.

## Measured Mac result (2026-09-05)

On the existing 6,000 × 4,000 A7 III JPEG `shot_1788646669777065000.jpg`, isolated ML Python processes compared the actual `recommend()` path against the former resident NPZ/full-score/argsort implementation. Both used the existing pinned CLIP encoder on MPS, all 233,785 rows, and two lookups. The query embedding SHA-256, all 15 nearest-neighbor IDs in order, and the returned settings were identical: f/6.7, ISO 100, 24 mm. This is a saved interior-scene equivalence test, not a general photographic-quality evaluation.

| Measurement | Original resident NPZ | Lossless mapped corpus |
| --- | ---: | ---: |
| Peak process RSS | 2,281,259,008 bytes | 1,583,955,968 bytes |
| Cold first recommendation (after Python imports) | 1.840 s | 2.198 s |
| Warm recommendation | 0.149 s | 0.326 s |

Peak RSS fell by 697,303,040 bytes (30.6%); latency increased. The mapped result is approximately 1.584 GB, so this **does not establish a sub-1-GB product**. The encoder/framework remains substantial. Full evidence is local at `artifacts/falsification/implementation-wave-20260905/runtime/actual-jpeg-comparison.json`; reproduce with `backend/.venv-ml/bin/python tools/benchmark_taste_runtime.py --image <existing-jpeg> --output <receipt.json>`. Compact members consume approximately the same disk bytes as the original lossless arrays; this change optimizes resident memory rather than disk size. The original NPZ remains intact.

Twenty-two focused tests passed in both installed Python runtimes: exact all-array conversion, nearest-neighbor and tie behavior, corrupt/writable-artifact refusal, source mismatch, metadata consistency, full-corpus count, legacy fallback with visible reason, compact-only package loading/health, no-model compact intent, and retained full-profile model capability. No model was downloaded, network contacted, or camera operated for this runtime lane.

## Split runtime and per-role Linux locks (2026-09-28)

Design: `docs/RUNTIME_SPLIT_2026-09-28.md`. On the puck, `CAMERA_AUTOPILOT_RUNTIME=split` in the shared
`runtime.env` makes `run.sh` start the camera-control role (`backend/role_control.py`), which refuses
torch/open_clip in-process and queues merges and timelapse encodes. Two more services do the rest:
`backend/role_perception.py` (CLIP over the Unix socket named by `CAMERA_AUTOPILOT_PERCEPTION_SOCKET`, e.g.
`/run/user/<uid>/camera-autopilot/perception.sock`) and `backend/role_jobs.py` (the heavy-jobs worker).
Render their units with `python3 deploy/provision.py --role perception|jobs --output <file>`; `--install`
installs and never starts, as for control. Without the variable the Mac product is unchanged.

Locks target Raspberry Pi OS Bookworm: CPython 3.11, glibc 2.36, aarch64. Regenerate with
`deploy/locks/compile.sh`, which runs, per role:

```
uv pip compile requirements-<role>.in -c mac-tested.constraints.txt \
  --python-platform aarch64-manylinux_2_36 --python-version 3.11 \
  --exclude-newer 2026-09-27T00:00:00Z --only-binary :all: [--torch-backend cpu] \
  -o requirements-<role>.linux-aarch64.lock
```

`--only-binary :all:` fails the resolution if any pin would build from source; `deploy/locks/check_wheels.py`
re-checks each pin against the PyPI JSON API and the PyTorch CPU index. On 2026-09-27 every pin had a
compatible wheel: control 25/25, perception 37/37, jobs 3/3 (for example `torch-2.14.0+cpu-cp311-cp311-manylinux_2_28_aarch64`,
`opencv_python_headless-5.0.0.93-cp37-abi3-manylinux2014_aarch64`, `rawpy-0.27.1-cp311-cp311-manylinux_2_28_aarch64`).
Nothing builds from source. Differences from the Mac-tested set: numpy is 2.4.6 (2.5.x requires Python 3.12),
OpenCV is the headless build of the same 5.0.0.93, and perception uses the CPU torch build (PyPI's aarch64
torch pulls CUDA wheels).

Board venvs, one per role (run from the repo root on the board):

```
python3 -m venv backend/.venv            && backend/.venv/bin/pip install -r deploy/locks/requirements-control.linux-aarch64.lock
python3 -m venv backend/.venv-jobs       && backend/.venv-jobs/bin/pip install -r deploy/locks/requirements-jobs.linux-aarch64.lock
python3 -m venv backend/.venv-perception && backend/.venv-perception/bin/pip install \
  --extra-index-url https://download.pytorch.org/whl/cpu -r deploy/locks/requirements-perception.linux-aarch64.lock
```

`rawpy` is in the control lock because RAW develop runs as a bounded child of control. `mcp` and `astral` are not:
only `server.py` (the MCP server) uses them, and it opens the camera itself, so it must not run on the puck.

Then the camera-free suite on the board, against the control venv plus the test-only packages (the MCP tests
import `mcp`):
`backend/.venv/bin/pip install pytest==9.1.1 httpx==0.28.1 PyYAML==6.0.3 'mcp[cli]==2.2.0' astral==3.2 && CAMERA_ENGINE=mock backend/.venv/bin/python -m pytest -q backend tests`.
Before starting the worker units, `python3 deploy/provision.py --role jobs --output /tmp/unit` must report no
memory-cgroup blocker (see `docs/RUNTIME_SPLIT_2026-09-28.md`).
No Linux container or board has run these locks yet; that is the first 1L receipt.
