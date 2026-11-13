"""Explicit-mask background recovery from already-registered JPEG/PNG frames.

White mask pixels assert that the corresponding source pixel is visible static
background; black means unknown/occluded. These assertions come from the caller,
not a statistical or semantic inference. Support fraction is not a probability.
The returned validity/never-observed masks must accompany the image: zero-filled
unsupported pixels are placeholders, not recovered or generated background.
"""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile

import cv2
import numpy as np

MAX_GUIDED_MASK_BYTES = 512 * 1024 * 1024
ALGORITHM_VERSION = "explicit-background-mask-average-v1"


@dataclass
class GuidedCrowdResult:
    image: np.ndarray  # BGR uint8; invalid pixels are black placeholders.
    support_count: np.ndarray  # Number of asserted background observations.
    confidence_map: np.ndarray  # round(255*count/N): support fraction, not probability.
    valid_mask: np.ndarray  # 255 only where minimum_observations is satisfied.
    never_observed_mask: np.ndarray  # 255 where no source asserts visible background.
    evidence: dict

    def save_bundle(self, directory):
        """Create a new private bundle. Never discard masks or overwrite outputs."""
        root = Path(directory)
        root.mkdir(mode=0o700)  # Existing paths/symlinks fail before any writes.
        arrays = {"recovered.png": self.image, "support_count.png": self.support_count,
                  "confidence_support.png": self.confidence_map, "valid_mask.png": self.valid_mask,
                  "never_observed_mask.png": self.never_observed_mask}
        outputs = {}
        for name, pixels in arrays.items():
            path = root / name
            if not cv2.imwrite(str(path), pixels):
                raise OSError(f"failed to write {name}; incomplete bundle remains at {root}")
            path.chmod(0o600)
            from stack_session import binary_receipt
            outputs[name] = binary_receipt(path, 160 * 1024 * 1024)
        evidence = {**self.evidence, "outputs": outputs}
        descriptor = os.open(root / "evidence.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(evidence, stream, indent=2, allow_nan=False)
            stream.write("\n")
        return root


def _read_mask(path, size):
    from image_safety import MAX_IMAGE_SOURCE_BYTES, image_size_from_bytes
    from stack_session import regular_bytes
    data = regular_bytes(Path(path), MAX_IMAGE_SOURCE_BYTES)
    from image_safety import image_orientation_from_bytes
    if image_orientation_from_bytes(data) != 1:
        raise ValueError("guided masks require identity EXIF orientation; normalize sources and masks before registration")
    if image_size_from_bytes(data) != size:
        raise ValueError("background mask dimensions must match the source image")
    # PNG IHDR declares original grayscale mode before bounded RGB conversion.
    # Reject color/soft-label files instead of treating a model score as evidence.
    if (len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[8:16] != b"\x00\x00\x00\x0dIHDR"
            or data[24] not in (1, 8) or data[25] != 0):
        raise ValueError("background masks must be binary grayscale PNG files")
    from image_safety import open_rgb_from_bytes
    with open_rgb_from_bytes(data) as image:
        with image.getchannel("R") as channel:
            pixels = np.asarray(channel)
            if not np.all((pixels == 0) | (pixels == 255)):
                raise ValueError("background masks must contain only 0 and 255; soft/model scores are not verified background")
            mask = (pixels == 255).astype(np.uint8)
    return mask, {"path": str(Path(path).resolve()), "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}



@contextmanager
def _source_snapshots(paths):
    """Bind source hashes, orientation and decoder bytes to private snapshots.

    An external source can change and change back while processing. Re-reading
    it later cannot prove which bytes a decoder saw; decoding private copies can.
    """
    from image_safety import MAX_IMAGE_SOURCE_BYTES, image_orientation_from_bytes
    from stack_session import regular_bytes, MAX_SESSION_SOURCE_BYTES
    with tempfile.TemporaryDirectory(prefix="camera-background-sources-") as directory:
        snapshots, receipts = [], []
        total = 0
        for index, path in enumerate(paths):
            data = regular_bytes(Path(path), MAX_IMAGE_SOURCE_BYTES)
            if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG\r\n\x1a\n")):
                raise ValueError("guided source bytes must be JPEG or PNG")
            if image_orientation_from_bytes(data) != 1:
                raise ValueError("guided sources require identity EXIF orientation; normalize sources and masks before registration")
            total += len(data)
            if total > MAX_SESSION_SOURCE_BYTES:
                raise ValueError("guided source snapshots exceed aggregate encoded-byte budget")
            if shutil.disk_usage(directory).free < len(data) + 64 * 1024**2:
                raise ValueError("insufficient temporary storage for immutable guided sources")
            snapshot = Path(directory) / f"{index:02d}{Path(path).suffix.lower()}"
            descriptor = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            snapshots.append(snapshot)
            receipts.append({"path": str(Path(path).resolve()), "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest(), "exif_orientation": 1})
            del data
        yield snapshots, receipts

def recover_background(paths, background_masks, *, registered=False, minimum_observations=1):
    """Recover only explicit visible-background samples; never infer unseen pixels.

    All inputs must already share a registered pixel grid. Registration is not
    silently re-estimated from moving foreground because that would invalidate
    the supplied masks. Nonidentity EXIF orientation is rejected before decode.
    Incorrect masks or registration remain caller errors.
    """
    if registered is not True:
        raise ValueError("explicit registered=True is required: masks and sources must already share a registered pixel grid")
    if not isinstance(paths, (list, tuple)) or not isinstance(background_masks, (list, tuple)) or len(paths) != len(background_masks):
        raise ValueError("provide one background mask for every source image")
    if type(minimum_observations) is not int or not 1 <= minimum_observations <= len(paths):
        raise ValueError("minimum_observations must be between 1 and the number of source frames")
    if any(Path(path).suffix.lower() not in (".jpg", ".jpeg", ".png") for path in paths):
        raise ValueError("guided crowd recovery supports JPEG/PNG sources; RAW is not decoded")
    from stack_session import validate_frames
    validate_frames(len(paths))
    with _source_snapshots(paths) as (snapshots, receipts):
        return _recover_from_snapshots(snapshots, background_masks, receipts, minimum_observations)


def _recover_from_snapshots(paths, background_masks, source_receipts, minimum_observations):
    import stacking
    from image_safety import MAX_IMAGE_SOURCE_BYTES
    from stack_session import binary_receipt
    width, height = stacking._validate_sources(list(paths))
    if width * height * len(paths) > MAX_GUIDED_MASK_BYTES:
        raise ValueError("guided mask stack exceeds aggregate temporary-mask budget")
    estimate = width * height * 16 + (stacking.STACK_TILE_SIZE + 2 * stacking._STACK_HALO)**2 * 128 + 96*1024**2
    if estimate > stacking.MAX_STACK_WORKING_BYTES:
        raise ValueError("guided recovery exceeds estimated working-memory budget")
    with tempfile.TemporaryDirectory(prefix="camera-background-masks-") as directory:
        if shutil.disk_usage(directory).free < width * height * len(paths) + 64*1024**2:
            raise ValueError("insufficient temporary storage for background masks")
        masks, mask_receipts = [], []
        encoded_bytes = 0
        for index, path in enumerate(background_masks):
            mask, receipt = _read_mask(path, (width, height))
            encoded_bytes += receipt["bytes"]
            if encoded_bytes > MAX_GUIDED_MASK_BYTES:
                raise ValueError("background masks exceed aggregate encoded-byte budget")
            target = Path(directory) / f"{index:02d}.mask"
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                mask.tofile(stream)
            masks.append(target)
            mask_receipts.append(receipt)
            del mask
        with stacking._staged_stack(list(paths), align=False) as (files, shape, _):
            image = np.zeros(shape, np.uint8)
            support_count = np.empty((height, width), np.uint8)
            confidence = np.empty_like(support_count)
            valid = np.empty_like(support_count)
            never = np.empty_like(support_count)
            for bounds, dest, interior in stacking._tiles(shape):
                count = None
                total = None
                for frame, mask_path in zip(files, masks):
                    tile = stacking._tile_from_file(frame, shape, bounds)
                    background = stacking._tile_from_file(mask_path, (height, width), bounds)
                    if count is None:
                        count = np.zeros(background.shape, np.uint8)
                        total = np.zeros(tile.shape, np.float32)
                    count += background
                    total += tile * background[:, :, None]
                counts = count[interior]
                accepted = counts >= minimum_observations
                result = np.rint(total[interior] / np.maximum(counts, 1)[:, :, None]).clip(0, 255).astype(np.uint8)
                result[~accepted] = 0
                image[dest] = result
                support_count[dest] = counts
                confidence[dest] = np.rint(counts.astype(np.float32) * (255 / len(files))).astype(np.uint8)
                valid[dest] = accepted.astype(np.uint8) * 255
                never[dest] = (counts == 0).astype(np.uint8) * 255
    # Source replacements must not be presented as provenance for different pixels.
    for original in source_receipts + mask_receipts:
        current = binary_receipt(Path(original["path"]), MAX_IMAGE_SOURCE_BYTES)
        if current["sha256"] != original["sha256"]:
            raise ValueError("source changed during guided background recovery")
    evidence = {"algorithm": ALGORITHM_VERSION, "frames": len(paths), "minimum_observations": minimum_observations,
        "source_receipts": source_receipts, "mask_receipts": mask_receipts,
        "registration": "caller_asserted_pre_registered_no_additional_warp",
        "source_binding": "private_encoded_snapshots_with_matching_sha256_and_identity_orientation",
        "background_labels": "caller_asserted_known_visible_background_not_automatically_verified",
        "confidence_semantics": "support_count divided by total frames; not a probability or semantic correctness score",
        "mask_semantics": "255 means valid/never-observed in the respective binary map; confidence encodes support fraction 0..255",
        "valid_fraction": float(np.count_nonzero(valid)/valid.size),
        "never_observed_fraction": float(np.count_nonzero(never)/never.size),
        "unsupported_placeholder": "black; consult valid_mask, not image brightness",
        "unseen_pixels_generated": False, "originals_unchanged": True}
    return GuidedCrowdResult(image, support_count, confidence, valid, never, evidence)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", nargs=2, action="append", required=True, metavar=("IMAGE", "BACKGROUND_MASK"))
    parser.add_argument("--registered", action="store_true", help="assert all source images and masks share a registered pixel grid")
    parser.add_argument("--minimum-observations", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True, help="new private bundle directory; never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; choose a new directory")
    try:
        result = recover_background([frame[0] for frame in args.frame], [frame[1] for frame in args.frame],
                                    registered=args.registered, minimum_observations=args.minimum_observations)
        root = result.save_bundle(args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"bundle": str(root.resolve()), "evidence": str((root / "evidence.json").resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
