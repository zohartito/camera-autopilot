"""Hash explicitly selected existing local artifacts, without downloads or copying."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def build_manifest(root: Path, relative_paths: list[str]) -> dict:
    root = root.resolve()
    entries = []
    if not relative_paths or len(relative_paths) > 1024 or len(set(relative_paths)) != len(relative_paths):
        raise ValueError("Select 1..1024 unique artifact paths")
    for relative in relative_paths:
        path = root / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
            raise ValueError("Only explicit local regular artifact files are accepted")
        path.resolve().relative_to(root)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Artifact must be a regular file")
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Artifact changed during hashing")
        entries.append({'path': relative, 'size_bytes': before.st_size, 'sha256': digest.hexdigest()})
    return {'schema': 'camera-offline-artifacts-v1', 'artifacts': entries,
            'scope': 'Explicitly selected local files only; does not certify target runtime compatibility'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--artifact', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build_manifest(args.root, args.artifact)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(args.output), 'artifacts': len(manifest['artifacts']),
                      'downloads_attempted': False, 'target_runtime_verified': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
