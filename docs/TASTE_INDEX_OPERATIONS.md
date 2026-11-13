# Taste Index Operations

## Release status

The v1 Wikimedia Quality Images taste index is complete and suitable for the
current Mac-hosted retrieval path.

| Contract | Verified value |
|---|---|
| Rows | 233,785 |
| Embedding shape | 233,785 x 512 |
| Embedding dtype | float32 |
| Artifact bytes | 697,615,942 |
| SHA-256 | `949d4bf79d42345df73a5c4eee886a4b4a6383a06e26b9abd20bb0d7a9531128` |
| Harvest yield | 233,785 / 233,788 (99.998717%) |
| Duplicate or empty titles | 0 |
| Non-finite values | 0 |
| Embedding norm range | 0.9999998212 to 1.0000001192 |

The blob is gitignored. The copy validated on 2026-07-27 was:

`<local-source-repository>/data/taste_index.npz`

The authoritative contract is
`data/taste_index.MANIFEST.json`. A mirrored artifact may live elsewhere as
long as its byte count, SHA-256, and array contract match that manifest.

## Validate a release or transfer

Fast identity check without loading arrays:

```bash
python3 backend/check_taste_index.py \
  /path/to/taste_index.npz \
  --manifest data/taste_index.MANIFEST.json \
  --manifest-only
```

Full semantic validation plus the current retrieval benchmark:

```bash
python3 backend/check_taste_index.py \
  /path/to/taste_index.npz \
  --manifest data/taste_index.MANIFEST.json \
  --benchmark \
  --output /tmp/taste-index-audit.json
```

The command exits nonzero and emits a stable error code for missing/corrupt
artifacts, manifest mismatches, missing arrays, wrong shapes or dtypes,
misaligned rows, non-finite values, non-unit embeddings, impossible settings,
empty titles, or duplicate titles. `--json` makes stdout machine-readable.

`--no-manifest` is only for auditing a newly built artifact before a new
release manifest has been intentionally written. `--skip-sha256` is unsuitable
for release or transfer verification.

## Known quality warning

One source row reports `focal_mm=0`, the conventional legacy EXIF value for
unknown:

`File:Detail facade Engel Apotheke - Vienna.jpg`

The validator reports `focal_mm_unknown` but passes the artifact. This is one
row out of 233,785 and does not indicate corruption. Other focal values must
remain within the hard numeric bounds.

## Measured performance

Measurements below are from the 2026-07-27 Mac audit and include a warm OS file
cache. They are operational baselines, not universal hardware guarantees.

| Operation | Result |
|---|---|
| Full manifest + semantic audit + benchmark | 0.836 s |
| Full validator peak process footprint | about 1.38 GB |
| Materialize all runtime arrays, cold | 113.5 ms |
| Reuse materialized runtime cache | under 0.001 ms |
| Resident array payload | 697,614,440 bytes |
| Runtime cache measurement peak process footprint | about 1.00 GB |
| 233,785-row cosine lookup + full sort, k=15 | 16.937 ms warm median |

`embed_index._get_index()` now materializes every NPZ member once. Caching the
`NpzFile` handle alone was not a real warm cache because each key access reread
that member. Checkpoints are also written to a temporary file, flushed, and
atomically replaced so interruption cannot destroy the previous good index.

The v1 artifact is production-ready for the current Mac/backend route with
reasonable memory headroom. It is not yet a proven fit for a roughly 1 GB
embedded puck. A memory-mapped or split-array release format should be evaluated
only when that hardware target becomes active; rewriting v1 was deliberately
out of scope for this closure.

## Unattended build runner

`backend/pc_overnight.sh` defaults to the optional broad-Commons expansion lane.
It supports:

- `HARVEST_SOURCE=broad` or `HARVEST_SOURCE=qi`
- `HARVEST_PAGES`, `CATCHUP_SLEEP_SECONDS`, and `MAX_EMBED_FAILURES`
- a preflight-free `PIPELINE_DRY_RUN=1`
- repeated-embed failure limits
- harvester exit-code propagation
- cleanup on interruption
- a final semantic index audit
- optional JSON logging through `PIPELINE_AUDIT_REPORT=/path/report.json`

From Windows OpenSSH into WSL, use the `systemd-run --user` command documented
at the top of the script. A plain detached child of `wsl.exe` is not reliable.

## Optional corpus work

No second source is required to ship the current taste-retrieval loop:

- Broad Wikimedia Commons remains the preferred free, commercial-safe expansion
  if measured product quality shows the 233,785-row corpus is insufficient.
- Flickr harvesting is implemented but parked because new API access is paid.
- Rebuilding toward 450k is an experiment, not a completion blocker.
- Any new corpus release needs a new manifest, full audit, held-out retrieval
  comparison, and deliberate distribution of the new gitignored blob.
