# docs

## Receipts (evidence from real cameras)

- [`RECEIPTS_2026-09-29.md`](RECEIPTS_2026-09-29.md) — A7 III and A7 II, afternoon to evening, plus iPhone tests.
- [`RECEIPTS_2026-10-01.md`](RECEIPTS_2026-10-01.md) — re-takes of the 29 September failures, dusk timelapse, iPhone pairing.
- [`RECEIPTS_2026-09-27.md`](RECEIPTS_2026-09-27.md), [`RECEIPTS_2026-09-22.md`](RECEIPTS_2026-09-22.md) — earlier sittings and the original receipt index.
- [`HUMAN_GATED_RECEIPTS_2026-09-07.md`](HUMAN_GATED_RECEIPTS_2026-09-07.md) — the copy-paste commands that produce a receipt.

How to read them:

- Each receipt lists the files it produced with their byte size and SHA-256. **The photos, videos
  and JSON responses themselves are not in this repository**: they were taken in a private home
  and stay on the author's machine under the gitignored `artifacts/` folder.
  `scripts/check_ledger_receipts.py` re-hashes them there; it cannot run from a public clone.
- Camera serial numbers, LAN addresses and similar identifiers were replaced with placeholders
  such as `A7III0SERIAL`, `A7II00SERIAL` and `<mac-lan-ip>`. Nothing else in the results was edited:
  failures are recorded as failures.
- The receipts link to internal planning documents (`SEQUENTIAL_AGENT_STACK_*`, `PROJECT_LEDGER_*`,
  `STAGE_WAVE_*` and others) and cite pull-request numbers (`#106`, `#118`, ...) from the private
  development repository. Those documents and that history are not published, so those links and
  numbers do not resolve here.

## Technical notes

- [`ROUTE_MATRIX_2026-09-15.md`](ROUTE_MATRIX_2026-09-15.md) — which camera routes (USB, Wi-Fi, focus, preview) are proven on which body.
- [`REVERSE_ENGINEERING.md`](REVERSE_ENGINEERING.md) — what the A7 II exposes over PTP and which limits are real.
- [`SECURITY_PRIVACY_RELEASE.md`](SECURITY_PRIVACY_RELEASE.md), [`USB_PTP_BRIDGE_SECURITY.md`](USB_PTP_BRIDGE_SECURITY.md) — token, network exposure and release gates.
- [`RUNTIME_SPLIT_2026-09-28.md`](RUNTIME_SPLIT_2026-09-28.md) — splitting control, perception and heavy jobs for a small Linux board.
- [`WAVE0_TIMELAPSE_2026-09-22.md`](WAVE0_TIMELAPSE_2026-09-22.md) — the Holy Grail timelapse control law (simulator evidence only).
- [`PANORAMA_IMPLEMENTATION_2026-09-05.md`](PANORAMA_IMPLEMENTATION_2026-09-05.md), [`LINEAR_DNG_EXPORT_2026-09-05.md`](LINEAR_DNG_EXPORT_2026-09-05.md) — offline panorama and DNG export.
- [`TASTE_SOURCES.md`](TASTE_SOURCES.md), [`TASTE_INDEX_OPERATIONS.md`](TASTE_INDEX_OPERATIONS.md) — the optional settings-retrieval index.
- [`IOS_BUILD.md`](IOS_BUILD.md) — the Capacitor iOS wrapper.
- [`LESSONS.md`](LESSONS.md) — mistakes and the tests that now guard against them.
