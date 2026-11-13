# Runtime split on the puck: control, perception, heavy jobs (2026-09-28)

Ledger rows X-SPLIT and the runtime half of X-COMPACT; plan step 0.6. Target: Raspberry Pi CM5, 8 GB,
Raspberry Pi OS Bookworm (Debian 12, system **Python 3.11**), arm64. The Mac keeps the single
process: nothing below changes `./run.sh` unless `CAMERA_AUTOPILOT_RUNTIME=split` is set.

## Three roles

| Role | Entry point | Owns | Must never |
|---|---|---|---|
| **Camera control** | `run.sh` → `backend/role_control.py` (uvicorn `app:app`) | USB, the gphoto2 handle, exposure/focus, shutter journal, session capture, metering, `remeter`, `focus_verify`, deterministic intent (`CAMERA_INTENT_PROFILE=compact`), gallery publication, the API | import torch/open_clip (blocked by an import guard), run a stack merge, a timelapse encode or RAW develop in-process |
| **Compact perception** | `backend/role_perception.py` | CLIP encoder: taste retrieval (`embed_index.recommend`) and the subject class floor (`subject_recognition._clip_classify`) over the mapped compact index | touch the camera or the gallery |
| **Heavy jobs** | `backend/role_jobs.py` | HDR/long-exposure/night/crowd merges, panorama stitch, timelapse ZIP + H.264, all from hash-verified originals already on disk | touch the camera or publish to the gallery |

## How they talk (nothing new except one socket)

- **Control → heavy jobs: the session directory.** With the split on, `product_sessions.run` captures,
  restores the body and marks the session `completed` without a merger (the existing contract: "without a
  merger, completion means a captured sequence"), then writes `heavy_job: {status: queued, inputs: [sha256…]}`
  into the session manifest. The worker scans the store, takes the session's existing cross-process
  `operation` flock, re-verifies every input hash, writes `result.jpg` (or `sequence.zip` + `timelapse.mp4`)
  and the `output` receipt, and marks the job `completed`. A side effect: the camera is restored right after
  the last frame instead of waiting for the merge.
- **Heavy jobs → control: the manifest again.** Control's publisher (an asyncio task beside the retention
  sweep) publishes completed jobs with the existing idempotent `_publish`. Publication stays in control because
  the gallery lifecycle lock is in-process only. The sweep scans without that lock and takes it non-blocking
  per session, only while publishing, so a shutter press never waits behind the scan. A timelapse ZIP is
  re-hashed before the lock is taken; under it remain only the small frame hash, the gallery copy and enrolment. The UI shows
  "captured, processing queued/running/failed" and offers "Retry processing (no recapture)".
- **Control → perception: one Unix socket** (`CAMERA_AUTOPILOT_PERCEPTION_SOCKET`, mode 0600 in a 0700 dir),
  one JSON line per request carrying a file path. This is the one new channel: the answer is needed before the
  final shutter, so a polled file queue would add latency, and loopback TCP is open to every local user. Any
  failure raises in control, which is exactly today's "CLIP unavailable" path: `subject_evidence` records
  `unavailable`, `recommend_live` and `recommend_settings` record "Taste unavailable". Nothing pretends to be
  the CLIP answer. One deadline (`CAMERA_AUTOPILOT_PERCEPTION_TIMEOUT_S`, 20 s) covers both CLIP calls of a
  shot, and after one fails the other fails at once. The service binds its socket only after CLIP has loaded,
  so a warming or restarting service refuses connections instead of consuming the deadline. A warm-up stuck past
  300 s (`CAMERA_AUTOPILOT_PERCEPTION_WARM_S`) exits 70 for a restart. A class name this control build does not
  know is treated as "unavailable", never as a floor.

## When one dies

| Dies | Effect | Recovery |
|---|---|---|
| Perception (crash, OOM) | Next shots degrade to labelled physics (`subject.status=unavailable`, taste note) | systemd restarts it; no camera state involved |
| Perception (hung) | The shot's one deadline expires and it takes the same labelled path | an inference past 60 s (`CAMERA_AUTOPILOT_PERCEPTION_HANG_S`) answers an error and exits 70; systemd restarts it |
| Heavy jobs (crash, OOM kill mid-merge) | `heavy_job.status` stays `running`, the flock is released by the kernel | next scan re-runs from the preserved originals (pure recompute); after 3 starts it is `failed` with the reason; the UI's retry (`POST /sessions/{id}/jobs`) re-queues without the camera |
| Heavy jobs (timelapse encode fails) | The job re-queues itself | after 3 attempts the JPEG sequence is published with the movie recorded as failed, as the single process does |
| Heavy jobs (hung merge) | One session's lock is held | a job past 3600 s (`CAMERA_AUTOPILOT_JOB_MAX_S`) exits the worker; the job is recomputed as after a crash |
| Heavy jobs (clean stop) | `KillMode=mixed`: only the worker gets SIGTERM; ffmpeg and the stitch child are not signalled | the worker finishes the current job and stops between jobs; past `TimeoutStopSec=120` the job is killed and recomputed |
| Control | Capture stops; sessions follow today's recovery rules (no shutter replay) | queued jobs still complete; publication resumes when control returns. Control starts even while the worker holds a session lock: a stale capture receipt is recovered once the lock is free |

Neither worker unit has a start limit (`StartLimitIntervalSec=0`, `RestartSec=10`): poison jobs are bounded by
the 3 attempts, and a stranded queue is worse. Control's `/device-health` reports both workers' heartbeats
under `runtime`.

## Memory on an 8 GB CM5 (cgroup budgets in the unit templates)

| Role | Budget | Basis |
|---|---|---|
| OS, headless | ~0.6 GB | estimate, not board-measured |
| Control | ~0.3–0.5 GB, plus its children (no cap set) | estimate: service + gphoto2 + one 24 MP decode in metering. The panorama preview child (up to 1 GiB, watchdogged), preview ffmpeg and the RAW develop child also run in control's cgroup |
| Perception | `MemoryMax=2560M` | measured 1.58 GB peak on the Mac (MPS) with the mapped index; CPU torch on arm64 unmeasured. The legacy NPZ loader (2.28 GB) exceeds `MemoryHigh=2G`: ship the compact index |
| Heavy jobs | `MemoryMax=2G` | merge working set measured 0.36–0.39 GB (Mac); panorama child and ffmpeg share this cgroup |
| Page cache headroom | ~2.4 GB | the 665 MB mapped index and 24 MP frames |

Both workers carry `OOMScoreAdjust=500`, so under whole-system pressure the kernel takes them before control.
Raspberry Pi kernels have shipped with the memory cgroup disabled, which would make `MemoryMax` a no-op. So
`provision.py --role perception|jobs` reports a blocker unless cgroup v2 `memory` is enabled and delegated to the
user manager, and both workers exit 78 at start when started by their unit (`Environment=CAMERA_AUTOPILOT_ENFORCE_MEMORY_CAP=1`
in the templates; an inherited `INVOCATION_ID` does not count) and their `memory.max` is not enforced
(`CAMERA_AUTOPILOT_ALLOW_UNCAPPED=1` overrides it, loudly). The units do not restart on 78.

## Module map

- Control: `app.py`, `engine.py`, `sonyremote/`, `product_sessions.py` (capture half), `stack_session.py`
  (runner, receipts), `session_jobs.py`, `remeter.py`, `focus_verify.py`, `capture_analysis.py`, `intent*.py`,
  `brain.py`, `photo_lifecycle.py`, `timelapse_controls.py`, `panorama.py` (supervisor only; stitching runs in its child).
- Perception: `embed_index.py`, `compact_taste_index.py`, `subject_recognition.py` (CLIP half),
  `runtime_resources.py`.
- Heavy jobs: `heavy_jobs.py`, `stack_session.merge_session_frames`, `stacking.py`, `crowd_auto_mask.py`,
  `crowd_recovery.py`, `deflicker.py`, `timelapse_video.py`, `product_sessions._sequence/_video`.
  RAW develop (`isolated_development.py`/`isolated_raw.py`) stays a bounded child of control, so `rawpy` is
  in the control lock.

## Locks

One `.in` and one `linux-aarch64` lock per role under `deploy/locks/`, resolved with
`uv pip compile --python-platform aarch64-manylinux_2_36 --python-version 3.11 --only-binary :all:` and a fixed
`--exclude-newer` (`deploy/locks/compile.sh`). Board venvs: `backend/.venv` (control), `backend/.venv-perception`,
`backend/.venv-jobs`. The control lock has no `mcp` or `astral`: only `server.py` uses them, and it opens the
camera itself, so it must not run beside control on the puck.

## Still in control, deliberately

Incremental previews at frame boundaries (the panorama preview in its bounded child, the timelapse preview
via ffmpeg, and deflicker gains for that preview) stay in control for now: they are the phone's live view
and already run out of process or on a bounded sample. RAW develop likewise stays a bounded child of control.
Moving either to the heavy-jobs role is the next step if board measurements show them delaying capture.

## Not proven here

Board memory, latency, thermal and OOM behaviour; the systemd units on a real user manager; the perception
socket under CPU torch; any camera path. The Mac proves the import boundary, the queue/worker/publish cycle
with a fake camera, and the socket protocol with a fake model.
