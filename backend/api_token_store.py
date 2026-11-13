"""Per-user persistent API token for run.sh, so a paired phone survives a server restart.

The token lives in one private file (macOS: ~/Library/Application Support/camera-autopilot/api-token;
elsewhere: ${XDG_CONFIG_HOME:-~/.config}/camera-autopilot/api-token). It is created once with
32 random bytes as hex, reused on every launch and replaced only on an explicit rotation.

The path is walked one directory at a time from the trusted base (HOME, or XDG_CONFIG_HOME when set)
with O_NOFOLLOW descriptors: every directory below the base must be a real directory owned by this
user and not writable by group/other, and the final one private (0700). Every file access then goes
through that directory's descriptor, and the file is opened with O_NOFOLLOW: a file that is not a regular file, is owned by
someone else or grants any group/other permission is refused, never repaired. Creation and rotation
write a 0600 temporary file in that directory first and then link/rename it into place, so no reader
ever sees a partial token and two launchers racing to create it end up with one token.

CLI (used by run.sh): ``python api_token_store.py [--load | --rotate]`` prints ``<state> <token>`` to stdout,
where state is created, reused or rotated. The token is never written anywhere else.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import secrets
import stat
import sys
from pathlib import Path

APP_DIR_NAME = "camera-autopilot"
TOKEN_NAME = "api-token"
TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_TOKEN_FILE_BYTES = 4096


class TokenStoreError(RuntimeError):
    """The stored token cannot be used safely; the message never contains the token."""


def store_location(env=None, system: str | None = None) -> tuple[Path, tuple[str, ...]]:
    """(trusted base, directory names below it). The base is chosen by the user: HOME or XDG_CONFIG_HOME."""
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    home = Path(env.get("HOME") or Path.home())
    if system == "Darwin":
        return home, ("Library", "Application Support", APP_DIR_NAME)
    xdg = env.get("XDG_CONFIG_HOME", "")
    # The XDG spec says a relative value is invalid and must be ignored.
    if xdg and Path(xdg).is_absolute():
        return Path(xdg), (APP_DIR_NAME,)
    return home, (".config", APP_DIR_NAME)


def default_store_path(env=None, system: str | None = None) -> Path:
    base, parts = store_location(env, system)
    return base.joinpath(*parts, TOKEN_NAME)


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def _check_dir(dir_fd: int, where: Path, *, private: bool) -> None:
    info = os.fstat(dir_fd)
    problem = None
    if not stat.S_ISDIR(info.st_mode):
        problem = "is not a directory"
    elif info.st_uid != os.geteuid():
        problem = "is not owned by this user"
    elif private and info.st_mode & 0o077:
        problem = f"has mode {stat.S_IMODE(info.st_mode):04o}; it must be 0700"
    elif not private and info.st_mode & 0o022:
        problem = f"has mode {stat.S_IMODE(info.st_mode):04o}; it must not be writable by group or others"
    if problem:
        raise TokenStoreError(f"Token directory {where} {problem}.")


def _open_private_dir(base: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """Walk base/parts with O_NOFOLLOW below the base; return a descriptor of the final directory."""
    if create:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_fd = os.open(base, _DIR_FLAGS)
    where = base
    try:
        _check_dir(dir_fd, where, private=False)
        for index, name in enumerate(parts):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=dir_fd)
                except FileExistsError:
                    pass
            where = where / name
            try:
                child = os.open(name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise TokenStoreError(
                    f"Token directory {where} is not a private directory (a symbolic link or unreadable).") from exc
            os.close(dir_fd)
            dir_fd = child
            _check_dir(dir_fd, where, private=index == len(parts) - 1)
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _resolve(path: Path | None, trusted_base: Path | None) -> tuple[Path, Path, tuple[str, ...]]:
    """(token path, trusted base, directory names from the base to the token's directory)."""
    if path is None:
        base, parts = store_location()
        return base.joinpath(*parts, TOKEN_NAME), base, parts
    path = Path(path)
    if path.name != TOKEN_NAME:
        raise TokenStoreError(f"The token file must be named {TOKEN_NAME}: {path}")
    base = path.parent.parent if trusted_base is None else Path(trusted_base)
    return path, base, path.parent.relative_to(base).parts


def _read_token(dir_fd: int, path: Path) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(TOKEN_NAME, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TokenStoreError(f"Refusing the token file {path}: it is a symbolic link or unreadable.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TokenStoreError(f"Refusing the token file {path}: it is not a regular file.")
        if info.st_uid != os.geteuid():
            raise TokenStoreError(f"Refusing the token file {path}: it is not owned by this user.")
        if info.st_mode & 0o077:
            raise TokenStoreError(
                f"Refusing the token file {path}: mode {stat.S_IMODE(info.st_mode):04o} lets other users "
                "read it. Treat the token as exposed and rotate it (./run.sh --rotate-token).")
        raw = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    text = raw.decode("ascii", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    if len(raw) > MAX_TOKEN_FILE_BYTES or not TOKEN_PATTERN.fullmatch(text):
        raise TokenStoreError(f"The token file {path} is malformed; rotate it (./run.sh --rotate-token).")
    return text


def _write_temp(dir_fd: int, token: str) -> str:
    temp_name = f".{TOKEN_NAME}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        data = (token + "\n").encode("ascii")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink_quietly(dir_fd, temp_name)
        raise
    os.close(fd)
    return temp_name


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def load_or_create(path: Path | None = None, *, rotate: bool = False,
                   trusted_base: Path | None = None) -> tuple[str, str]:
    """Return (state, token): state is "created", "reused" or "rotated"."""
    path, base, parts = _resolve(path, trusted_base)
    dir_fd = _open_private_dir(base, parts, create=True)
    try:
        if not rotate:
            try:
                return "reused", _read_token(dir_fd, path)
            except FileNotFoundError:
                pass
        token = secrets.token_hex(TOKEN_BYTES)
        temp_name = _write_temp(dir_fd, token)
        try:
            if rotate:
                os.replace(temp_name, TOKEN_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                state = "rotated"
            else:
                try:
                    os.link(temp_name, TOKEN_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                            follow_symlinks=False)
                    state = "created"
                except FileExistsError:
                    # Another launcher created it first: use theirs, so both agree.
                    return "reused", _read_token(dir_fd, path)
        finally:
            _unlink_quietly(dir_fd, temp_name)
        os.fsync(dir_fd)
        return state, token
    finally:
        os.close(dir_fd)


def read_existing(path: Path | None = None, *, trusted_base: Path | None = None) -> str:
    """Read the stored token without creating one (FileNotFoundError when there is none)."""
    path, base, parts = _resolve(path, trusted_base)
    dir_fd = _open_private_dir(base, parts, create=False)
    try:
        return _read_token(dir_fd, path)
    finally:
        os.close(dir_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print '<state> <token>' for run.sh.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--load", action="store_true", help="reuse the stored token, creating it once (default)")
    mode.add_argument("--rotate", action="store_true", help="replace the stored token")
    mode.add_argument("--print-path", action="store_true", help="print the token file path only")
    args = parser.parse_args(argv)
    if args.print_path:
        print(default_store_path())
        return 0
    try:
        state, token = load_or_create(rotate=args.rotate)
    except (TokenStoreError, OSError) as exc:
        print(f"API token store: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(f"{state} {token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
