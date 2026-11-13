"""Isolated development child. No sockets, camera control, or source mutation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import resource
import signal
import sys

from raw_colour import linear_tiff_bytes_for, read_linear_rgb_tile, write_tiff_header


def main(request_path):
    os.umask(0o077)
    request = json.loads(Path(request_path).read_text())
    cpu_seconds = max(1, math.ceil(request['seconds']))
    signal.alarm(cpu_seconds + 1)  # Still expires if the supervising app exits.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (request['memory_bytes'], request['memory_bytes']))
    import numpy as np
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = request['max_pixels']
    source = Path(request['source'])
    root = Path(request_path).parent
    camera_linear = request.get('camera_linear')
    raw = bool(camera_linear)
    linear_file = None
    if raw:
        if not sys.platform.startswith('linux'):
            raise ValueError('RAW requires hard address-space enforcement.')
        width, height = int(request['linear_width']), int(request['linear_height'])
        if min(width, height) < 1 or width * height > request['max_pixels']:
            raise ValueError('RAW dimensions exceed pixel budget.')
        linear_path = Path(camera_linear)
        if linear_path.is_symlink() or not linear_path.is_file():
            raise ValueError('RAW requires hard address-space enforcement.')
        if linear_path.stat().st_size != linear_tiff_bytes_for(width, height):
            raise ValueError('Pre-rendered linear TIFF is invalid.')
        linear_file = open(linear_path, 'rb')
        input_note = 'Isolated LibRaw unpack; camera as-shot WB and RGB->XYZ; linear 16-bit sRGB, no auto brightness.'
    else:
        if source.suffix.lower() not in {'.jpg', '.jpeg'}:
            raise ValueError('RAW requires hard address-space enforcement.')
        with Image.open(source) as opened:
            if opened.format not in {'JPEG', 'MPO'} or opened.width * opened.height > request['max_pixels']:
                raise ValueError('JPEG source or dimensions are invalid.')
            # Sony JPEGs may contain an MPO secondary thumbnail; develop only primary frame.
            opened.seek(0)
            image = ImageOps.exif_transpose(opened).convert('RGB')
        width, height = image.size
        input_note = 'Decoded JPEG treated as sRGB. Clipped JPEG highlights cannot be recovered; a 16-bit output does not restore lost source precision.'
    if not 0 < width * height <= request['max_pixels']:
        raise ValueError('Invalid developed dimensions.')
    output = Image.new('RGB', (width, height))
    options = request['options']
    try:
        with (root / 'linear.tiff').open('wb') as tiff:
            write_tiff_header(tiff, width, height)
            for top in range(0, height, 128):
                bottom = min(top + 128, height)
                if raw:
                    linear = read_linear_rgb_tile(linear_file, width, top, bottom)
                else:
                    encoded = np.asarray(image.crop((0, top, width, bottom)), dtype=np.float32) / 255
                    linear = np.where(encoded <= .04045, encoded / 12.92, ((encoded + .055) / 1.055) ** 2.4)
                linear *= 2 ** options['exposure_ev']
                linear *= np.array([options['wb_red'], 1, options['wb_blue']], dtype=np.float32)
                # Shoulder compresses existing bright signal; cannot reconstruct lost detail.
                h = options['highlight_compression']
                linear /= 1 + h * np.maximum(linear - .5, 0)
                linear = np.clip(linear, 0, 1)
                # Bounded shadow lift in linear light, zero strength is identity.
                linear = (1 - options['tone_strength']) * linear + options['tone_strength'] * np.sqrt(linear)
                tiff.write(np.rint(linear * 65535).astype('<u2').tobytes())
                encoded = np.where(linear <= .0031308, 12.92 * linear, 1.055 * np.power(linear, 1 / 2.4) - .055)
                output.paste(Image.fromarray(np.rint(np.clip(encoded, 0, 1) * 255).astype('uint8')), (0, top))
    finally:
        if linear_file is not None:
            linear_file.close()
    save = dict(quality=95, subsampling=0)
    identity = request.get('source_identity') or {}
    if raw and (identity.get('make') or identity.get('model')):
        exif = Image.Exif()
        if identity.get('make'):
            exif[271] = str(identity['make'])[:80]
        if identity.get('model'):
            exif[272] = str(identity['model'])[:80]
        save['exif'] = exif
    output.save(root / 'developed.jpg', **save)
    preview = output.copy()
    preview.thumbnail((1280, 1280))
    preview.save(root / 'preview.jpg', quality=95, subsampling=0)
    # Independently parse/decode both encodings before declaring success.
    with Image.open(root / 'linear.tiff') as check:
        assert check.size == (width, height) and tuple(check.tag_v2[258]) == (16, 16, 16)
        check.load()
    with Image.open(root / 'developed.jpg') as check:
        check.load()
        assert check.size == (width, height)
    render = {'width': width, 'height': height,
        'source_kind': 'raw' if raw else 'jpeg', 'algorithm': 'linear-development-v1',
        'neural': False, 'input_note': input_note,
        'linear_encoding': '16-bit unsigned linear sRGB RGB TIFF; not mosaiced sensor RAW or DNG',
        'preview_policy': 'Preview is downsampled from the same developed RGB pixels as final JPEG.',
        'metadata_policy': ('Photographic Make/Model copied to JPEG; GPS omitted. Original remains in source.'
                            if raw else 'Original metadata remains in source; output omits EXIF/GPS.')}
    if request.get('colour'):
        render['colour'] = request['colour']
    if identity:
        render['source_identity'] = identity
    (root / 'render.json').write_text(json.dumps(render))


if __name__ == '__main__':
    main(sys.argv[1])
