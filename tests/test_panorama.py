"""Camera-free image geometry, bounded processing and recoverable failure proofs."""
import json
import math
from pathlib import Path
import stat
import sys
import threading

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import panorama


@pytest.fixture
def views(tmp_path):
    rng = np.random.default_rng(1701)
    world = rng.integers(0, 256, (620, 940, 3), dtype=np.uint8)
    world = cv2.GaussianBlur(world, (3, 3), 0)
    for i in range(60):
        x, y = rng.integers(10, 900), rng.integers(10, 580)
        cv2.circle(world, (int(x), int(y)), int(rng.integers(4, 18)), (20, 240, 80), 2)
    cv2.line(world, (30, 280), (900, 280), (250, 20, 20), 3)
    positions = [(0, 0), (300, 0), (300, 200), (0, 200)]
    paths, images = [], []
    for i, (x, y) in enumerate(positions):
        view = world[y:y + 420, x:x + 640].copy()
        path = tmp_path / f'view{i}.jpg'
        assert cv2.imwrite(str(path), view, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(path)
        images.append(cv2.imread(str(path)))
    return paths, images, positions


def test_multirow_ransac_recovers_known_geometry(views):
    paths, images, positions = views
    # Add a genuine projective transform to the final overlapping view.
    perspective = np.float64([[1, .007, 2], [-.004, 1, 3], [.000008, -.00001, 1]])
    images[-1] = cv2.warpPerspective(images[-1], perspective, (640, 420))
    transforms, evidence = panorama._align(images)
    sample = np.float32([[[100, 100]], [[400, 250]], [[500, 350]]])
    for index, (actual, (x, y)) in enumerate(zip(transforms, positions)):
        expected = np.float64([[1, 0, x], [0, 1, y], [0, 0, 1]])
        if index == 3:
            expected = expected @ np.linalg.inv(perspective)
        error = np.linalg.norm(cv2.perspectiveTransform(sample, actual) - cv2.perspectiveTransform(sample, expected), axis=2)
        assert error.max() < 1.5
    assert len(evidence) == 3
    assert min(item['inliers'] for item in evidence) >= 20


@pytest.mark.parametrize('projection', panorama.PROJECTIONS)
def test_each_projection_preserves_coverage_and_pixels(projection, views):
    _, images, _ = views
    image = images[0]
    mask = np.full(image.shape[:2], 255, np.uint8)
    result, valid, crop, _pre = panorama._project(image, mask, projection, 90, 3, True)
    assert result.shape[:2] == valid.shape
    assert result.shape[0] > 200 and result.shape[1] > 300
    assert valid.min() == 255
    assert result.std() > 10
    assert crop[2] > crop[0] and crop[3] > crop[1]


def test_supervised_multirow_job_has_hash_receipts_and_preserves_sources(views, tmp_path):
    paths, _, _ = views
    before = [panorama._sha(path) for path in paths]
    result = panorama.build_panorama(paths, tmp_path / 'jobs', projection='rectilinear',
                                     layout={'rows': 2, 'columns': 2}, level_horizon=False)
    job = tmp_path / 'jobs' / result['job_id']
    assert result['status'] == 'completed'
    assert result['output']['width'] >= 900
    assert result['output']['height'] >= 590
    assert result['output']['pre_crop_width'] >= result['output']['post_crop_width'] >= 900
    assert result['output']['pre_crop_height'] >= result['output']['post_crop_height']
    assert result['coverage']['valid_fraction'] == 1
    assert result['output']['sha256'] == panorama._sha(job / 'panorama.jpg')
    assert before == [panorama._sha(path) for path in paths]
    assert result['inputs'][0]['width'] == 640
    assert not list(job.glob('[0-9]*.jpg'))
    assert stat.S_IMODE(job.stat().st_mode) == 0o700
    assert stat.S_IMODE((job / 'panorama.jpg').stat().st_mode) == 0o600
    assert json.loads((job / 'receipt.json').read_text())['status'] == 'completed'


def test_textureless_images_fail_without_published_result(tmp_path):
    paths = [tmp_path / 'blank1.jpg', tmp_path / 'blank2.jpg']
    for i, path in enumerate(paths):
        cv2.imwrite(str(path), np.full((200, 300, 3), 100 + i * 10, np.uint8))
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama(paths, tmp_path / 'jobs')
    assert failed.value.reason == 'insufficient_overlap'
    job = tmp_path / 'jobs' / failed.value.job_id
    assert not (job / 'panorama.jpg').exists()
    assert not list(job.glob('[0-9]*.jpg'))
    assert json.loads((job / 'receipt.json').read_text())['status'] == 'failed'


def test_disjoint_textured_images_are_rejected():
    rng = np.random.default_rng(81)
    images = [rng.integers(0, 256, (250, 400, 3), dtype=np.uint8) for _ in range(2)]
    with pytest.raises(panorama.PanoramaError, match='overlap'):
        panorama._align(images)


def test_timeout_kills_child_and_logs_without_result(views, tmp_path):
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama(views[0], tmp_path / 'jobs', timeout_seconds=.1)
    assert failed.value.reason == 'timeout'
    job = tmp_path / 'jobs' / failed.value.job_id
    assert not (job / 'panorama.jpg').exists()
    assert json.loads((job / 'receipt.json').read_text())['reason'] == 'timeout'


def test_cancel_before_processing_is_durable(views, tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama(views[0], tmp_path / 'jobs', cancel_event=event)
    assert failed.value.reason == 'canceled'
    assert not (tmp_path / 'jobs' / failed.value.job_id / 'request.json').exists()


def test_duplicate_content_and_symlinks_are_not_extra_frames(views, tmp_path):
    first = views[0][0]
    duplicate = tmp_path / 'duplicate.jpg'
    duplicate.write_bytes(first.read_bytes())
    with pytest.raises(panorama.PanoramaError, match='Duplicate'):
        panorama.build_panorama([first, duplicate], tmp_path / 'jobs')
    link = tmp_path / 'link.jpg'
    link.symlink_to(first)
    with pytest.raises(ValueError, match='distinct'):
        panorama.build_panorama([first, link], tmp_path / 'jobs')


def test_manual_plan_is_serpentine_and_no_camera_claim():
    plan = panorama.guided_plan(2, 3)
    assert [(f['row'], f['column']) for f in plan['frames']] == [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]
    assert plan['capture_performed'] is False
    assert plan['frames'][0]['pitch_deg'] > plan['frames'][3]['pitch_deg']
    with pytest.raises(ValueError):
        panorama.guided_plan(4, 4)
    with pytest.raises(ValueError):
        panorama.guided_plan(2, 3, overlap=float('nan'))


def test_output_limit_precedes_warp_allocation(views):
    images = views[1][:2]
    transforms = [np.eye(3), np.float64([[1, 0, 20000], [0, 1, 0], [0, 0, 1]])]
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama._mosaic(images, transforms)
    assert failed.value.reason == 'output_limit'


def test_repeated_framing_cannot_masquerade_as_new_coverage(views):
    image = views[1][0]
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama._mosaic([image, image], [np.eye(3), np.eye(3)])
    assert failed.value.reason == 'insufficient_new_coverage'


@pytest.mark.parametrize('projection', ['spherical', 'cylindrical'])
def test_projection_marker_matches_analytic_ray_geometry(projection):
    import math
    width, height = 640, 420
    image = np.zeros((height, width, 3), np.uint8)
    x, y = 490, 120
    cv2.circle(image, (x, y), 5, (0, 0, 255), -1)
    mask = np.full((height, width), 255, np.uint8)
    result, _, _, _ = panorama._project(image, mask, projection, 90, 0, False)
    focal = width / 2
    dx, dy = x - (width - 1) / 2, y - (height - 1) / 2
    expected_x = focal * math.atan2(dx, focal)
    if projection == 'spherical':
        expected_y = focal * (math.pi / 2 + math.atan2(dy, math.sqrt(dx * dx + focal * focal)))
    else:
        expected_y = focal * dy / math.sqrt(dx * dx + focal * focal)
    intrinsic = np.float32([[focal, 0, (width - 1) / 2], [0, focal, (height - 1) / 2], [0, 0, 1]])
    # Only origin/extent comes from OpenCV; the marker projection above is independent.
    roi = cv2.PyRotationWarper(projection, focal).warpRoi((width, height), intrinsic, np.eye(3, dtype=np.float32))
    rows, columns = np.where(result[..., 2] > 180)
    assert abs(columns.mean() - (expected_x - roi[0])) < 1
    assert abs(rows.mean() - (expected_y - roi[1])) < 1


def test_sony_mpo_primary_jpeg_is_accepted(views, tmp_path):
    from PIL import Image
    paths = []
    for i, array in enumerate(views[1][:2]):
        path = tmp_path / f'sony{i}.jpg'
        Image.fromarray(cv2.cvtColor(array, cv2.COLOR_BGR2RGB)).save(
            path, format='MPO', save_all=True, append_images=[Image.new('RGB', (64, 42))])
        paths.append(path)
    result = panorama.build_panorama(paths, tmp_path / 'jobs')
    assert result['status'] == 'completed'
    assert all(item['container_format'] == 'MPO' and item['decoded_frame'] == 0 for item in result['inputs'])
    assert result['inputs'][0]['width'] == 640


def test_opencv_probe_executes_not_just_imports():
    probe = panorama.probe_opencv()
    assert probe['warp_executed'] is True
    assert probe['warper_executed'] is True
    assert probe['opencv_version']
    # Fail closed: an empty or non-numeric version is not a working stitcher.
    assert any(ch.isdigit() for ch in probe['opencv_version'])


def test_capture_plan_from_optics_is_pinhole_geometry():
    plan = panorama.capture_plan(focal_length_mm=24, sensor_width_mm=35.8, sensor_height_mm=23.9,
                                 overlap=.35, yaw_span_deg=120, pitch_span_deg=80)
    expected_hfov = math.degrees(2 * math.atan(35.8 / 48))
    expected_vfov = math.degrees(2 * math.atan(23.9 / 48))
    assert abs(plan['hfov_deg'] - expected_hfov) < 1e-9
    assert abs(plan['vfov_deg'] - expected_vfov) < 1e-9
    assert plan['rows'] == 2 and plan['columns'] == 2
    assert plan['multi_row'] is True
    assert plan['capture_performed'] is False
    assert plan['pitch_step_deg'] == pytest.approx(expected_vfov * .65)
    assert [(f['row'], f['column']) for f in plan['frames']] == [(0, 0), (0, 1), (1, 1), (1, 0)]
    assert plan['session']['frame_count'] == 4
    assert plan['session']['layout'] == {'rows': 2, 'columns': 2, 'order': 'serpentine'}
    with pytest.raises(ValueError, match='12'):
        panorama.capture_plan(focal_length_mm=24, sensor_width_mm=35.8, sensor_height_mm=23.9,
                              overlap=.35, yaw_span_deg=360, pitch_span_deg=180)
    with pytest.raises(ValueError):
        panorama.capture_plan(focal_length_mm=24, sensor_width_mm=35.8, sensor_height_mm=23.9,
                              overlap=.2, yaw_span_deg=40, pitch_span_deg=30)
    with pytest.raises(ValueError, match='one frame'):
        panorama.capture_plan(focal_length_mm=14, sensor_width_mm=35.8, sensor_height_mm=23.9,
                              overlap=.35, yaw_span_deg=20, pitch_span_deg=15)
    optics = panorama.plan_from_mapping({'focal_length_mm': 24, 'sensor_width_mm': 35.8,
                                         'sensor_height_mm': 23.9, 'yaw_span_deg': 120,
                                         'pitch_span_deg': 80, 'overlap': .35,
                                         'rows': 1, 'columns': 3})
    assert optics['rows'] == 2 and optics['columns'] == 2
    grid = panorama.plan_from_mapping({'rows': 2, 'columns': 3})
    assert len(grid['frames']) == 6
    with pytest.raises(ValueError, match='Optics plan'):
        panorama.plan_from_mapping({'focal_length_mm': 24, 'rows': 2, 'columns': 3})


def _equirect_texture(width=1440, height=720):
    rng = np.random.default_rng(1701)
    world = rng.integers(48, 200, (height, width, 3), dtype=np.uint8)
    for _ in range(280):
        x = int(rng.integers(8, width - 8))
        y = int(rng.integers(8, height - 8))
        radius = int(rng.integers(6, 22))
        color = tuple(int(c) for c in rng.integers(10, 255, 3))
        cv2.circle(world, (x, y), radius, color, -1)
        cv2.circle(world, (x, y), radius, (20, 240, 80), 2)
    equator = height // 2
    cv2.line(world, (0, equator), (width - 1, equator), (250, 250, 250), 5)
    for lon in range(0, width, width // 12):
        cv2.line(world, (lon, 0), (lon, height - 1), (40, 220, 90), 2)
    return world


def _rotation(yaw_deg, pitch_deg, roll_deg):
    y, p, r = map(math.radians, (yaw_deg, pitch_deg, roll_deg))
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    yaw = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    pitch = np.array([[1, 0, 0], [0, cp, sp], [0, -sp, cp]], dtype=np.float64)
    roll = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]], dtype=np.float64)
    return yaw @ pitch @ roll


def render_sphere_view(env, yaw_deg, pitch_deg, roll_deg, hfov_deg, width, height):
    """Pinhole sample of an equirectangular sphere. Camera-free synthetic fixture."""
    eh, ew = env.shape[:2]
    ys, xs = np.indices((height, width), dtype=np.float64)
    focal = (width - 1) / (2 * math.tan(math.radians(hfov_deg) / 2))
    x = (xs - (width - 1) / 2) / focal
    y_up = -((ys - (height - 1) / 2) / focal)
    z = np.ones_like(x)
    rays = np.stack([x, y_up, z], axis=-1)
    world = rays @ _rotation(yaw_deg, pitch_deg, roll_deg).T
    lon = np.arctan2(world[..., 0], world[..., 2])
    lat = np.arctan2(world[..., 1], np.hypot(world[..., 0], world[..., 2]))
    map_x = ((lon / math.pi + 1) * .5 * (ew - 1)).astype(np.float32)
    map_y = ((.5 - lat / math.pi) * (eh - 1)).astype(np.float32)
    return cv2.remap(env, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def _write_sphere_grid(tmp_path, plan, env, hfov_deg, size, roll_deg=0):
    width, height = size
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for frame in plan['frames']:
        view = render_sphere_view(env, frame['yaw_deg'], frame['pitch_deg'], roll_deg, hfov_deg, width, height)
        path = tmp_path / f"row{frame['row']}_col{frame['column']}.jpg"
        assert cv2.imwrite(str(path), view, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(path)
    return paths


def test_multirow_sphere_grid_stitches_and_low_overlap_is_refused(tmp_path):
    env = _equirect_texture()
    hfov, overlap = 50.0, .35
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * 320 / 480))
    wide = panorama.guided_plan(2, 3, overlap=overlap, hfov_deg=hfov, vfov_deg=vfov)
    wide_paths = _write_sphere_grid(tmp_path / 'wide', wide, env, hfov, (480, 320))
    images = [cv2.imread(str(path)) for path in wide_paths]
    _transforms, evidence = panorama._align(images, {'rows': 2, 'columns': 3})
    assert len(evidence) == 5
    assert any(item['row'] != item['anchor_row'] for item in evidence)
    assert min(item['estimated_overlap_fraction'] for item in evidence) >= .3
    # A 2×3 rotating-camera set explodes the planar 8 MP canvas; told the lens field of view
    # (these renders carry no EXIF focal length) it stitches as one rotating camera instead.
    wide_result = panorama.build_panorama(wide_paths, tmp_path / 'exploded', horizontal_fov_deg=hfov,
                                          layout={'rows': 2, 'columns': 3})
    assert wide_result['geometry']['model'] == 'rotation'
    assert wide_result['geometry']['planar_refusal']['reason'] == 'output_limit'
    assert wide_result['geometry']['focal_source'] == 'horizontal_fov_deg'
    assert any(item['row'] != item['anchor_row'] for item in wide_result['alignment'])
    # A 2×2 grid still fits the planar mosaic and publishes through it.
    plan = panorama.guided_plan(2, 2, overlap=.4, hfov_deg=hfov, vfov_deg=vfov)
    paths = _write_sphere_grid(tmp_path / 'grid', plan, env, hfov, (480, 320))
    sources = [path.read_bytes() for path in paths]
    result = panorama.build_panorama(paths, tmp_path / 'jobs', projection='cylindrical',
                                     horizontal_fov_deg=90, layout={'rows': 2, 'columns': 2})
    assert result['status'] == 'completed'
    assert result['geometry'] == {'model': 'planar'}
    assert result['layout'] == {'rows': 2, 'columns': 2, 'order': 'serpentine'}
    assert result['horizon']['level_horizon'] is True
    assert result['toolchain']['warp_executed'] is True
    assert any(item['row'] != item['anchor_row'] for item in result['alignment'])
    assert min(item['estimated_overlap_fraction'] for item in result['alignment']) >= panorama.MIN_STITCH_OVERLAP
    output = tmp_path / 'jobs' / result['job_id'] / 'panorama.jpg'
    mosaic = cv2.imread(str(output))
    assert mosaic is not None and mosaic.shape[1] > 400 and mosaic.shape[0] > 200
    assert mosaic.std() > 10
    assert sources == [path.read_bytes() for path in paths]
    # Intentional low overlap: 5% of a textured strip, not a grid.
    world = env[:180, :520].copy()
    low = tmp_path / 'low'
    low.mkdir()
    a = world[:, 0:240]
    b = world[:, 228:468]  # 12 px / 240 ≈ 5%
    pa, pb = low / 'a.jpg', low / 'b.jpg'
    assert cv2.imwrite(str(pa), a) and cv2.imwrite(str(pb), b)
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama([pa, pb], tmp_path / 'low-jobs')
    assert failed.value.reason == 'insufficient_overlap'
    job = tmp_path / 'low-jobs' / failed.value.job_id
    assert not (job / 'panorama.jpg').exists()


@pytest.mark.parametrize('projection', ['rectilinear', 'cylindrical', 'spherical'])
def test_required_projections_render_from_sphere_grid(projection, tmp_path):
    env = _equirect_texture()
    hfov, overlap = 48.0, .4
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * 240 / 360))
    plan = panorama.guided_plan(2, 2, overlap=overlap, hfov_deg=hfov, vfov_deg=vfov)
    paths = _write_sphere_grid(tmp_path, plan, env, hfov, (360, 240))
    result = panorama.build_panorama(paths, tmp_path / projection, projection=projection,
                                     layout={'rows': 2, 'columns': 2}, level_horizon=False)
    assert result['status'] == 'completed'
    assert result['projection'] == projection
    assert result['coverage']['valid_fraction'] > .9
    image = cv2.imread(str(tmp_path / projection / result['job_id'] / 'panorama.jpg'))
    assert image is not None and image.std() > 8


def test_horizon_estimate_levels_a_known_tilt():
    image = np.full((240, 420, 3), 28, np.uint8)
    cv2.line(image, (20, 90), (400, 150), (240, 240, 240), 4)
    mask = np.full((240, 420), 255, np.uint8)
    estimated, confidence, method = panorama._estimate_horizon_deg(image, mask)
    expected = -math.degrees(math.atan2(60, 380))
    assert method == 'hough_near_horizontal'
    assert confidence > .4
    assert abs(estimated - expected) < 2.5
    again, _, _ = panorama._estimate_horizon_deg(image, mask)
    assert again == estimated


def test_horizon_estimate_does_not_tilt_a_level_textured_frame(views):
    image = views[1][0]
    mask = np.full(image.shape[:2], 255, np.uint8)
    estimated, _confidence, _method = panorama._estimate_horizon_deg(image, mask)
    assert abs(estimated) < 1


def _interior_scene(roll_deg=5.0, size=(1200, 960)):
    """Synthetic room: parallel verticals plus a ceiling/floor perspective fan."""
    width, height = size
    image = np.full((height, width, 3), 28, np.uint8)
    rng = np.random.default_rng(1701)
    image = cv2.add(image, rng.integers(0, 36, (height, width, 3), dtype=np.uint8))
    for _ in range(80):
        x, y = int(rng.integers(20, width - 20)), int(rng.integers(20, height - 20))
        cv2.circle(image, (x, y), int(rng.integers(5, 16)), (20, 240, 80), 2)
    vanishing = (width // 2, int(height * 0.42))
    for x in range(-width, 2 * width, 14):
        cv2.line(image, (x, height - 1), vanishing, (50, 85, 175), 2)
        cv2.line(image, (x, 0), vanishing, (175, 75, 40), 2)
    for t in np.linspace(-0.32, 0.32, 21):
        cv2.line(image, (0, int(vanishing[1] + t * height)),
                 (width - 1, int(vanishing[1] - t * height)), (80, 80, 95), 2)
    for x in range(36, width - 36, 44):
        cv2.line(image, (x, 16), (x, height - 16), (235, 235, 235), 3)
        cv2.line(image, (x + 7, 16), (x + 7, height - 16), (200, 200, 210), 2)
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), roll_deg, 1.0)
    return cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR,
                          borderValue=(28, 28, 28))


def _write_views(directory, world, positions, size):
    width, height = size
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (x, y) in enumerate(positions):
        view = world[y:y + height, x:x + width].copy()
        path = directory / f'view{index}.jpg'
        assert cv2.imwrite(str(path), view, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(path)
    return paths


def _near_vertical_lean_deg(image):
    """Length-weighted median of near-vertical Hough lines. Independent of the stitcher bin."""
    panorama._configure_native()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    scale = min(1., 512 / max(height, width))
    if scale < 1:
        gray = cv2.resize(gray, (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                          interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    work_h, work_w = gray.shape
    min_len = max(24, min(work_h, work_w) // 4)
    panorama._configure_native()
    lines = cv2.HoughLinesP(edges, 1, math.pi / 180, threshold=max(20, min_len // 2),
                            minLineLength=min_len, maxLineGap=max(8, min_len // 8))
    samples = []
    if lines is None:
        return None
    for x0, y0, x1, y1 in lines.reshape(-1, 4):
        dx, dy = float(x1 - x0), float(y1 - y0)
        length = math.hypot(dx, dy)
        if length < min_len:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        if angle > 90:
            angle -= 180
        if angle <= -90:
            angle += 180
        if abs(angle) <= 45:
            continue
        tilt = angle - 90.0 if angle > 0 else angle + 90.0
        if abs(tilt) <= 20:
            samples.append((tilt, length))
    if not samples:
        return None
    samples.sort(key=lambda item: item[0])
    total = sum(length for _, length in samples)
    acc = 0.0
    for tilt, length in samples:
        acc += length
        if acc >= total / 2:
            return tilt
    return samples[-1][0]


def test_horizon_estimate_prefers_verticals_on_perspective_interior():
    roll = 5.0
    image = _interior_scene(roll_deg=roll)
    mask = np.full(image.shape[:2], 255, np.uint8)
    estimated, confidence, method = panorama._estimate_horizon_deg(image, mask)
    assert method == 'hough_near_vertical'
    assert confidence > .4
    assert abs(estimated - roll) < 0.5
    level, _, level_method = panorama._estimate_horizon_deg(_interior_scene(roll_deg=0), mask)
    assert abs(level) < 0.5
    assert level_method == 'hough_near_vertical'


def test_rolled_interior_grid_and_strip_level_within_half_degree(tmp_path):
    roll = 5.0
    world = _interior_scene(roll_deg=roll)
    grid_paths = _write_views(tmp_path / 'grid-src', world,
                              [(0, 0), (340, 0), (340, 280), (0, 280)], (640, 500))
    grid = panorama.build_panorama(grid_paths, tmp_path / 'grid', projection='rectilinear',
                                   layout={'rows': 2, 'columns': 2}, level_horizon=True)
    assert grid['horizon']['method'] == 'hough_near_vertical'
    assert abs(grid['horizon']['applied_deg'] - roll) < 0.5
    grid_image = cv2.imread(str(tmp_path / 'grid' / grid['job_id'] / 'panorama.jpg'))
    assert grid_image is not None
    grid_lean = _near_vertical_lean_deg(grid_image)
    assert grid_lean is not None and abs(grid_lean) < 0.5
    y = (world.shape[0] - 360) // 2
    strip_paths = _write_views(tmp_path / 'strip-src', world,
                               [(20 + i * 180, y) for i in range(4)], (520, 360))
    strip = panorama.build_panorama(strip_paths, tmp_path / 'strip', projection='rectilinear',
                                    level_horizon=True)
    assert abs(strip['horizon']['applied_deg'] - roll) < 0.5
    strip_image = cv2.imread(str(tmp_path / 'strip' / strip['job_id'] / 'panorama.jpg'))
    assert strip_image is not None
    strip_lean = _near_vertical_lean_deg(strip_image)
    assert strip_lean is not None and abs(strip_lean) < 0.5


def _level_horizon_strip(height=120):
    image = np.full((height, 512, 3), 30, np.uint8)
    cv2.line(image, (10, height - 30), (501, height - 30), (240, 240, 240), 2)
    return image


@pytest.mark.parametrize('roll', [0.0, 2.0])
@pytest.mark.parametrize('poles', [1, 2])
def test_horizon_estimate_keeps_a_clear_horizon_over_leaning_poles(roll, poles):
    image = _level_horizon_strip()
    for x in [100, 300][:poles]:
        cv2.line(image, (x, 10), (x + 8, 60), (240, 240, 240), 2)
    if roll:
        matrix = cv2.getRotationMatrix2D((256, 60), roll, 1.0)
        image = cv2.warpAffine(image, matrix, (512, 120), borderMode=cv2.BORDER_REPLICATE)
    mask = np.full(image.shape[:2], 255, np.uint8)
    estimated, _confidence, method = panorama._estimate_horizon_deg(image, mask)
    assert method == 'hough_near_horizontal'
    assert abs(estimated - roll) < 0.5


def test_horizon_estimate_weak_verticals_never_level_on_their_own():
    # One pole plus a short diagonal that keeps the horizon family below 90%.
    image = _level_horizon_strip()
    cv2.line(image, (100, 10), (108, 60), (240, 240, 240), 2)
    cv2.line(image, (290, 15), (350, 33), (240, 240, 240), 2)
    mask = np.full(image.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(image, mask)[0]) < 0.5
    # A pole with no horizon at all.
    lone = np.full((120, 512, 3), 30, np.uint8)
    cv2.line(lone, (100, 10), (108, 110), (240, 240, 240), 2)
    assert panorama._estimate_horizon_deg(lone, mask)[0] == 0


def test_horizon_estimate_weak_verticals_veto_a_disagreeing_perspective_line():
    # Level room: two plumb door edges, one long receding shelf line and a short diagonal.
    image = np.full((320, 512, 3), 30, np.uint8)
    for x in [150, 350]:
        cv2.line(image, (x, 40), (x, 280), (240, 240, 240), 2)
    cv2.line(image, (10, 100), (500, 151), (240, 240, 240), 2)
    cv2.line(image, (50, 250), (200, 210), (240, 240, 240), 2)
    mask = np.full(image.shape[:2], 255, np.uint8)
    estimated, _confidence, method = panorama._estimate_horizon_deg(image, mask)
    assert (estimated, method) == (0.0, 'undetermined')


def test_horizon_estimate_does_not_count_pole_edges_or_fragments_as_separate_lines():
    # Both silhouette edges of two wide leaning poles.
    image = _level_horizon_strip()
    for x in [100, 300]:
        cv2.line(image, (x, 10), (x + 8, 60), (240, 240, 240), 16)
    mask = np.full(image.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(image, mask)[0]) < 0.5
    # One thin leaning line broken into Hough fragments on a narrow raster.
    narrow = np.full((512, 64, 3), 30, np.uint8)
    cv2.line(narrow, (10, 10), (34, 450), (240, 240, 240), 1)
    for top in [90, 185, 280, 375]:
        narrow[top:top + 15] = 30
    cv2.line(narrow, (5, 485), (58, 485), (240, 240, 240), 2)
    narrow_mask = np.full(narrow.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(narrow, narrow_mask)[0]) < 0.5


@pytest.mark.xfail(strict=True, reason='open: 28 px poles show 4 edges >= 24 px apart, counted as a rolled room (+9°)')
def test_horizon_estimate_open_two_very_wide_leaning_poles():
    image = np.full((320, 512, 3), 30, np.uint8)
    cv2.line(image, (10, 290), (501, 290), (240, 240, 240), 2)
    for x in [100, 300]:
        cv2.line(image, (x, 30), (x + 32, 230), (240, 240, 240), 28)
    mask = np.full(image.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(image, mask)[0]) < 0.5


@pytest.mark.xfail(strict=True, reason='open: an off-centre pitched facade is a one-sided keystone fan (+2.25°)')
def test_horizon_estimate_open_pitched_facade_with_level_ground_line():
    # Pinhole f=700 px, 12° pitch, zero roll, facade at Z=20 left of centre (codex round 3).
    image = np.full((320, 512, 3), 30, np.uint8)
    for top, bottom in [((44, 47), (60, 312)), ((79, 47), (93, 312)), ((114, 47), (125, 312)),
                        ((150, 47), (158, 312)), ((185, 47), (191, 312))]:
        cv2.line(image, top, bottom, (240, 240, 240), 2)
    cv2.line(image, (27, 312), (419, 312), (240, 240, 240), 2)
    mask = np.full(image.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(image, mask)[0]) < 0.5


@pytest.mark.xfail(strict=True, reason='open: one leaning pole vetoes a correct 5° horizon held at 89% agreement')
def test_horizon_estimate_open_rolled_horizon_with_pole_and_diagonal():
    image = _level_horizon_strip()
    cv2.line(image, (100, 10), (108, 60), (240, 240, 240), 2)
    cv2.line(image, (290, 15), (350, 33), (240, 240, 240), 2)
    matrix = cv2.getRotationMatrix2D((256, 60), 5.0, 1.0)
    image = cv2.warpAffine(image, matrix, (512, 120), borderMode=cv2.BORDER_REPLICATE)
    mask = np.full(image.shape[:2], 255, np.uint8)
    assert abs(panorama._estimate_horizon_deg(image, mask)[0] - 5.0) < 0.5


@pytest.mark.parametrize('horizon', [True, False])
@pytest.mark.parametrize('pairs', [1, 2, 3, 5])
def test_horizon_estimate_does_not_level_to_one_side_of_a_keystone_fan(pairs, horizon):
    image = _level_horizon_strip(height=320) if horizon else np.full((320, 512, 3), 30, np.uint8)
    for index in range(pairs):
        x = 60 + 30 * index
        cv2.line(image, (x, 40), (x - 20, 220), (240, 240, 240), 2)
        cv2.line(image, (511 - x, 40), (511 - x + 20, 220), (240, 240, 240), 2)
    mask = np.full(image.shape[:2], 255, np.uint8)
    estimated, _confidence, _method = panorama._estimate_horizon_deg(image, mask)
    assert abs(estimated) < 0.5


@pytest.mark.parametrize('horizon', [True, False])
@pytest.mark.parametrize('skew', [0, 8, 16])
def test_horizon_estimate_ignores_a_skewed_coverage_boundary(skew, horizon):
    image = np.full((120, 512, 3), 140, np.uint8)
    if horizon:
        cv2.line(image, (10, 85), (500, 85), (250, 250, 250), 2)
    mask = np.zeros((120, 512), np.uint8)
    cv2.fillConvexPoly(mask, np.int32([[30, 0], [470, 0], [470 + skew, 119], [30 + skew, 119]]), 255)
    estimated, _confidence, _method = panorama._estimate_horizon_deg(image, mask)
    assert abs(estimated) < 0.5
    # A mosaic whose top and bottom coverage edges slope.
    sloped = np.zeros((120, 512), np.uint8)
    cv2.fillConvexPoly(sloped, np.int32([[0, 20], [511, 20 + skew], [511, 100 + skew], [0, 100]]), 255)
    estimated, _confidence, _method = panorama._estimate_horizon_deg(np.full((120, 512, 3), 140, np.uint8), sloped)
    assert abs(estimated) < 0.5


def test_horizon_leveling_and_manual_crop_preserve_originals(tmp_path):
    env = _equirect_texture()
    hfov, overlap = 50.0, .4
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * 320 / 480))
    plan = panorama.guided_plan(2, 2, overlap=overlap, hfov_deg=hfov, vfov_deg=vfov)
    paths = _write_sphere_grid(tmp_path, plan, env, hfov, (480, 320), roll_deg=8)
    before = [path.read_bytes() for path in paths]
    leveled = panorama.build_panorama(paths, tmp_path / 'leveled', projection='cylindrical',
                                      layout={'rows': 2, 'columns': 2}, level_horizon=True)
    tilted = panorama.build_panorama(paths, tmp_path / 'tilted', projection='cylindrical',
                                     layout={'rows': 2, 'columns': 2}, level_horizon=False)
    assert abs(leveled['horizon']['applied_deg']) > 3
    cropped = panorama.build_panorama(paths, tmp_path / 'crop', projection='cylindrical',
                                      layout={'rows': 2, 'columns': 2}, level_horizon=False,
                                      crop=[.1, .1, .9, .9])
    assert cropped['output']['width'] < tilted['output']['width']
    assert cropped['output']['height'] < tilted['output']['height']
    assert cropped['manual_crop'] == [.1, .1, .9, .9]
    assert before == [path.read_bytes() for path in paths]
    with pytest.raises(ValueError, match='too small'):
        panorama.build_panorama(paths, tmp_path / 'tiny', crop=[0, 0, .04, 1])
    uncovered = panorama.build_panorama(paths, tmp_path / 'full', projection='cylindrical',
                                        layout={'rows': 2, 'columns': 2}, autocrop=False,
                                        level_horizon=False)
    assert uncovered['coverage']['valid_fraction'] < 1
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama(paths, tmp_path / 'bad-crop', projection='cylindrical',
                                layout={'rows': 2, 'columns': 2}, autocrop=False,
                                level_horizon=False, crop=[0, 0, .2, .2])
    assert failed.value.reason == 'invalid_crop'


# --- Panned tripod sweeps: one rotation per frame when a plane cannot hold them ------

A7_HFOV = math.degrees(2 * math.atan(18 / 24))  # 24 mm on a 36 mm-wide frame


def _save_with_focal(path, bgr, focal_35mm=24):
    """JPEG with EXIF FocalLengthIn35mmFilm, the tag Sony bodies write."""
    from PIL import Image
    image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[41989] = focal_35mm
    image.save(path, quality=97, exif=exif.tobytes())
    return path


def _card_texture():
    rng = np.random.default_rng(7)
    card = rng.integers(30, 220, (400, 400, 3), dtype=np.uint8)
    for _ in range(120):
        cv2.circle(card, (int(rng.integers(0, 400)), int(rng.integers(0, 400))), int(rng.integers(5, 20)),
                   tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return card


def _render_with_near_card(env, card, yaw_deg, pitch_deg, pupil_offset_m, width=600, height=400,
                           depth_m=.6, half_m=.35):
    """Pinhole view of a far sphere plus a textured card depth_m ahead. The entrance pupil
    sits pupil_offset_m in front of the rotation axis: 0 is a nodal pan, >0 adds parallax."""
    view = render_sphere_view(env, yaw_deg, pitch_deg, 0, A7_HFOV, width, height)
    ys, xs = np.indices((height, width), dtype=np.float64)
    focal = (width - 1) / (2 * math.tan(math.radians(A7_HFOV) / 2))
    rays = np.stack([(xs - (width - 1) / 2) / focal, -((ys - (height - 1) / 2) / focal), np.ones_like(xs)], -1)
    rotation = _rotation(yaw_deg, pitch_deg, 0)
    world = rays @ rotation.T
    centre = rotation @ np.array([0, 0, pupil_offset_m])
    t = (depth_m - centre[2]) / world[..., 2]
    hit_x, hit_y = centre[0] + t * world[..., 0], centre[1] + t * world[..., 1]
    hit = (t > 0) & (np.abs(hit_x) < half_m) & (np.abs(hit_y) < half_m)
    size = card.shape[0] - 1
    map_x = ((hit_x / half_m + 1) * .5 * size).astype(np.float32)
    map_y = ((1 - hit_y / half_m) * .5 * size).astype(np.float32)
    near = cv2.remap(card, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    view[hit] = near[hit]
    return view


def test_panned_strip_past_the_planar_limit_stitches_level_from_exif_focal(tmp_path):
    """45° pans at 24 mm, like the first real A7 III sweep: the planar mosaic refuses them."""
    env = _equirect_texture()
    paths = [_save_with_focal(tmp_path / f'pan{i}.jpg', render_sphere_view(env, yaw, 0, 0, A7_HFOV, 600, 400))
             for i, yaw in enumerate((-45, 0, 45))]
    before = [path.read_bytes() for path in paths]
    result = panorama.build_panorama(paths, tmp_path / 'jobs', projection='spherical')
    geometry = result['geometry']
    assert geometry['model'] == 'rotation'
    assert geometry['planar_refusal']['reason'] in {'insufficient_overlap', 'output_limit', 'geometry_rejected'}
    assert geometry['focal_source'] == 'exif_focal_length_35mm'
    assert geometry['working_focal_pixels'] == pytest.approx(400, abs=.01)
    assert result['algorithm'] == 'sift-ransac-rotation-sweep-surface-projection-v1'
    assert [item['anchor_frame'] for item in result['alignment']] == [0, 1]
    assert max(item['median_reprojection_error_pixels'] for item in result['alignment']) < 1
    assert all(item['focal_length_35mm'] == 24 for item in result['inputs'])
    assert result['horizon']['method'] == 'wave_correct'
    assert geometry['up_axis_change_deg'] < .5
    assert result['coverage']['valid_fraction'] == 1
    # 90° of pan plus one 73.7° view at 400 px/rad spans ~1140 px of sphere.
    assert 1000 < result['output']['width'] < 1200
    output = cv2.imread(str(tmp_path / 'jobs' / result['job_id'] / 'panorama.jpg'))
    # The equator line stays one straight, level row across all three frames.
    white = (output.min(axis=2) > 225)
    columns = [np.flatnonzero(white[:, x]) for x in range(20, output.shape[1] - 20, 40)]
    rows = [float(np.median(hits)) for hits in columns if 0 < len(hits) < 15]
    assert len(rows) > 20 and max(rows) - min(rows) <= 2
    assert before == [path.read_bytes() for path in paths]


def test_panned_two_by_three_grid_stitches_with_row_links(tmp_path):
    env = _equirect_texture()
    poses = [(-45, 17), (0, 17), (45, 17), (45, -18), (0, -18), (-45, -18)]  # serpentine
    paths = [_save_with_focal(tmp_path / f'g{i}.jpg', render_sphere_view(env, yaw, pitch, 0, A7_HFOV, 600, 400))
             for i, (yaw, pitch) in enumerate(poses)]
    result = panorama.build_panorama(paths, tmp_path / 'jobs', projection='spherical',
                                     layout={'rows': 2, 'columns': 3})
    assert result['geometry']['model'] == 'rotation'
    assert result['layout'] == {'rows': 2, 'columns': 3, 'order': 'serpentine'}
    assert any(item['row'] != item['anchor_row'] for item in result['alignment'])
    checks = result['geometry']['loop_checks']
    assert checks and all(check['agreeing_matches'] >= check['required_matches'] for check in checks)
    assert result['output']['width'] > 900 and result['output']['height'] > 400


def test_nodal_sweep_with_near_object_stitches_but_parallax_is_refused(tmp_path):
    env, card = _equirect_texture(), _card_texture()
    poses = [(-20, 15), (20, 15), (20, -15), (-20, -15)]
    nodal = [_save_with_focal(tmp_path / f'nodal{i}.jpg', _render_with_near_card(env, card, yaw, pitch, 0))
             for i, (yaw, pitch) in enumerate(poses)]
    result = panorama.build_panorama(nodal, tmp_path / 'nodal', projection='spherical',
                                     layout={'rows': 2, 'columns': 2})
    assert result['geometry']['model'] == 'rotation'
    # The same scene with the lens 15 cm ahead of the pan axis: the card shifts against the sphere.
    offset = [_save_with_focal(tmp_path / f'offset{i}.jpg', _render_with_near_card(env, card, yaw, pitch, .15))
              for i, (yaw, pitch) in enumerate(poses)]
    with pytest.raises(panorama.PanoramaError) as failed:
        panorama.build_panorama(offset, tmp_path / 'offset', projection='spherical',
                                layout={'rows': 2, 'columns': 2})
    assert failed.value.reason == 'parallax'
    assert 'Frames 2 and 1' in str(failed.value) and 'parallax' in str(failed.value)
    assert not (tmp_path / 'offset' / failed.value.job_id / 'panorama.jpg').exists()


def test_rotation_path_keeps_refusing_static_and_disjoint_views(tmp_path):
    env = _equirect_texture()
    # A repeated framing (half a degree apart) is still not a panorama.
    static = [_save_with_focal(tmp_path / f'static{i}.jpg', render_sphere_view(env, yaw, 0, 0, A7_HFOV, 600, 400))
              for i, yaw in enumerate((0, .5))]
    with pytest.raises(panorama.PanoramaError) as repeated:
        panorama.build_panorama(static, tmp_path / 'static')
    assert repeated.value.reason == 'insufficient_new_coverage'
    # Opposite directions share nothing: the frame is named with its best candidate's numbers.
    disjoint = [_save_with_focal(tmp_path / f'far{i}.jpg', render_sphere_view(env, yaw, 0, 0, A7_HFOV, 600, 400))
                for i, yaw in enumerate((0, 180))]
    with pytest.raises(panorama.PanoramaError) as apart:
        panorama.build_panorama(disjoint, tmp_path / 'disjoint')
    assert apart.value.reason == 'insufficient_overlap'
    assert str(apart.value).startswith('Frame 1 cannot align reliably: its best candidate, frame 0')
    rng = np.random.default_rng(81)
    with pytest.raises(panorama.PanoramaError, match='overlap'):
        panorama._align_rotation([rng.integers(0, 256, (250, 400, 3), dtype=np.uint8) for _ in range(2)], 300.)


def test_focal_from_35mm_uses_the_frame_diagonal():
    assert panorama._focal_from_35mm(24, 6000, 4000) == pytest.approx(4000)
    assert panorama._focal_from_35mm(24, 4000, 3000) == pytest.approx(24 * 5000 / math.hypot(36, 24))
    assert panorama._focal_from_35mm(None, 6000, 4000) is None
    assert panorama._focal_from_35mm(0, 6000, 4000) is None
    assert panorama._focal_from_35mm(True, 6000, 4000) is None
    assert panorama._focal_from_35mm(float('nan'), 6000, 4000) is None
    from PIL.TiffImagePlugin import IFDRational
    assert panorama._focal_from_35mm(IFDRational(24, 1), 600, 400) == pytest.approx(400)


def test_rotation_render_gives_every_covered_pixel_a_weight():
    """Resized seam masks can miss a covered pixel; it must still carry its tile's colour."""
    def rotation(yaw, pitch, roll):
        return (cv2.Rodrigues(np.float64([0, math.radians(yaw), 0]))[0]
                @ cv2.Rodrigues(np.float64([math.radians(pitch), 0, 0]))[0]
                @ cv2.Rodrigues(np.float64([0, 0, math.radians(roll)]))[0])
    images = [np.full((1000, 1500, 3), 180, np.uint8) for _ in range(3)]
    rotations = [rotation(0, 0, 0), rotation(45, -4.896335, -7.774676), rotation(90, 8.834026, -13.671517)]
    rotations, _method, _up = panorama._level_rotations(rotations, True, 0)
    output, coverage, *_ = panorama._render_rotation(images, rotations, 1000., 'spherical', False)
    covered = output[coverage == 255]
    assert covered.size and covered.min() >= 178 and covered.max() <= 182


def test_cli_layout_accepts_the_session_object_and_the_older_pair():
    import argparse
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_panorama_cli', Path(__file__).resolve().parents[1] / 'tools' / 'build_panorama.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli._layout_arg('{"rows": 2, "columns": 3}') == {'rows': 2, 'columns': 3}
    assert cli._layout_arg('{"rows": 2, "columns": 3, "order": "serpentine"}') == {'rows': 2, 'columns': 3}
    assert cli._layout_arg('2,3') == {'rows': 2, 'columns': 3}
    for bad in ('{"rows": 2', '{"rows": 2, "columns": 3, "order": "raster"}', '{"rows": 2, "cols": 3}', '2x3',
                '{}', '{"rows": 2}', '{"rows": true, "columns": 3}', '{"rows": 1.5, "columns": 2}',
                '{"rows": 0, "columns": 3}', '{"rows": 4, "columns": 4}', '1,1'):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._layout_arg(bad)


@pytest.mark.parametrize('level_horizon', [True, False])
def test_half_circle_sweep_is_centred_away_from_the_wrap_seam(tmp_path, level_horizon):
    env = _equirect_texture()
    paths = [_save_with_focal(tmp_path / f'half{i}.jpg', render_sphere_view(env, yaw, 0, 0, A7_HFOV, 600, 400))
             for i, yaw in enumerate((0, 45, 90, 135, 180))]
    result = panorama.build_panorama(paths, tmp_path / 'jobs', projection='spherical', level_horizon=level_horizon)
    assert result['geometry']['model'] == 'rotation'
    # 180° of pan plus one 73.7° view at 400 px/rad is ~1770 px; the ±180° seam would split it.
    assert result['output']['width'] > 1600
    assert result['coverage']['valid_fraction'] == 1


def test_sweep_centre_puts_the_seam_in_the_widest_gap_and_is_stable():
    def yawed(deg, pitch=0.):
        return _rotation(deg, pitch, 0)
    uneven = [yawed(deg) for deg in (0, 15, 30, 45, 60, 75, 90, 135, 180, 225, 270)]
    # Views cover 0..270°: the widest gap is 270..360, so the centre is the middle, 135°.
    assert math.degrees(panorama._sweep_centre_yaw(uneven)) == pytest.approx(135)
    even = [yawed(deg) for deg in range(0, 360, 45)]
    nudged = [yawed(deg + (1e-8 if deg == 315 else 0)) for deg in range(0, 360, 45)]
    first = panorama._sweep_centre_yaw(even)
    assert panorama._sweep_centre_yaw(nudged) == pytest.approx(first, abs=1e-6)
    assert abs(math.degrees(math.remainder(first, 2 * math.pi))) <= 22.5 + 1e-6  # stays next to frame 0
    assert panorama._sweep_centre_yaw([yawed(0, 90), yawed(0, -90)]) == 0.0
    assert panorama._sweep_centre_yaw([yawed(0, 90 - 1e-8), yawed(20, 90 + 1e-8)]) == 0.0
    assert math.degrees(panorama._sweep_centre_yaw([yawed(10), yawed(30)])) == pytest.approx(20)
    # One column shot in two rows repeats a heading; that is not a 360° gap.
    repeated = [yawed(0, 10), yawed(0, -10), yawed(45), yawed(90), yawed(135), yawed(180)]
    assert math.degrees(panorama._sweep_centre_yaw(repeated)) == pytest.approx(90)
    assert math.degrees(panorama._sweep_centre_yaw([yawed(30), yawed(30, 20)])) == pytest.approx(30)
    # A full circle nudged either way keeps the same side of frame 0.
    circle = lambda last: [yawed(deg) for deg in (0, 45, 90, 135, last, 225, 270, 315)]
    assert panorama._sweep_centre_yaw(circle(179.999)) == pytest.approx(panorama._sweep_centre_yaw(circle(180.001)), abs=1e-4)
