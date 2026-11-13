"""Bounded offline JPEG mosaics and virtual-camera projections.

This is a planar homography stitcher, not calibrated 360-degree reconstruction.
All heavy native image work happens in a supervised child. Callers own gallery
path authorization; this module never opens a camera or requests another photo.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

MAX_FRAMES = 12
MAX_SOURCE_BYTES = 48 * 1024 * 1024
MAX_SOURCE_PIXELS = 36_000_000
WORK_PIXELS_PER_FRAME = 1_500_000
MAX_OUTPUT_PIXELS = 8_000_000
MAX_RSS_BYTES = 1024 * 1024 * 1024
MIN_OVERLAP = .3
MIN_STITCH_OVERLAP = .08
ROTATION_MIN_INLIERS = 20
ROTATION_MIN_SPREAD = .01
ROTATION_TOLERANCE_PX = 2.5
SEAM_PIXELS_PER_FRAME = 250_000
SEAM_BLEND_PIXELS = 8.
PROJECTIONS = {
    'spherical': 'spherical', 'cylindrical': 'cylindrical',
    'mercator': 'mercator', 'transverse_mercator': 'transverseMercator',
    'compressed_rectilinear_1': 'compressedPlaneA2B1',
    'compressed_rectilinear_2': 'compressedPlaneA1.5B1',
    'panini_1': 'paniniA2B1', 'panini_2': 'paniniA1.5B1',
    'rectilinear': 'plane',
}
_LOCK = threading.Lock()


class PanoramaError(RuntimeError):
    def __init__(self, message, *, reason='panorama_failed', job_id=None):
        super().__init__(message)
        self.reason = reason
        self.job_id = job_id
        self.retry_capture = False


def _number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite and between {low} and {high}.')
    return float(value)


def probe_opencv():
    """Execute the stitcher toolchain. Presence on PATH/import is not enough."""
    try:
        import cv2
        import numpy as np
        version = cv2.__version__
        if not version:
            raise RuntimeError('OpenCV reported an empty version.')
        sample = np.zeros((16, 16, 3), np.uint8)
        sample[4:12, 4:12] = 200
        warped = cv2.warpPerspective(sample, np.eye(3, dtype=np.float64), (16, 16))
        if warped.shape != (16, 16, 3) or float(warped.mean()) <= 0:
            raise RuntimeError('OpenCV warpPerspective did not execute.')
        warper = cv2.PyRotationWarper('cylindrical', 8.0)
        roi = warper.warpRoi((16, 16), np.float32([[8, 0, 7.5], [0, 8, 7.5], [0, 0, 1]]), np.eye(3, dtype=np.float32))
        if roi[2] < 2 or roi[3] < 2:
            raise RuntimeError('OpenCV rotation warper did not execute.')
        return {'opencv_version': version, 'warp_executed': True, 'warper_executed': True}
    except Exception as exc:
        raise PanoramaError(f'Panorama stitcher toolchain is not usable: {exc}',
                            reason='toolchain_unavailable') from exc


def frame_fov_deg(focal_length_mm, sensor_width_mm, sensor_height_mm):
    """Pinhole field of view from focal length and sensor size, in degrees."""
    focal_length_mm = _number(focal_length_mm, 8, 400, 'focal_length_mm')
    sensor_width_mm = _number(sensor_width_mm, 1, 50, 'sensor_width_mm')
    sensor_height_mm = _number(sensor_height_mm, 1, 50, 'sensor_height_mm')
    hfov = math.degrees(2 * math.atan(sensor_width_mm / (2 * focal_length_mm)))
    vfov = math.degrees(2 * math.atan(sensor_height_mm / (2 * focal_length_mm)))
    return {'hfov_deg': hfov, 'vfov_deg': vfov, 'focal_length_mm': focal_length_mm,
            'sensor_width_mm': sensor_width_mm, 'sensor_height_mm': sensor_height_mm}


def _grid_order(rows, columns):
    cells = []
    for row in range(rows):
        for col in (range(columns) if row % 2 == 0 else reversed(range(columns))):
            cells.append((row, col))
    return cells


def _serpentine_frames(rows, columns, yaw_step, pitch_step):
    frames = []
    for index, (row, col) in enumerate(_grid_order(rows, columns)):
        frames.append({'index': index, 'row': row, 'column': col,
                       'yaw_deg': (col - (columns - 1) / 2) * yaw_step,
                       'pitch_deg': ((rows - 1) / 2 - row) * pitch_step})
    return frames


def _layout(layout, n_sources=None):
    if layout is None:
        return None
    if not isinstance(layout, dict):
        raise ValueError('layout must be an object with integer rows and columns.')
    rows, columns = layout.get('rows'), layout.get('columns')
    if any(type(n) is not int or n < 1 for n in (rows, columns)):
        raise ValueError('layout rows and columns must be positive integers.')
    if not 2 <= rows * columns <= MAX_FRAMES:
        raise ValueError(f'layout must describe 2..{MAX_FRAMES} frames.')
    if n_sources is not None and rows * columns != n_sources:
        raise ValueError('layout rows×columns must match the number of source frames.')
    return {'rows': int(rows), 'columns': int(columns), 'order': 'serpentine'}


def _normalize_crop(crop):
    if crop is None:
        return None
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise ValueError('crop must be four normalized coordinates [left, top, right, bottom].')
    values = [_number(value, 0, 1, 'crop') for value in crop]
    if not (values[0] < values[2] and values[1] < values[3]):
        raise ValueError('crop must be inside the image with positive area.')
    if values[2] - values[0] < .05 or values[3] - values[1] < .05:
        raise ValueError('crop is too small.')
    return values


def _frames_for_span(fov_deg, span_deg, overlap):
    if span_deg <= fov_deg + 1e-9:
        return 1
    step = fov_deg * (1 - overlap)
    return 1 + math.ceil((span_deg - fov_deg) / step - 1e-12)


def guided_plan(rows, columns, overlap=.35, hfov_deg=60, vfov_deg=40):
    """Manual serpentine targets; actual overlap still needs image verification."""
    if any(type(n) is not int or n < 1 for n in (rows, columns)) or not 2 <= rows * columns <= MAX_FRAMES:
        raise ValueError(f'Plan needs 2..{MAX_FRAMES} frames with positive integer rows and columns.')
    overlap = _number(overlap, MIN_OVERLAP, .8, 'overlap')
    hfov_deg = _number(hfov_deg, 10, 100, 'hfov_deg')
    vfov_deg = _number(vfov_deg, 10, 100, 'vfov_deg')
    yaw_step, pitch_step = hfov_deg * (1 - overlap), vfov_deg * (1 - overlap)
    frames = _serpentine_frames(rows, columns, yaw_step, pitch_step)
    return {'frames': frames, 'overlap_target': overlap, 'order': 'serpentine',
            'rows': rows, 'columns': columns, 'yaw_step_deg': yaw_step, 'pitch_step_deg': pitch_step,
            'hfov_deg': hfov_deg, 'vfov_deg': vfov_deg, 'multi_row': rows >= 2,
            'session': {'frame_count': rows * columns, 'exposure_policy': 'locked_base',
                        'layout': {'rows': rows, 'columns': columns, 'order': 'serpentine'}},
            'guidance': 'Move the tripod head manually; keep textured overlap between rows. Lock exposure, white balance and focus. Targets are approximate; they are not measured camera orientation.',
            'capture_performed': False}


def capture_plan(*, focal_length_mm, sensor_width_mm, sensor_height_mm, overlap=.35,
                 yaw_span_deg, pitch_span_deg):
    """Rows×columns and pitch-per-row from optics, sensor size and ≥30% overlap."""
    overlap = _number(overlap, MIN_OVERLAP, .8, 'overlap')
    yaw_span_deg = _number(yaw_span_deg, 1, 360, 'yaw_span_deg')
    pitch_span_deg = _number(pitch_span_deg, 1, 180, 'pitch_span_deg')
    fov = frame_fov_deg(focal_length_mm, sensor_width_mm, sensor_height_mm)
    columns = _frames_for_span(fov['hfov_deg'], yaw_span_deg, overlap)
    rows = _frames_for_span(fov['vfov_deg'], pitch_span_deg, overlap)
    count = rows * columns
    if count < 2:
        raise ValueError('Scene fits in one frame; a panorama needs at least two overlapping views.')
    if count > MAX_FRAMES:
        raise ValueError(f'This optics/overlap/span plan needs {count} frames (max {MAX_FRAMES}). '
                         'Use a wider lens, a smaller yaw/pitch span, or less overlap.')
    yaw_step = fov['hfov_deg'] * (1 - overlap)
    pitch_step = fov['vfov_deg'] * (1 - overlap)
    frames = _serpentine_frames(rows, columns, yaw_step, pitch_step)
    return {'frames': frames, 'overlap_target': overlap, 'order': 'serpentine',
            'rows': rows, 'columns': columns, 'yaw_step_deg': yaw_step, 'pitch_step_deg': pitch_step,
            'yaw_span_deg': yaw_span_deg, 'pitch_span_deg': pitch_span_deg, 'multi_row': rows >= 2,
            **fov, 'geometry': 'pinhole_atan',
            'session': {'frame_count': count, 'exposure_policy': 'locked_base',
                        'layout': {'rows': rows, 'columns': columns, 'order': 'serpentine'}},
            'guidance': 'Move the tripod head manually; keep textured overlap between rows. Lock exposure, white balance and focus. Targets come from pinhole geometry, not a measured IMU or camera orientation.',
            'capture_performed': False}


def plan_from_mapping(data):
    """API/CLI dispatcher: optics plan when complete, otherwise an explicit grid."""
    if not isinstance(data, dict):
        raise ValueError('Plan request must be an object.')
    optics = ('focal_length_mm', 'sensor_width_mm', 'sensor_height_mm', 'yaw_span_deg', 'pitch_span_deg')
    present = [key for key in optics if data.get(key) is not None]
    if present and len(present) != len(optics):
        raise ValueError('Optics plan needs focal_length_mm, sensor_width_mm, sensor_height_mm, '
                         'yaw_span_deg and pitch_span_deg.')
    if present:
        kwargs = {key: data[key] for key in optics}
        if data.get('overlap') is not None:
            kwargs['overlap'] = data['overlap']
        return capture_plan(**kwargs)
    return guided_plan(int(data.get('rows', 1)), int(data.get('columns', 3)),
                       overlap=data.get('overlap', .35), hfov_deg=data.get('hfov_deg', 60),
                       vfov_deg=data.get('vfov_deg', 40))


def _write_json(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        json.dump(data, out, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stage(source, destination):
    source = Path(source)
    if source.suffix.lower() not in {'.jpg', '.jpeg'} or source.is_symlink():
        raise PanoramaError('Sources must be regular JPEG files.', reason='invalid_source')
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as original:
        before = os.fstat(original.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise PanoramaError('JPEG exceeds byte budget or is not a regular file.', reason='invalid_source')
        digest = hashlib.sha256()
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
            count = 0
            while chunk := original.read(min(1024 * 1024, MAX_SOURCE_BYTES + 1 - count)):
                count += len(chunk)
                if count > MAX_SOURCE_BYTES:
                    raise PanoramaError('Source grew beyond byte budget.', reason='invalid_source')
                digest.update(chunk)
                out.write(chunk)
        after = os.fstat(original.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or count != before.st_size:
            raise PanoramaError('Source changed while snapshotting.', reason='source_changed')
    return {'index': int(destination.stem), 'name': source.name, 'sha256': digest.hexdigest(), 'bytes': count}


def _rss(pid):
    if sys.platform.startswith('linux'):
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
        except FileNotFoundError:
            return 0
    result = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    return int(result.stdout.strip() or 0) * 1024


def build_panorama(sources, output_root, *, projection='spherical', horizontal_fov_deg=90,
                   rotation_deg=0, autocrop=True, timeout_seconds=60, cancel_event=None,
                   level_horizon=True, crop=None, layout=None):
    """Return a durable private job receipt, or PanoramaError with its job_id.

    Sources are snapshotted and SHA-checked before native decode. Raster work is
    capped at 1.5 MP/frame, 8 MP/output, 12 frames. A 1 GiB RSS watchdog is sampled,
    not a claim of a hard instantaneous allocation ceiling on macOS. Originals are
    never modified.
    """
    sources = list(sources)
    if not 2 <= len(sources) <= MAX_FRAMES or len({str(Path(p).resolve()) for p in sources}) != len(sources):
        raise ValueError(f'Supply 2..{MAX_FRAMES} distinct JPEG paths.')
    if projection not in PROJECTIONS:
        raise ValueError('Unknown panorama projection.')
    horizontal_fov_deg = _number(horizontal_fov_deg, 20, 140, 'horizontal_fov_deg')
    rotation_deg = _number(rotation_deg, -45, 45, 'rotation_deg')
    timeout_seconds = _number(timeout_seconds, .1, 120, 'timeout_seconds')
    if type(autocrop) is not bool:
        raise ValueError('autocrop must be a boolean.')
    if type(level_horizon) is not bool:
        raise ValueError('level_horizon must be a boolean.')
    crop = _normalize_crop(crop)
    layout = _layout(layout, n_sources=len(sources))
    root = Path(output_root)
    if root.is_symlink():
        raise ValueError('Output root cannot be a symbolic link.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    job_id = uuid.uuid4().hex
    job = root / job_id
    job.mkdir(mode=0o700)
    receipt = {'job_id': job_id, 'status': 'running', 'capture_performed': False,
               'algorithm': 'sift-ransac-planar-mosaic-virtual-projection-v2', 'inputs': [],
               'projection': projection, 'horizontal_fov_deg': horizontal_fov_deg,
               'rotation_deg': rotation_deg, 'autocrop': autocrop,
               'level_horizon': level_horizon, 'crop': crop, 'layout': layout}
    process = None
    started = time.monotonic()
    peak = 0
    try:
        if not _LOCK.acquire(blocking=False):
            raise PanoramaError('Another panorama is processing.', reason='busy')
        try:
            if cancel_event and cancel_event.is_set():
                raise PanoramaError('Panorama canceled.', reason='canceled')
            for index, source in enumerate(sources):
                receipt['inputs'].append(_stage(source, job / f'{index}.jpg'))
            if len({item['sha256'] for item in receipt['inputs']}) != len(sources):
                raise PanoramaError('Duplicate image content is not an additional panorama frame.', reason='invalid_source')
            _write_json(job / 'request.json', receipt)
            env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
            with os.fdopen(os.open(job / 'worker.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(job.resolve())], stdout=log, stderr=log, start_new_session=True, env=env)
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    peak = max(peak, _rss(process.pid))
                    if cancel_event and cancel_event.is_set():
                        raise PanoramaError('Panorama canceled.', reason='canceled')
                    if elapsed > timeout_seconds:
                        raise PanoramaError('Panorama exceeded its deadline.', reason='timeout')
                    if peak > MAX_RSS_BYTES:
                        raise PanoramaError('Panorama exceeded its RSS budget.', reason='memory_limit')
                    time.sleep(.1)
            result_path = job / 'result.json'
            if not result_path.is_file():
                raise PanoramaError(f'Panorama worker exited {process.returncode}.', reason='worker_failed')
            result = json.loads(result_path.read_text())
            if result['status'] != 'completed' or process.returncode:
                raise PanoramaError(result.get('error', 'Worker failed.'), reason=result.get('reason', 'worker_failed'))
            receipt.update(result)
        finally:
            _LOCK.release()
    except Exception as exc:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        receipt.update(status='failed', reason=getattr(exc, 'reason', 'invalid_source'), error=str(exc))
        for filename in ('panorama.jpg', 'coverage.png'):
            (job / filename).unlink(missing_ok=True)
        raise PanoramaError(str(exc), reason=receipt['reason'], job_id=job_id) from exc
    finally:
        receipt.update(elapsed_seconds=time.monotonic() - started, observed_peak_rss_bytes=peak)
        for index in range(len(sources)):
            (job / f'{index}.jpg').unlink(missing_ok=True)
        _write_json(job / 'receipt.json', receipt)
    return receipt


def _configure_native():
    """Pin OpenCV threads, RNG and OpenCL so stitch/horizon geometry follows the pixels, not the host SIMD."""
    import cv2
    cv2.setNumThreads(1)
    cv2.setRNGSeed(1701)
    if hasattr(cv2, 'ocl'):
        cv2.ocl.setUseOpenCL(False)


def _corners(shape):
    import numpy as np
    h, w = shape[:2]
    return np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]).reshape(-1, 1, 2)


def _align(images, layout=None):
    import cv2
    import numpy as np
    _configure_native()
    layout = _layout(layout, n_sources=len(images))
    cells = _grid_order(layout['rows'], layout['columns']) if layout else None
    detector = cv2.SIFT_create(nfeatures=2500)
    features = [detector.detectAndCompute(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), None) for image in images]
    transforms = [np.eye(3)]
    evidence = []
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    for index in range(1, len(images)):
        best = None
        points_i, descriptors_i = features[index]
        if descriptors_i is None:
            raise PanoramaError(f'Frame {index} has insufficient textured overlap.', reason='insufficient_overlap')
        for previous in range(index):
            points_p, descriptors_p = features[previous]
            if descriptors_p is None or len(descriptors_p) < 2:
                continue
            matches = matcher.knnMatch(descriptors_i, descriptors_p, k=2)
            good = [a for pair in matches if len(pair) == 2 for a, b in [pair] if a.distance < .7 * b.distance]
            good.sort(key=lambda m: (float(m.distance), int(m.queryIdx), int(m.trainIdx)))
            if len(good) < 20:
                continue
            source = np.float32([points_i[m.queryIdx].pt for m in good])
            target = np.float32([points_p[m.trainIdx].pt for m in good])
            _configure_native()
            transform, inliers = cv2.findHomography(source, target, cv2.RANSAC, 2.5, maxIters=2000, confidence=.995)
            if transform is None or inliers is None:
                continue
            selected = inliers.ravel().astype(bool)
            count = int(selected.sum())
            if count < 20 or count / len(good) < .5:
                continue
            if any(cv2.contourArea(cv2.convexHull(points[selected])) < .03 * image.shape[0] * image.shape[1]
                   for points, image in ((source, images[index]), (target, images[previous]))):
                continue
            corners = _corners(images[index].shape)
            denominator = corners.reshape(4, 2) @ transform[2, :2] + transform[2, 2]
            if not (np.all(denominator > 1e-6) or np.all(denominator < -1e-6)):
                continue
            warped = cv2.perspectiveTransform(corners, transform).reshape(4, 2)
            if not np.isfinite(warped).all() or not cv2.isContourConvex(warped):
                continue
            area = cv2.contourArea(warped, oriented=True)
            input_area = images[index].shape[0] * images[index].shape[1]
            if not .2 * input_area < area < 5 * input_area:
                continue
            overlap, _ = cv2.intersectConvexConvex(warped, _corners(images[previous].shape).reshape(4, 2))
            overlap_fraction = float(overlap / min(area, images[previous].shape[0] * images[previous].shape[1]))
            if overlap_fraction < MIN_STITCH_OVERLAP:
                continue
            errors = np.linalg.norm(cv2.perspectiveTransform(source[selected].reshape(-1, 1, 2), transform).reshape(-1, 2) - target[selected], axis=1)
            candidate = (count, previous, transform, float(np.median(errors)), overlap_fraction)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            raise PanoramaError(f'Frame {index} cannot align reliably: use textured, overlapping views and avoid parallax.', reason='insufficient_overlap')
        count, previous, transform, error, overlap = best
        transforms.append(transforms[previous] @ transform)
        item = {'frame': index, 'anchor_frame': previous, 'inliers': count,
                'median_reprojection_error_pixels': error, 'estimated_overlap_fraction': overlap}
        if cells:
            item['row'], item['column'] = cells[index]
            item['anchor_row'], item['anchor_column'] = cells[previous]
        evidence.append(item)
    if layout and layout['rows'] >= 2:
        if not any(item['row'] != item['anchor_row'] for item in evidence):
            raise PanoramaError('Rows did not overlap; keep at least 30% textured overlap between rows.',
                                reason='insufficient_overlap')
    return transforms, evidence


def _mosaic(images, transforms):
    import cv2
    import numpy as np
    _configure_native()
    corners = np.concatenate([cv2.perspectiveTransform(_corners(image.shape), transform).reshape(-1, 2)
                              for image, transform in zip(images, transforms)])
    if not np.isfinite(corners).all():
        raise PanoramaError('Unbounded panorama transform.', reason='geometry_rejected')
    low = np.floor(corners.min(axis=0))
    high = np.ceil(corners.max(axis=0)) + 1
    width, height = (high - low).astype(int)
    if width < 2 or height < 2 or width > 16000 or height > 16000 or width * height > MAX_OUTPUT_PIXELS:
        raise PanoramaError('Mosaic exceeds the 8 MP geometry budget.', reason='output_limit')
    offset = np.array([[1, 0, -low[0]], [0, 1, -low[1]], [0, 0, 1]], dtype=np.float64)
    total = np.zeros((height, width, 3), np.float32)
    weight_sum = np.zeros((height, width), np.float32)
    for image, transform in zip(images, transforms):
        h, w = image.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        mask[1:-1, 1:-1] = 255
        weights = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        weights /= max(1., float(weights.max()))
        warped = cv2.warpPerspective(image, offset @ transform, (width, height))
        warped_weights = cv2.warpPerspective(weights, offset @ transform, (width, height))
        total += warped.astype(np.float32) * warped_weights[..., None]
        weight_sum += warped_weights
    mask = (weight_sum > .001).astype(np.uint8) * 255
    if np.count_nonzero(mask) < 1.1 * max(image.shape[0] * image.shape[1] for image in images):
        raise PanoramaError('Views add less than 10% new coverage; a repeated framing is not a panorama.', reason='insufficient_new_coverage')
    np.divide(total, np.maximum(weight_sum[..., None], .001), out=total)
    return np.clip(total, 0, 255).astype(np.uint8), mask, [list(map(float, low)), list(map(float, high))]


def _focal_from_35mm(focal_35mm, width, height):
    """Pixel focal length from EXIF FocalLengthIn35mmFilm (defined on the 36×24 mm diagonal)."""
    # Pillow may decode the tag as an IFDRational, which is a numbers.Real but not an int or float.
    if isinstance(focal_35mm, bool) or not isinstance(focal_35mm, numbers.Real):
        return None
    focal_35mm = float(focal_35mm)
    if not math.isfinite(focal_35mm) or not 8 <= focal_35mm <= 1200:
        return None
    return focal_35mm * math.hypot(width, height) / math.hypot(36, 24)


def _rays(points, focal, shape):
    import numpy as np
    h, w = shape[:2]
    rays = np.column_stack([(points[:, 0] - (w - 1) / 2) / focal, (points[:, 1] - (h - 1) / 2) / focal,
                            np.ones(len(points))])
    return rays / np.linalg.norm(rays, axis=1, keepdims=True)


def _fit_rotations(a, b):
    """Batched least-squares rotations R with R a ≈ b; a, b are (..., n, 3) unit rays."""
    import numpy as np
    u, _s, vt = np.linalg.svd(np.swapaxes(b, -1, -2) @ a)
    u[..., :, 2] *= np.sign(np.linalg.det(u @ vt))[..., None]
    return u @ vt


def _rotation_ransac(a, b, tolerance, iterations=1000):
    """Deterministic two-ray RANSAC for a pure camera rotation; returns (R, inlier mask)."""
    import numpy as np
    rng = np.random.default_rng(1701)
    first = rng.integers(0, len(a), iterations)
    second = (first + rng.integers(1, len(a), iterations)) % len(a)
    best = None
    for start in range(0, iterations, 200):
        picks = np.stack([first[start:start + 200], second[start:start + 200]], axis=1)
        hypotheses = _fit_rotations(a[picks], b[picks])
        inliers = np.linalg.norm(np.einsum('kij,nj->kni', hypotheses, a) - b[None], axis=2) < tolerance
        index = int(np.argmax(inliers.sum(axis=1)))
        if best is None or inliers[index].sum() > best.sum():
            best = inliers[index]
    for _ in range(2):
        rotation = _fit_rotations(a[best], b[best])
        best = np.linalg.norm(a @ rotation.T - b, axis=1) < tolerance
        if best.sum() < 3:
            break
    return _fit_rotations(a[best], b[best]) if best.sum() >= 3 else None, best


def _rotation_overlap(rotation, focal, shape):
    """Share of one frame's pixel grid whose rays land inside the other frame (same intrinsics)."""
    import numpy as np
    h, w = shape[:2]
    xs, ys = np.meshgrid(np.linspace(0, w - 1, 24), np.linspace(0, h - 1, 16))
    rays = _rays(np.column_stack([xs.ravel(), ys.ravel()]), focal, shape) @ rotation.T
    ahead = rays[:, 2] > 1e-6
    x = rays[:, 0] / np.where(ahead, rays[:, 2], 1) * focal + (w - 1) / 2
    y = rays[:, 1] / np.where(ahead, rays[:, 2], 1) * focal + (h - 1) / 2
    return float((ahead & (x >= 0) & (x <= w - 1) & (y >= 0) & (y <= h - 1)).mean())


def _align_rotation(images, focal, layout=None):
    """Tripod-sweep alignment: one shared pinhole focal, one 3-D rotation per frame.

    A panned camera maps frames by H = K R K⁻¹. Past roughly 40° of pan the planar mosaic
    cannot hold that on frame 0's plane, so this path estimates R from matched rays instead.
    A pair counts only when its rotation-consistent inliers pass Brown & Lowe's probabilistic
    check (inliers > 8 + 0.3·matches), are spread over ≥1% of both frames and the pair's
    views overlap ≥8%. Frames join along a maximum-inlier spanning tree rooted at frame 0;
    a frame that joins nowhere is refused with its best candidate's numbers.
    """
    import cv2
    import numpy as np
    _configure_native()
    layout = _layout(layout, n_sources=len(images))
    cells = _grid_order(layout['rows'], layout['columns']) if layout else None
    shape = images[0].shape
    if any(image.shape != shape for image in images):
        raise PanoramaError('The rotation model needs frames of one size from one lens setting.',
                            reason='insufficient_overlap')
    area = shape[0] * shape[1]
    tolerance = ROTATION_TOLERANCE_PX / focal
    # Night interiors at high ISO give SIFT few corners; local contrast equalization only
    # changes which corners are detected, never the pixels that are stitched.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    detector = cv2.SIFT_create(nfeatures=2500)
    features = [detector.detectAndCompute(clahe.apply(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)), None)
                for image in images]
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    edges, candidates = {}, {}
    for index in range(1, len(images)):
        for other in range(index):
            points_i, descriptors_i = features[index]
            points_o, descriptors_o = features[other]
            record = {'matches': 0, 'inliers': 0, 'required_inliers': ROTATION_MIN_INLIERS,
                      'reason': 'too few matched features'}
            if descriptors_i is not None and descriptors_o is not None and len(descriptors_i) >= 2 and len(descriptors_o) >= 2:
                good = [a for pair in matcher.knnMatch(descriptors_i, descriptors_o, k=2)
                        if len(pair) == 2 for a, b in [pair] if a.distance < .7 * b.distance]
                good.sort(key=lambda m: (float(m.distance), int(m.queryIdx), int(m.trainIdx)))
                record['matches'] = len(good)
                record['required_inliers'] = max(ROTATION_MIN_INLIERS, math.floor(8 + .3 * len(good)) + 1)
                if len(good) >= ROTATION_MIN_INLIERS:
                    source = np.float64([points_i[m.queryIdx].pt for m in good])
                    target = np.float64([points_o[m.trainIdx].pt for m in good])
                    rays_i, rays_o = _rays(source, focal, shape), _rays(target, focal, shape)
                    record['rays'] = rays_i, rays_o
                    rotation, selected = _rotation_ransac(rays_i, rays_o, tolerance)
                    count = int(selected.sum())
                    record['inliers'] = count
                    record['reason'] = 'too few matches fit one camera rotation (parallax, repeated texture or too little overlap)'
                    if rotation is not None and count >= record['required_inliers']:
                        spread = min(cv2.contourArea(cv2.convexHull(points[selected].astype(np.float32))) / area
                                     for points in (source, target))
                        overlap = _rotation_overlap(rotation, focal, shape)
                        record.update(spread_fraction=spread, estimated_overlap_fraction=overlap)
                        if spread < ROTATION_MIN_SPREAD:
                            record['reason'] = 'matching features cover too little of the frame'
                        elif overlap < MIN_STITCH_OVERLAP:
                            record['reason'] = 'views overlap too little'
                        else:
                            residual = np.linalg.norm(rays_i[selected] @ rotation.T - rays_o[selected], axis=1)
                            record.update(reason=None, rotation=rotation,
                                          median_reprojection_error_pixels=float(np.median(residual) * focal))
                            edges[(index, other)] = record
            candidates[(index, other)] = record
    world = {0: np.eye(3)}
    parent = {}
    while len(world) < len(images):
        joins = [(record['inliers'], -max(pair), -min(pair), pair) for pair, record in edges.items()
                 if (pair[0] in world) != (pair[1] in world)]
        if not joins:
            # Name the unjoined frame whose best link into the aligned frames came closest.
            linked = [(record['inliers'], -max(pair), -min(pair), pair) for pair, record in candidates.items()
                      if (pair[0] in world) != (pair[1] in world)]
            pair = max(linked)[3]
            record = candidates[pair]
            frame, partner = (pair[1], pair[0]) if pair[0] in world else pair
            raise PanoramaError(
                f"Frame {frame} cannot align reliably: its best candidate, frame {partner}, has "
                f"{record['inliers']} of {record['matches']} matches fitting one camera rotation "
                f"(needs {record['required_inliers']}): {record['reason']}. Use textured, overlapping "
                'views and avoid parallax from near objects.', reason='insufficient_overlap')
        _count, _a, _b, (index, other) = max(joins)
        rotation = edges[(index, other)]['rotation']  # rays of `index` into `other`'s camera
        if other in world:
            world[index] = world[other] @ rotation
            parent[index] = other
        else:
            world[other] = world[index] @ rotation.T
            parent[other] = index
    evidence = []
    for frame in range(1, len(images)):
        anchor = parent[frame]
        record = edges[(max(frame, anchor), min(frame, anchor))]
        item = {'frame': frame, 'anchor_frame': anchor, 'inliers': record['inliers'], 'matches': record['matches'],
                'median_reprojection_error_pixels': record['median_reprojection_error_pixels'],
                'estimated_overlap_fraction': record['estimated_overlap_fraction']}
        if cells:
            item['row'], item['column'] = cells[frame]
            item['anchor_row'], item['anchor_column'] = cells[anchor]
        evidence.append(item)
    if layout and layout['rows'] >= 2 and not any(item['row'] != item['anchor_row'] for item in evidence):
        raise PanoramaError('Rows did not overlap; keep at least 30% textured overlap between rows.',
                            reason='insufficient_overlap')
    rotations = [world[frame] for frame in range(len(images))]
    # Every other pair that the solved sweep says overlaps by the planned 30% must agree with it.
    # Near objects shift between views of a camera not turned about its lens (parallax): such a
    # pair's matches fit no rotation, or not the rotation the rest of the sweep implies, and a
    # blend would double them. A drift of twice the RANSAC tolerance is allowed along the chain.
    loops = []
    for (index, other), record in sorted(candidates.items()):
        if parent.get(index) == other or parent.get(other) == index or 'rays' not in record:
            continue
        relative = rotations[other].T @ rotations[index]
        overlap = _rotation_overlap(relative, focal, shape)
        if overlap < MIN_OVERLAP or record['matches'] < 2 * ROTATION_MIN_INLIERS:
            continue
        rays_i, rays_o = record['rays']
        agreeing = int((np.linalg.norm(rays_i @ relative.T - rays_o, axis=1) < 2 * tolerance).sum())
        check = {'frame': index, 'other_frame': other, 'estimated_overlap_fraction': overlap,
                 'matches': record['matches'], 'agreeing_matches': agreeing,
                 'required_matches': record['required_inliers']}
        loops.append(check)
        if agreeing < record['required_inliers']:
            raise PanoramaError(
                f"Frames {index} and {other} overlap by {overlap:.0%} but only {agreeing} of their "
                f"{record['matches']} matches agree with the rest of the sweep (needs {record['required_inliers']}): "
                'near objects shift between these views (parallax), so a stitch would duplicate or cut them. Turn the '
                "camera about the lens's entrance pupil (a nodal slide) or keep near objects out of the overlap.",
                reason='parallax')
    return rotations, evidence, loops


def _render_rotation(images, rotations, focal, projection, autocrop):
    """Warp each frame by its own rotation onto one projection surface; blend along graph-cut seams."""
    import cv2
    import numpy as np
    _configure_native()
    h, w = images[0].shape[:2]
    intrinsic = np.float32([[focal, 0, (w - 1) / 2], [0, focal, (h - 1) / 2], [0, 0, 1]])
    if projection == 'rectilinear':
        corners = _rays(np.float64([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]), focal, images[0].shape)
        if min(float((corners @ rotation.T)[:, 2].min()) for rotation in rotations) < math.cos(math.radians(75)):
            raise PanoramaError('A rectilinear projection cannot hold a sweep this wide; use spherical or cylindrical.',
                                reason='output_limit')
    scale = focal
    for _ in range(3):
        warper = cv2.PyRotationWarper(PROJECTIONS[projection], scale)
        rois = [warper.warpRoi((w, h), intrinsic, rotation.astype(np.float32)) for rotation in rotations]
        x0, y0 = min(roi[0] for roi in rois), min(roi[1] for roi in rois)
        x1, y1 = max(roi[0] + roi[2] for roi in rois), max(roi[1] + roi[3] for roi in rois)
        width, height = x1 - x0, y1 - y0
        if width <= 16000 and height <= 16000 and width * height <= MAX_OUTPUT_PIXELS:
            break
        scale *= .98 * min(math.sqrt(MAX_OUTPUT_PIXELS / (width * height)), 16000 / max(width, height))
    else:
        raise PanoramaError('Projected panorama exceeds output budget.', reason='output_limit')
    if scale < .25 * focal or width < 2 or height < 2:
        raise PanoramaError('Projected panorama exceeds output budget.', reason='output_limit')
    covered = np.zeros((height, width), np.uint8)
    largest = 0
    full = np.full((h, w), 255, np.uint8)
    placed = []
    for image, rotation in zip(images, rotations):
        rotation = rotation.astype(np.float32)
        corner, warped = warper.warp(image, intrinsic, rotation, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
        _, warped_mask = warper.warp(full, intrinsic, rotation, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
        left, top = corner[0] - x0, corner[1] - y0
        rows = slice(max(0, top), min(height, top + warped.shape[0]))
        cols = slice(max(0, left), min(width, left + warped.shape[1]))
        source = (slice(rows.start - top, rows.stop - top), slice(cols.start - left, cols.stop - left))
        mask = np.where(warped_mask[source] == 255, 255, 0).astype(np.uint8)
        covered[rows, cols][mask == 255] = 255
        largest = max(largest, int(np.count_nonzero(mask)))
        placed.append((rows, cols, warped[source], mask))
    if np.count_nonzero(covered) < 1.1 * largest:
        raise PanoramaError('Views add less than 10% new coverage; a repeated framing is not a panorama.',
                            reason='insufficient_new_coverage')
    # A camera turned off its entrance pupil shifts near objects between views. Averaging the
    # whole overlap doubles them; a colour-and-gradient graph-cut seam routes each overlap
    # through places where the views agree, and blends only a narrow band either side of it.
    seam_scale = min(1., math.sqrt(SEAM_PIXELS_PER_FRAME / max(mask.size for *_, mask in placed)))
    small = lambda array, interpolation: cv2.resize(array, (max(1, round(array.shape[1] * seam_scale)),
                                                            max(1, round(array.shape[0] * seam_scale))),
                                                    interpolation=interpolation)
    seams = cv2.detail_GraphCutSeamFinder('COST_COLOR_GRAD').find(
        [small(warped, cv2.INTER_AREA).astype(np.float32) for _, _, warped, _ in placed],
        [(round(cols.start * seam_scale), round(rows.start * seam_scale)) for rows, cols, *_ in placed],
        [small(mask, cv2.INTER_NEAREST) for *_, mask in placed])
    total = np.zeros((height, width, 3), np.float32)
    weight_sum = np.zeros((height, width), np.float32)
    for (rows, cols, warped, mask), seam in zip(placed, seams):
        seam = seam.get() if hasattr(seam, 'get') else seam
        seam = cv2.resize(cv2.dilate(seam, np.ones((3, 3), np.uint8)), (mask.shape[1], mask.shape[0]),
                          interpolation=cv2.INTER_LINEAR)
        seam = np.where((seam > 0) & (mask == 255), 255, 0).astype(np.uint8)
        band = np.minimum(cv2.distanceTransform(seam, cv2.DIST_L2, 3), SEAM_BLEND_PIXELS) / SEAM_BLEND_PIXELS
        # A small constant over the tile's fully covered pixels fills any pixel a resized
        # seam mask missed, so every covered pixel has a positive weight.
        weights = band + 1e-3 * (mask == 255)
        total[rows, cols] += warped.astype(np.float32) * weights[..., None]
        weight_sum[rows, cols] += weights
    np.divide(total, weight_sum[..., None], out=total, where=weight_sum[..., None] > 0)
    result = np.clip(total, 0, 255).astype(np.uint8)
    pre_crop = {'width': int(width), 'height': int(height)}
    crop = _valid_crop(covered) if autocrop else [0, 0, width, height]
    x_start, y_start, x_end, y_end = crop
    bounds = [[float(x0), float(y0)], [float(x1), float(y1)]]
    return result[y_start:y_end, x_start:x_end], covered[y_start:y_end, x_start:x_end], crop, pre_crop, bounds, scale


def _sweep_centre_yaw(rotations):
    """Heading to put at longitude 0 so the surface's ±180° seam falls in the widest gap between views.

    A turn about the up axis only. Views within 10° of straight up or down have no reliable
    heading (it flips as the view crosses the pole) and are skipped. When gaps tie within 1°
    (views spread evenly around a full circle) the centre nearest frame 0 wins, and centres
    equally near it resolve to the counter-clockwise one, so a tiny change in the solved
    rotations cannot swing the panorama around.
    """
    headings = [math.atan2(float(rotation[0, 2]), float(rotation[2, 2])) for rotation in rotations
                if math.hypot(float(rotation[0, 2]), float(rotation[2, 2])) > math.sin(math.radians(10))]
    if not headings:
        return 0.0
    reference = headings[0]
    ordered = sorted(headings)
    # Repeated headings (one column shot in two rows) leave zero gaps, not full circles.
    gaps = [((ordered[(i + 1) % len(ordered)] - ordered[i]) % (2 * math.pi), ordered[i])
            for i in range(len(ordered))]
    widest = max(gap for gap, _start in gaps)
    if widest == 0:
        return reference

    def centre_of(gap, start):
        return math.remainder(start + gap / 2 + math.pi, 2 * math.pi)

    offsets = [math.remainder(centre_of(gap, start) - reference, 2 * math.pi)
               for gap, start in gaps if gap >= widest - math.radians(1)]
    # Candidates as near frame 0 as each other (within 1°) resolve to the counter-clockwise one.
    nearest = min(abs(offset) for offset in offsets)
    return math.remainder(reference + max(offset for offset in offsets if abs(offset) <= nearest + math.radians(1)),
                          2 * math.pi)


def _level_rotations(rotations, level_horizon, user_deg):
    """Wave-correct the sweep (camera x-axes into one horizontal plane), centre its heading, apply the user roll."""
    import cv2
    import numpy as np
    method, corrected = 'disabled', [np.asarray(rotation, np.float64) for rotation in rotations]
    if level_horizon and len(rotations) >= 2:
        kind = getattr(cv2.detail, 'WAVE_CORRECT_AUTO', cv2.detail.WAVE_CORRECT_HORIZ)
        corrected = [np.asarray(rotation, np.float64) for rotation in
                     cv2.detail.waveCorrect([rotation.astype(np.float32) for rotation in rotations], kind)]
        method = 'wave_correct'
    yaw = _sweep_centre_yaw(corrected)
    centre = np.float64([[math.cos(yaw), 0, -math.sin(yaw)], [0, 1, 0], [math.sin(yaw), 0, math.cos(yaw)]])
    corrected = [centre @ rotation for rotation in corrected]
    angle = math.radians(user_deg)
    roll = np.float64([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
    # How far "up" moved from frame 0's own up axis (its pitch and roll on the tripod).
    change = corrected[0] @ np.asarray(rotations[0], np.float64).T
    wave_deg = math.degrees(math.acos(max(-1., min(1., float(change[1, 1])))))
    return [roll @ rotation for rotation in corrected], method, wave_deg


def _valid_crop(mask):
    """Largest valid rectangle on a conservative <=512-cell grid."""
    import numpy as np
    h, w = mask.shape
    block = max(1, math.ceil(max(h, w) / 512))
    gh, gw = h // block, w // block
    if gh == 0 or gw == 0:
        raise PanoramaError('No valid crop.', reason='empty_coverage')
    valid = (mask[:gh * block, :gw * block].reshape(gh, block, gw, block).min(axis=(1, 3)) == 255)
    heights = [0] * gw
    best = (0, 0, 0, 0, 0)
    for row in range(gh):
        heights = [height + 1 if valid[row, col] else 0 for col, height in enumerate(heights)]
        stack = []
        for col in range(gw + 1):
            height = heights[col] if col < gw else 0
            start = col
            while stack and stack[-1][1] > height:
                left, old = stack.pop()
                area = old * (col - left)
                if area > best[0]:
                    best = (area, left, row + 1 - old, col, row + 1)
                start = left
            if not stack or stack[-1][1] < height:
                stack.append((start, height))
    if best[0] == 0:
        raise PanoramaError('No valid crop.', reason='empty_coverage')
    return [value * block for value in best[1:]]


def _undirected_deg(dx, dy):
    angle = math.degrees(math.atan2(dy, dx))
    if angle > 90:
        angle -= 180
    if angle <= -90:
        angle += 180
    return angle


def _dominant_line_rotation(segments, span, method, min_share=.28):
    """Length-weighted dominant 1° bin; undetermined if the long edges disagree.

    Segments are (angle, length, offset), offset being where the line crosses the
    working raster's middle, in pixels along the other axis. The fourth value counts
    agreeing lines at least 24 px (~5% of the ≤512 px raster) apart, so both edges of
    a narrow pole or the Hough fragments of one line count once (a pole wider than
    24 px on the raster still counts twice).
    """
    if not segments:
        return 0.0, 0.0, 'undetermined', 0
    bins = {}
    for angle, length, _offset in segments:
        key = int(round(angle))
        bins[key] = bins.get(key, 0.0) + length
    peak_bin = max(bins, key=lambda key: (bins[key], -abs(key), key))
    total = sum(length for _, length, _ in segments)
    local = [segment for segment in segments if abs(int(round(segment[0])) - peak_bin) <= 2]
    local_w = sum(length for _, length, _ in local)
    if local_w < .35 * span or local_w < min_share * total:
        return 0.0, 0.0, 'undetermined', 0
    separated, last = 0, None
    for offset in sorted(offset for _, _, offset in local):
        if last is None or offset - last >= 24:
            separated, last = separated + 1, offset
    sine = cosine = 0.0
    for angle, length, _offset in local:
        rad = math.radians(2 * angle)
        sine += length * math.sin(rad)
        cosine += length * math.cos(rad)
    tilt = round((0.5 * math.degrees(math.atan2(sine, cosine))) * 4) / 4
    rotation = max(-45.0, min(45.0, -tilt))
    if abs(rotation) < 1e-12:
        rotation = 0.0
    return rotation, min(1.0, local_w / total), method, separated


def _estimate_horizon_deg(image, mask):
    """Roll from agreeing near-verticals, else near-horizontals; 0 if neither agrees.

    Ceiling and floor perspective fans in a tall mosaic can own the near-horizontal
    1° bin while the room is still rolled. World-verticals stay parallel under
    optical-axis roll, so they are preferred when a clear majority agrees (one side
    of a keystone fan is not a majority) and at least four separated lines agree.
    Fewer agreeing verticals only veto a near-horizontal estimate that disagrees by
    more than 1°, unless 90% of that family agrees, so a thin leaning pole can neither
    tilt a level horizon nor be mistaken for a rolled room. A one-sided keystone fan
    (pitched, off-centre facade) can still pass as roll. Coverage-boundary edges are
    not scene lines and are dropped for both families. Probabilistic Hough
    and Canny differ across OpenCV SIMD: work on a blurred ≤512 px raster, pin
    threads/RNG, then take the dominant 1° bin.
    """
    import cv2
    import numpy as np
    _configure_native()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = np.where(mask == 255, gray, 0).astype(np.uint8)
    height, width = gray.shape
    scale = min(1., 512 / max(height, width))
    if scale < 1:
        gray = cv2.resize(gray, (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                          interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    work_h, work_w = gray.shape
    covered = cv2.resize(mask, (work_w, work_h), interpolation=cv2.INTER_AREA) == 255
    covered = cv2.erode(covered.astype(np.uint8), np.ones((9, 9), np.uint8))
    edges[covered == 0] = 0
    min_len = max(24, min(work_h, work_w) // 4)
    _configure_native()
    lines = cv2.HoughLinesP(edges, 1, math.pi / 180, threshold=max(20, min_len // 2),
                            minLineLength=min_len, maxLineGap=max(8, min_len // 8))
    if lines is None:
        return 0.0, 0.0, 'undetermined'
    verticals, horizontals = [], []
    for x0, y0, x1, y1 in sorted(lines.reshape(-1, 4).tolist()):
        dx, dy = float(x1 - x0), float(y1 - y0)
        length = math.hypot(dx, dy)
        if length < min_len:
            continue
        angle = _undirected_deg(dx, dy)
        if abs(angle) <= 45:
            horizontals.append((angle, length, y0 + (work_w / 2 - x0) * dy / dx))
            continue
        tilt = angle - 90.0 if angle > 0 else angle + 90.0
        if abs(tilt) <= 20:
            verticals.append((tilt, length, x0 + (work_h / 2 - y0) * dx / dy))
    vertical = _dominant_line_rotation(verticals, work_h, 'hough_near_vertical', min_share=.65)
    horizontal = _dominant_line_rotation(horizontals, work_w, 'hough_near_horizontal')
    # Four or more separated parallel verticals is how a rolled room looks. Fewer can be
    # a door frame or a leaning pole or tree: they never level on their own, and they
    # veto a disagreeing horizontal estimate unless 90% of that family agrees (a horizon).
    if vertical[2] != 'undetermined' and vertical[3] >= 4:
        return vertical[:3]
    if (vertical[2] != 'undetermined' and horizontal[2] != 'undetermined'
            and abs(horizontal[0] - vertical[0]) > 1 and horizontal[1] < .9):
        return 0.0, 0.0, 'undetermined'
    return horizontal[:3]


def _apply_manual_crop(image, mask, crop, origin):
    """Crop a normalized box of the current raster; require fully covered pixels."""
    height, width = mask.shape
    x0 = int(round(crop[0] * width))
    y0 = int(round(crop[1] * height))
    x1 = int(round(crop[2] * width))
    y1 = int(round(crop[3] * height))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise PanoramaError('Manual crop is too small.', reason='invalid_crop')
    region = mask[y0:y1, x0:x1]
    if region.size == 0 or int((region == 255).min()) != 1:
        raise PanoramaError('Manual crop includes uncovered pixels.', reason='invalid_crop')
    final = [origin[0] + x0, origin[1] + y0, origin[0] + x1, origin[1] + y1]
    return image[y0:y1, x0:x1], region, final


def _project(image, mask, projection, hfov, rotation, autocrop):
    import cv2
    import numpy as np
    _configure_native()
    h, w = image.shape[:2]
    focal = w / (2 * math.tan(math.radians(hfov) / 2))
    intrinsic = np.float32([[focal, 0, (w - 1) / 2], [0, focal, (h - 1) / 2], [0, 0, 1]])
    angle = math.radians(rotation)
    rotation_matrix = np.float32([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
    warper = cv2.PyRotationWarper(PROJECTIONS[projection], focal)
    roi = warper.warpRoi((w, h), intrinsic, rotation_matrix)
    if roi[2] < 2 or roi[3] < 2 or roi[2] > 16000 or roi[3] > 16000 or roi[2] * roi[3] > MAX_OUTPUT_PIXELS:
        raise PanoramaError('Projected panorama exceeds output budget.', reason='output_limit')
    _, result = warper.warp(image, intrinsic, rotation_matrix, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
    # Linear coverage followed by exact-255 preserves only wholly covered samples.
    _, projected_mask = warper.warp(mask, intrinsic, rotation_matrix, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
    projected_mask = (projected_mask == 255).astype(np.uint8) * 255
    pre_crop = {'width': int(result.shape[1]), 'height': int(result.shape[0])}
    crop = _valid_crop(projected_mask) if autocrop else [0, 0, result.shape[1], result.shape[0]]
    x0, y0, x1, y1 = crop
    return result[y0:y1, x0:x1], projected_mask[y0:y1, x0:x1], crop, pre_crop


def _worker(job):
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (120, 121))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    import cv2
    from PIL import Image
    _configure_native()
    request = json.loads((job / 'request.json').read_text())
    try:
        probe = probe_opencv()
        images, inputs = [], []
        for item in request['inputs']:
            path = job / f"{item['index']}.jpg"
            if _sha(path) != item['sha256']:
                raise PanoramaError('Snapshot SHA mismatch.', reason='source_changed')
            with Image.open(path) as header:
                width, height = header.size
                container_format = header.format
                # Sony JPEGs can carry MPO metadata/secondary thumbnails. Decode only primary frame.
                if header.format not in {'JPEG', 'MPO'} or width < 32 or height < 32 or width * height > MAX_SOURCE_PIXELS:
                    raise PanoramaError('JPEG dimensions exceed source budget.', reason='invalid_source')
                exif = header.getexif()
                if exif.get(274, 1) != 1:
                    raise PanoramaError('Normalize EXIF orientation into pixels before stitching.', reason='unsupported_orientation')
                focal_35mm = exif.get_ifd(0x8769).get(41989)
                header.verify()
            image = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
            if image is None or image.shape[:2] != (height, width):
                raise PanoramaError('JPEG decode failed or dimensions changed.', reason='invalid_source')
            scale = min(1., math.sqrt(WORK_PIXELS_PER_FRAME / (width * height)))
            if scale < 1:
                image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
            images.append(image)
            focal = _focal_from_35mm(focal_35mm, width, height)
            inputs.append(dict(item, width=width, height=height, container_format=container_format, decoded_frame=0,
                               working_width=image.shape[1], working_height=image.shape[0],
                               focal_length_35mm=float(focal_35mm) if focal else None,
                               working_focal_pixels=focal * image.shape[1] / width if focal else None))
        user_rotation = float(request['rotation_deg'])
        geometry = {'model': 'planar'}
        try:
            transforms, alignment = _align(images, request.get('layout'))
            mosaic, mask, bounds = _mosaic(images, transforms)
        except PanoramaError as planar:
            if planar.reason not in {'insufficient_overlap', 'output_limit', 'geometry_rejected'}:
                raise
            # A panned tripod sweep: one plane cannot hold it, but one rotation per frame can.
            focals = {round(item['working_focal_pixels'], 3) for item in inputs if item['working_focal_pixels']}
            if len(focals) == 1 and all(item['working_focal_pixels'] for item in inputs):
                focal, focal_source = focals.pop(), 'exif_focal_length_35mm'
            else:
                focal = images[0].shape[1] / (2 * math.tan(math.radians(request['horizontal_fov_deg']) / 2))
                focal_source = 'horizontal_fov_deg'
            try:
                rotations, alignment, loops = _align_rotation(images, focal, request.get('layout'))
            except PanoramaError as rotated:
                raise PanoramaError(f'{rotated} (The planar mosaic also refused: {planar.reason}.)',
                                    reason=rotated.reason) from rotated
            rotations, method, wave_deg = _level_rotations(rotations, request.get('level_horizon', True), user_rotation)
            output, coverage, crop_box, pre_crop, bounds, scale = _render_rotation(
                images, rotations, focal, request['projection'], request['autocrop'])
            geometry = {'model': 'rotation', 'planar_refusal': {'reason': planar.reason, 'error': str(planar)},
                        'working_focal_pixels': focal, 'focal_source': focal_source, 'projection_scale': scale,
                        'loop_checks': loops}
            geometry['up_axis_change_deg'] = wave_deg
            # No 2-D roll is estimated here: wave correction levels the sweep in 3-D.
            estimated_rotation, confidence, applied_rotation = 0.0, None, user_rotation
        if geometry['model'] == 'planar':
            estimated_rotation, confidence, method = (0.0, 0.0, 'disabled')
            if request.get('level_horizon', True):
                estimated_rotation, confidence, method = _estimate_horizon_deg(mosaic, mask)
            applied_rotation = max(-45.0, min(45.0, estimated_rotation + user_rotation))
            output, coverage, crop_box, pre_crop = _project(mosaic, mask, request['projection'], request['horizontal_fov_deg'],
                                                            applied_rotation, request['autocrop'])
        post_crop = {'width': int(output.shape[1]), 'height': int(output.shape[0])}
        manual = _normalize_crop(request.get('crop'))
        if manual:
            output, coverage, crop_box = _apply_manual_crop(output, coverage, manual, crop_box)
        for name, array in [('panorama.jpg', output), ('coverage.png', coverage)]:
            if not cv2.imwrite(str(job / name), array):
                raise PanoramaError('Output encoding failed.', reason='write_failed')
            os.chmod(job / name, 0o600)
        result = {'status': 'completed', 'inputs': inputs, 'alignment': alignment,
                  'mosaic_bounds': bounds, 'crop_xyxy': crop_box, 'manual_crop': manual,
                  'horizon': {'level_horizon': bool(request.get('level_horizon', True)),
                              'method': method, 'estimated_deg': estimated_rotation,
                              'user_deg': user_rotation, 'applied_deg': applied_rotation,
                              'confidence': confidence, 'leveling_angle_deg': applied_rotation},
                  'layout': request.get('layout'), 'geometry': geometry,
                  'algorithm': ('sift-ransac-planar-mosaic-virtual-projection-v2' if geometry['model'] == 'planar'
                                else 'sift-ransac-rotation-sweep-surface-projection-v1'),
                  'toolchain': probe,
                  'output': {'filename': 'panorama.jpg', 'sha256': _sha(job / 'panorama.jpg'),
                             'width': output.shape[1], 'height': output.shape[0],
                             'pre_crop_width': pre_crop['width'], 'pre_crop_height': pre_crop['height'],
                             'post_crop_width': post_crop['width'], 'post_crop_height': post_crop['height']},
                  'coverage': {'filename': 'coverage.png', 'sha256': _sha(job / 'coverage.png'),
                               'valid_fraction': float((coverage == 255).mean())},
                  'limitations': ['Planar homographies can fail with parallax, changing focus or textureless overlap.',
                                 'Projection uses a user-selected virtual field of view, not calibrated lens or 360-degree reconstruction.',
                                 'Working resolution is capped at 1.5 MP per input; this is not full-resolution stitching.',
                                 'Feather blending does not remove moving objects or correct exposure drift.',
                                 'Horizon leveling estimates a 2D rotation from long near-vertical edges when most of them agree, otherwise long near-horizontal edges; it is not IMU gravity and can be undetermined.',
                                 'Manual crop is applied after auto-crop and never writes back to the originals.']
                                + (['A sweep the planar mosaic cannot hold is aligned as one pinhole camera rotating about its lens; '
                                    'lens distortion is not modelled. Near objects that shift between views (parallax) '
                                    'are hidden by graph-cut seams when mild and refused when a pair disagrees with the '
                                    'sweep; a seam can still cut through a near object. Wave correction levels the sweep '
                                    'from the camera axes, not from gravity.']
                                   if geometry['model'] == 'rotation' else []),
                  'opencv_version': probe['opencv_version']}
        _write_json(job / 'result.json', result)
        return 0
    except Exception as exc:
        _write_json(job / 'result.json', {'status': 'failed', 'reason': getattr(exc, 'reason', 'worker_failed'), 'error': str(exc)})
        return 1


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit('Use tools/build_panorama.py.')
    raise SystemExit(_worker(Path(sys.argv[2])))
