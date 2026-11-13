#!/usr/bin/env python3
"""Record the installed Python runtime closure, exact pins and available licenses.

This records installed artifacts; it does not certify redistribution rights or
supply wheel hashes. Run with the interpreter that will launch the candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOTS = [
    "fastapi", "uvicorn[standard]", "defusedxml", "gphoto2", "pillow", "astral",
    "mcp[cli]", "PyYAML", "numpy", "opencv-contrib-python", "torch", "torchvision",
    "open_clip_torch",
]


def collect(roots=ROOTS):
    pending = [Requirement(value) for value in roots]
    examined = set()
    distributions = {}
    while pending:
        requirement = pending.pop()
        key = canonicalize_name(requirement.name)
        state = (key, tuple(sorted(requirement.extras)))
        if state in examined:
            continue
        examined.add(state)
        distribution = metadata.distribution(requirement.name)
        if distribution.version not in requirement.specifier:
            raise ValueError(f"Installed {key}=={distribution.version} does not satisfy {requirement}")
        distributions[key] = distribution
        for value in distribution.requires or []:
            child = Requirement(value)
            contexts = {""} | set(requirement.extras)
            if child.marker is None or any(child.marker.evaluate({"extra": extra}) for extra in contexts):
                pending.append(child)
    return distributions


def snapshot(output: Path):
    distributions = collect()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    license_sections = []
    for key, dist in sorted(distributions.items()):
        texts = []
        for relative in sorted(dist.files or [], key=str):
            parts = Path(relative).parts
            name = Path(relative).name.lower()
            if not any(part.endswith('.dist-info') for part in parts):
                continue
            if not (name.startswith(('license', 'copying', 'notice')) or 'licenses' in parts):
                continue
            path = Path(dist.locate_file(relative))
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            data = path.read_bytes()
            try:
                content = data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            texts.append({'file': str(relative), 'sha256': hashlib.sha256(data).hexdigest()})
            license_sections.append(f"\n{'=' * 72}\n{dist.metadata['Name']} {dist.version}: {relative}\n{'=' * 72}\n{content}\n")
        expression = dist.metadata.get('License-Expression')
        legacy = dist.metadata.get('License') or ''
        classifiers = [v for v in dist.metadata.get_all('Classifier', []) if v.startswith('License ::')]
        rows.append({'name': dist.metadata['Name'], 'version': dist.version,
                     'license_expression': expression, 'license_metadata': legacy,
                     'license_classifiers': classifiers, 'license_files': texts})
    inventory = {'python': platform.python_version(), 'system': platform.system(),
                 'machine': platform.machine(), 'roots': ROOTS, 'packages': rows,
                 'scope': 'Installed Python runtime dependency closure; excludes development tests, model weights, datasets, native/system libraries and iOS/npm artifacts.'}
    (output / 'python-runtime-inventory.json').write_text(json.dumps(inventory, indent=2, sort_keys=True) + '\n')
    (output / 'python-runtime-licenses.txt').write_text('Installed distribution license texts; see inventory for scope and hashes.\n' + ''.join(license_sections))
    lock = '# Observed installed runtime, ' + platform.python_version() + ' ' + platform.system() + ' ' + platform.machine() + '\n'
    lock += '# Exact version pins; wheel hashes and native/system libraries are not included.\n'
    lock += '\n'.join(f"{dist.metadata['Name']}=={dist.version}" for _, dist in sorted(distributions.items())) + '\n'
    (output / 'requirements-runtime.lock').write_text(lock)
    print(json.dumps({'package_count': len(rows), 'missing_license_texts': [r['name'] for r in rows if not r['license_files']], 'output': str(output)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    snapshot(args.output)
