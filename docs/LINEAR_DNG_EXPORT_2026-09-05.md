# Output-referred linear DNG export — 2026-09-05

A completed JPEG development can now produce a **DNG containing the same rendered 16-bit linear RGB samples as its TIFF**. It is explicitly JPEG-derived, output-referred and non-neural. It is not sensor RAW, a Bayer mosaic, reconstructed highlight detail or restored JPEG precision.

This product includes DNG technology under license by Adobe.

## Format decision and sources

Adobe permits output-referred values through `ColorimetricReference=1` from DNG 1.2 onward. The specification allows the primary image in IFD 0, requires orientation and a unique model identifier, and requires a color matrix for non-monochrome images. LinearRaw stores all color components per pixel. See [Adobe DNG Specification 1.7.1](https://helpx.adobe.com/content/dam/help/en/camera-raw/digital-negative/jcr_content/root/content/flex/items/position/position-par/download_section_733958301/download-1/DNG_Spec_1_7_1_0.pdf), pages 13, 22–25, 32–33, 47 and 100–103.

Our color matrix characterizes the defined linear-sRGB encoding under D65 using the [W3C XYZ-to-linear-sRGB matrix](https://www.w3.org/TR/css-color-4/#color-conversion-code). It is not a measured Sony sensor calibration. The file identifies its creator as Camera Autopilot Rendered linear sRGB derivative. The [Adobe DNG resource page](https://helpx.adobe.com/ca/camera-raw/desktop/dng-and-file-formats/digital-negative.html) provides the specification and licensing notice.

## Test it

From the repository root:

```bash
.venv/bin/python tools/export_linear_dng.py /absolute/development/linear.tiff /absolute/development/receipt.json --verification-python backend/.venv-ml/bin/python --output-root artifacts/linear-dng
```

Inspect `artifacts/linear-dng/<job_id>/output.dng`, `verification.json` and `receipt.json`. A missing or failing independent decoder removes the candidate DNG and retains a failure receipt. `--validate-input-only` produces the checked input contract without an export.

Python API:

```python
export_linear_dng(tiff_path, development_receipt_path, output_root,
                  verification_python=None)
```

Returns `job_id`, `status`, `output` (`filename`, `sha256`, `bytes`), `verification`, source/provenance hashes and explicit `sensor_raw=False`, `neural=False`, `source_precision_restored=False`. The output is `output_root/job_id/output.dng`. `DNGError` extends `ValueError` and exposes `reason`, `job_id`, `retry_capture=False`; filesystem errors can also arise before job creation. The default verifier is the current interpreter, which must contain `rawpy`. A different verifier path must be trusted server configuration, never a client command.

## Accepted input and bounds

Only this project's exact single-strip, uncompressed, little-endian RGB uint16 TIFF layout is accepted. The associated receipt must be a completed `linear-development-v1` JPEG result with the matching TIFF SHA-256, byte count and dimensions. The source JPEG provenance hash is required. RAW receipts and arbitrary TIFF layouts are rejected. No input image decoder is called by the writer; it checks bounded header fields and streams the existing samples.

Input limit: 40 MP and 240,065,536 bytes. Receipts and TIFF headers are bounded to 64 KiB. One export runs per process at a time. The independent decoder executes in a child with a 30-second deadline, CPU/file limits, a sampled 1 GiB RSS watchdog, and a Linux address-space ceiling. Only the newly generated, bounded DNG reaches that decoder. General user-supplied RAW/DNG decoding on macOS remains gated.

The writer sets DNG version/backward version 1.2, LinearRaw, output-referred colorimetry, three unsigned 16-bit channels, black 0, white 65535, D65, neutral RGB multipliers and a straight profile tone curve. It carries source JPEG/TIFF hashes and a JPEG-origin explanation in its image description. No CFA pattern, camera calibration measurements or private camera EXIF/GPS are fabricated or copied.

## Verification and limits

**10 focused tests pass in both Python environments.** They use actual JPEG developer outputs, verify every decoded sample and required metadata, and exercise changed-source hashes, unknown TIFF directories, RAW provenance, symlinks, missing decoder, concurrent export and bounded hashing. The root environment uses the explicitly supplied existing ML interpreter for independent decoder tests.

A real previously developed 6000×4000 Sony JPEG produced a 144,001,084-byte DNG. LibRaw 0.22.1 through rawpy 0.27.0 independently decoded all 72 million RGB uint16 samples with an exact hash match. LibRaw uses a four-channel internal buffer; the unused fourth channel was verified zero. The export took 0.489 seconds with a sampled verifier peak of 227,164,160 bytes on this Mac. ExifTool's independent `-validate` returned `OK`, with no warning/error and the expected output-referred/LinearRaw/16-bit tags.

Private evidence: `artifacts/falsification/implementation-wave-20260905/linear-dng/`:

- `real-photo-result.json` and `real-photo-exiftool-validation.json`.
- `real-photo/916a89b5bace44479e9b89be9a1d3045/output.dng` and its verification/receipt.
- `tests-root.txt`, `tests-ml.txt`, `source-manifest.json`.

Adobe's SDK `dng_validate` is not installed. ExifTool and LibRaw acceptance do not prove Adobe SDK conformance or visual rendering in Lightroom/Camera Raw. The first unpublished verification attempt exposed LibRaw's padded four-channel buffer; handling was corrected without changing any RGB samples. No decoder, SDK or package was installed.

A38 therefore gains a real independently decodable **rendered linear DNG export**. Sensor-RAW stacking, full RAW latitude and editor-specific color/appearance acceptance remain distinct requirements. Feedback should identify the export job ID, target editor, and whether the issue concerns opening, color, brightness or provenance. Keep the matching TIFF and receipt when comparing editors.
