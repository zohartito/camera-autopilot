# AGENTS.md — notes for people and coding agents working in this repository

This project was built largely by AI coding agents working from written specs, with a human
reviewing every change and testing on real cameras. These are the working rules they follow.

## How to verify a change

- Camera-free gate: `bash scripts/test_safe.sh` (pytest over `backend/` and `tests/`, physics
  self-checks, the MCP stdio contract, launcher and USB-bridge security scripts, the deterministic
  intent eval). It sets `CAMERA_ENGINE=mock` and points the Ollama/vision hosts at a dead port.
- Test environment: install `requirements-test.lock` into `.venv` (Python 3.14, also used by
  Linux CI); `requirements-test.txt` is the direct dependency list. The launcher tests
  expect `backend/.venv`; a symlink to `../.venv` is enough.
- Model skips are not camera or model verification. A passing mock run says nothing about a real
  body; real-camera claims live in `docs/RECEIPTS_*.md`.

## Runtime knobs

- `CAMERA_ENGINE`: `mock` (default, no hardware), `real` (Sony over USB via `sonyremote/`).
- `CAMERA_AUTOPILOT_SHOTS_DIR`: overrides the gallery root. Without it only `CAMERA_ENGINE=real`
  writes `backend/shots/`; the mock engine (tests, `run.sh` default, the iOS simulator smoke)
  writes the gitignored `artifacts/demo-shots/`. Mock records carry `engine: "mock"`.
- `OLLAMA_HOST` / `OLLAMA_MODEL`: optional local LLM for free-form intent. Without it the
  deterministic intent rules are used.
- `VISION_BANANA_HOST`: optional local vision critic for `auto_correct`. When it is unreachable
  the loop degrades (a luma-based critique, or a single shot) instead of failing.
- `CAMERA_AUTOPILOT_RUNTIME=split`: control / perception / heavy-job roles for a small Linux board
  (`docs/RUNTIME_SPLIT_2026-09-28.md`). Unset means one process.
- API token: without `CAMERA_AUTOPILOT_API_TOKEN`, `run.sh` reuses one per-user 0600 token
  (`backend/api_token_store.py`); `./run.sh --rotate-token` replaces it.

## Rules

- Identity is the USB serial, never the gphoto2 model label (a sleeping body can report the wrong
  model). No serial, no capture.
- Never commit photos, RAW files, video, `artifacts/`, `captures/`, tokens or device identifiers.
  `.gitignore` enforces most of this; CI runs gitleaks.
- Root `conftest.py` fails any test that leaves a product worker thread running.
- Real-data tests run only with `CAMERA_REAL_DATA_TESTS=1`.
