"""Where the bring-up kit may write receipts, staged config and credentials. Stdlib only.

A receipt or staging directory is refused when it resolves:
  * inside the repository (a staged AP passphrase or receipt there could be committed), or
  * inside a system directory such as /etc, /boot or /usr, unless the caller runs with
    --apply AND passes --allow-system-dir explicitly. A system root itself (``/etc``) and ``/``
    are refused even then, because the kit chmods the directories it creates.

Callers resolve ONCE with ``resolve_output`` (``~`` expansion plus realpath) and use that same path
for the check and for every write, so a quoted ``~`` or a relative path can never be checked in one
place and written in another. Containment is judged by string AND by ``os.path.samefile`` against
every existing ancestor, so a differently-cased spelling on a case-insensitive filesystem, or a
symlinked or hard-linked directory, cannot slip past.

``python3 deploy/safe_paths.py --repo REPO [--allow-system-dir] [--print-resolved] PATH`` exits 2
with the reason on refusal and 0 otherwise. It only reads the filesystem; it creates nothing.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # "writes nothing" includes __pycache__

import argparse  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

SYSTEM_DIRS = ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/opt", "/srv",
               "/proc", "/sys", "/dev", "/run", "/var/lib", "/var/log", "/var/cache", "/var/spool",
               "/var/db", "/private/etc", "/System", "/Library")


def control_character(path: Path | str) -> str | None:
    """A control character (newline included) in a path is refused outright: "/boot\\n" would be
    validated as one path and, after shell command substitution strips the newline, written as another."""
    for char in str(path):
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return f"refusing a path containing the control character {char!r}: {str(path)!r}"
    return None


def path_arg(text: str) -> Path:
    """argparse ``type=`` for every path argument in the kit."""
    problem = control_character(text)
    if problem:
        raise argparse.ArgumentTypeError(problem)
    return Path(text)


def path_text(text: str) -> str:
    """Same check for a path argument the caller keeps as a string."""
    return str(path_arg(text))


def resolve_output(path: Path | str) -> Path:
    """The one spelling of an output path that is both checked and written."""
    return Path(os.path.realpath(Path(path).expanduser().absolute()))


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _self_and_existing_ancestors(target: Path) -> list[Path]:
    return [p for p in (target, *target.parents) if os.path.exists(p)]


def _inside(target: Path, chain: list[Path], directory: Path) -> bool:
    return _within(target, directory) or any(_same(p, directory) for p in chain)


def refusal(path: Path | str, repo: Path | str, allow_system: bool = False) -> str | None:
    """Return why ``path`` may not be written, or None when it may."""
    for candidate in (path, repo):
        problem = control_character(candidate)
        if problem:
            return problem
    target = resolve_output(path)
    if control_character(target):  # a clean path can still be a symlink to "receipts\n"
        return f"refusing {str(path)!r}: it resolves to {str(target)!r}, which contains a control character"
    chain = _self_and_existing_ancestors(target)
    if target == Path("/") or _same(target, Path("/")):
        return "refusing the filesystem root as an output directory"
    root = resolve_output(repo)
    if _inside(target, chain, root):
        return (f"refusing {target}: it is inside the repository ({root}); receipts and staged "
                "credentials must live outside the repo tree")
    for name in SYSTEM_DIRS:
        system = resolve_output(name)
        if target == system or _same(target, system):
            return f"refusing {target}: a system directory itself is never an output directory"
        if _inside(target, chain, system) and not allow_system:
            return (f"refusing {target}: it is inside the system directory {system}; pass --apply "
                    "and --allow-system-dir to name it explicitly")
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=path_arg, required=True)
    parser.add_argument("--allow-system-dir", action="store_true")
    parser.add_argument("--print-resolved", action="store_true",
                        help="on success print the resolved path the caller must write to")
    parser.add_argument("path", type=path_arg)
    args = parser.parse_args(argv)
    reason = refusal(args.path, args.repo, args.allow_system_dir)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    if args.print_resolved:
        print(resolve_output(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
