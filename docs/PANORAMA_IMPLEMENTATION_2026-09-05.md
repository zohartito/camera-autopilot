# Offline panorama implementation — 2026-09-05

You can build a bounded panorama from **2–12 existing overlapping JPEGs**, generate a multi-row capture plan from focal length, sensor size and ≥30% overlap (or an explicit rows×columns grid), stitch that grid, select a projection (including rectilinear, cylindrical and spherical/equirectangular), level the horizon from long edges, auto-crop to the largest fully covered rectangle, then apply an optional manual crop. Processing writes a private JPEG, coverage mask, input hashes, alignment measurements and a durable receipt. Originals are preserved. It does not control a camera or trigger replacement captures.

Software tests of synthetic grids are not a real sweep. Physical multi-row capture remains Wave 3.

## Test it

Run from the repo root:

```bash
.venv/bin/python tools/build_panorama.py --plan 2 3
.venv/bin/python tools/build_panorama.py --focal-length 24 --sensor-width 35.8 --sensor-height 23.9 --yaw-span 120 --pitch-span 80 --overlap 0.35
.venv/bin/python tools/build_panorama.py /absolute/frame1.jpg /absolute/frame2.jpg --projection spherical --output-root artifacts/panoramas --layout 2,2 --crop 0.1,0.1,0.9,0.9
```

The command returns a job ID. Inspect `artifacts/panoramas/<job_id>/panorama.jpg`, `coverage.png`, and `receipt.json`. Failed jobs retain an error receipt and remove temporary source copies and any partial result. `--rotation` accepts -45…45 degrees and is added after automatic horizon leveling (disable with `--no-level-horizon`). `--no-autocrop` retains the coverage boundary. `--horizontal-fov` specifies a virtual field of view of 20…140 degrees, not EXIF lens calibration. `--crop` is a normalized `[left,top,right,bottom]` box of the auto-cropped (or full) result.

The Python entry point is `backend.panorama.build_panorama(sources, output_root, projection='spherical', horizontal_fov_deg=90, rotation_deg=0, autocrop=True, timeout_seconds=60, cancel_event=None, level_horizon=True, crop=None, layout=None)`. It returns a receipt, or raises `PanoramaError` with `reason`, `job_id`, and `retry_capture=False`. A calling API must restrict input paths to its owned gallery. `capture_plan(focal_length_mm=..., sensor_width_mm=..., sensor_height_mm=..., overlap=.35, yaw_span_deg=..., pitch_span_deg=...)` computes rows×columns and the pitch between rows from pinhole geometry. `guided_plan(rows, columns, overlap=.35, hfov_deg=60, vfov_deg=40)` remains the explicit-grid form. Both return serpentine yaw/pitch targets and a `session` block the UI can follow; they do not claim measured orientation or verified overlap.

## Implemented behavior

SIFT matches and RANSAC recover homographies across earlier accepted views, including successive rows. A `layout` of rows×columns (serpentine) requires at least one inter-row match; otherwise the job refuses with `insufficient_overlap`. Every accepted link requires at least 20 inliers, a majority inlier ratio, distributed matches, bounded convex geometry and estimated overlap of at least 8%. Failure to align any submitted frame rejects the whole job. Repeated framing adding less than 10% coverage is rejected. Feather weights blend the accepted frames. Conservative autocrop finds a wholly covered rectangle; an optional normalized manual crop is then applied and must stay on covered pixels. Horizon leveling estimates a 2D rotation from the dominant long near-vertical Hough bin when at least 65% of near-vertical length agrees and at least four agreeing lines lie 24 px apart on the working raster, otherwise from the dominant long near-horizontal bin (blurred ≤512 px raster, one OpenCV thread, fixed RNG); it is not IMU gravity and stays undetermined when those edges do not agree. Ceiling and floor perspective lines are not used when enough parallel verticals are available. Fewer agreeing verticals never level on their own; they make the result undetermined when a near-horizontal estimate disagrees by more than 1° and less than 90% of that family agrees, so neither a thin leaning pole nor a receding shelf line tilts the result. Edges on the coverage boundary are ignored for both families (main let them vote). Known open cases, pinned as strict xfail tests: an off-centre pitched façade (one-sided keystone fan, +2.25°), two leaning poles wider than 24 px on the working raster (+9°), and a correct 5° horizon at 89% agreement vetoed by one pole (left unleveled). The receipt records `output.pre_crop_width/height`, `output.post_crop_width/height` and `horizon.leveling_angle_deg` so Mac vs Linux geometry can be compared. A mask remains inspectable when autocrop is disabled.

Available projections:

| API name | OpenCV warper |
| --- | --- |
| `spherical` | `spherical` |
| `cylindrical` | `cylindrical` |
| `mercator` | `mercator` |
| `transverse_mercator` | `transverseMercator` |
| `compressed_rectilinear_1` | `compressedPlaneA2B1` |
| `compressed_rectilinear_2` | `compressedPlaneA1.5B1` |
| `panini_1` | `paniniA2B1` |
| `panini_2` | `paniniA1.5B1` |
| `rectilinear` | `plane` |

[Arsenal's official iOS guide](https://support.witharsenal.com/en/articles/6687991-arsenal-2-getting-started-with-panorama-ios) lists eight projection families and describes multi-row capture. Our compressed and Panini parameter choices are explicit above; Arsenal does not publish its exact parameters there, so matching labels do not establish identical rendering.

## Bounds and limits

- JPEG/MPO primary frame only, 48 MiB and 36 MP maximum per source. Sony JPEGs carrying MPO metadata are accepted; secondary thumbnails are not decoded as additional frames. Non-normalized EXIF rotation is explicitly rejected.
- Native decode, features, warps and encoding execute in a separate process. Maximum 12 frames, 1.5 MP working raster per frame, 8 MP output, 120-second configurable wall deadline, CPU/file limits, and a sampled 1 GiB RSS kill watchdog. Linux additionally applies a 3 GiB address-space limit. The macOS RSS watchdog is not a hard instantaneous allocation ceiling.
- Source snapshots are regular files, byte bounded and SHA checked; the receipt records original dimensions and working dimensions. Job directories are mode 0700 and artifacts 0600. No source copies remain after a finished or failed job.
- This is **planar homography stitching followed by virtual-camera projection**, not calibrated rotational camera reconstruction or a complete 360-degree sphere. It cannot recover parallax, missing texture, or moving-object identity. Feathering does not guarantee seam-free real photographs.
- Working-resolution reduction means full-resolution Arsenal panorama parity remains open. Native phone processing, live accumulation during a physical two-row camera sweep, and IMU-backed horizon/sweep guidance are not established by this software lane. Horizon leveling from image edges is unproven on real photographs.

## Verification

`tests/test_panorama.py` covers known two-row synthetic geometry, spherical/cylindrical marker locations against independent analytic ray geometry, every projection's output/coverage, Sony MPO primary JPEGs, insufficient overlap, repeated framing, deadline/cancel, duplicate-input and output-limit rejection, plus a textured-sphere multi-row grid, optics capture plans, horizon estimate, a rolled interior 2×2 grid that must level to within 0.5°, a level horizon with leaning poles (thin, wide or fragmented), a symmetric keystone fan, a receding line vetoed by two plumb edges and skewed coverage boundaries that must not tilt, and manual crop. Real tripod sweeps remain necessary photographic acceptance evidence.

Private evidence: `artifacts/falsification/implementation-wave-20260905/panorama/`.

- `pytest-python314.txt` and `pytest-python313.txt`: focused suite receipts.
- `synthetic-projection-summary.json`: nine independent supervised jobs on a textured four-view two-row synthetic scene. Each completed with a valid output and full coverage after crop; about 0.85–0.86 seconds and 172–187 MiB sampled peak RSS on this Mac. This small fixture is not a full-resolution performance benchmark.
- `jobs/95cdd15aa1e44a33b52d32d2797a9569/panorama.jpg`: visually inspected rectilinear result; the grid continues across both row and column overlaps without visible broken lines.
- `existing-camera-frames-result.json`: two pre-existing 6000×4000 Sony JPEGs were accepted as MPO primary images, but their views did not align reliably; the job correctly failed `insufficient_overlap` and published no panorama. They were not captured as a deliberate sweep. An earlier probe identified the Sony MPO container handling gap; it was fixed and covered by a regression test.

For feedback, supply the job ID and whether the problem is alignment, line shape, crop, projection or speed. The job receipt identifies the exact inputs and parameters. Real tripod sweeps with near/far subjects and low-texture sky remain necessary photographic acceptance evidence.

## Browser workflow

`backend/web/panorama.html` provides a mobile-width gallery selector for 2–12 JPEGs, selection-order adjustment, all nine projection choices, virtual field of view, manual horizon rotation and autocrop. It displays the saved result, processing receipt, download action and a direct Review feedback link. The multi-row form displays numbered serpentine targets. Authentication uses the existing session token header and `/client.js` media helper; tokens are never placed in image URLs.

The integration contract is `GET /panorama`, `POST /panoramas/plan`, and `POST /panoramas`. The page submits gallery basenames only and expects the published Shot result with `image` and `evidence`. Backend route/publication verification belongs to the parent integration lane.

A dropped response or HTTP 5xx leaves publication uncertain and disables resubmission across page reload. The user can inspect Review and explicitly enable another build; the page never silently resubmits an ambiguous job. A typed 4xx rejection displays the supplied explanation and job ID.

Playwright CLI isolated browser fixtures verified selection and projection payload, successful result/receipt rendering, the four-target manual plan, typed 422 failure, dropped response, reload recovery, explicit recovery and HTTP 500 ambiguity. These were intercepted fixture responses on a static localhost server, not camera commands or integrated backend proof. A 390×844 viewport had no horizontal overflow and was visually inspected. Receipts are `browser-*.txt` in the private panorama evidence directory; the screenshot is `output/playwright/panorama-mobile-fixture.png`. Expected network-console errors came from intentionally rejected/aborted fixture requests.
