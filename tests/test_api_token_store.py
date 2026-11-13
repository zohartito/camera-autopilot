"""The per-user API token store behind run.sh: created once, private, reused, rotated on request.

Every test points the store at a private tmp HOME; the real per-user file is never touched.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import api_token_store as store  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return store.default_store_path({"HOME": str(tmp_path / "home")}, "Darwin")


def test_default_paths_follow_each_platform_convention(tmp_path: Path) -> None:
    home = str(tmp_path)
    assert store.default_store_path({"HOME": home}, "Darwin") == \
        tmp_path / "Library/Application Support/camera-autopilot/api-token"
    assert store.default_store_path({"HOME": home}, "Linux") == tmp_path / ".config/camera-autopilot/api-token"
    xdg = tmp_path / "xdg"
    assert store.default_store_path({"HOME": home, "XDG_CONFIG_HOME": str(xdg)}, "Linux") == \
        xdg / "camera-autopilot/api-token"
    # A relative XDG_CONFIG_HOME is invalid by the spec and must not steer the token into the cwd.
    assert store.default_store_path({"HOME": home, "XDG_CONFIG_HOME": "rel"}, "Linux") == \
        tmp_path / ".config/camera-autopilot/api-token"


def test_created_once_private_then_reused(token_path: Path) -> None:
    state, token = store.load_or_create(token_path)
    assert state == "created"
    assert len(token) == 64 and all(ch in "0123456789abcdef" for ch in token)
    assert _mode(token_path) == 0o600
    assert _mode(token_path.parent) == 0o700
    assert token_path.read_text() == token + "\n"
    assert store.load_or_create(token_path) == ("reused", token)
    assert store.read_existing(token_path) == token
    assert sorted(os.listdir(token_path.parent)) == ["api-token"], "no temporary file may remain"


def test_rotation_replaces_the_token_atomically_with_a_private_file(token_path: Path) -> None:
    _, first = store.load_or_create(token_path)
    state, second = store.load_or_create(token_path, rotate=True)
    assert state == "rotated" and second != first
    assert store.load_or_create(token_path) == ("reused", second)
    assert _mode(token_path) == 0o600
    assert sorted(os.listdir(token_path.parent)) == ["api-token"]


def test_group_or_world_readable_token_is_refused_not_repaired(token_path: Path) -> None:
    _, token = store.load_or_create(token_path)
    os.chmod(token_path, 0o640)
    with pytest.raises(store.TokenStoreError) as refused:
        store.load_or_create(token_path)
    assert token not in str(refused.value) and "rotate" in str(refused.value)
    assert _mode(token_path) == 0o640
    # Rotation is the way out: a fresh 0600 file replaces the exposed one.
    state, fresh = store.load_or_create(token_path, rotate=True)
    assert state == "rotated" and fresh != token and _mode(token_path) == 0o600


def test_symlink_and_non_regular_files_are_refused(token_path: Path, tmp_path: Path) -> None:
    token_path.parent.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("0" * 64 + "\n")
    os.chmod(target, 0o600)
    token_path.symlink_to(target)
    with pytest.raises(store.TokenStoreError, match="symbolic link"):
        store.load_or_create(token_path)
    token_path.unlink()
    token_path.mkdir()
    with pytest.raises(store.TokenStoreError):
        store.load_or_create(token_path)


def test_directory_owned_by_someone_else_or_loose_is_refused(token_path: Path, monkeypatch) -> None:
    store.load_or_create(token_path)
    os.chmod(token_path.parent, 0o755)
    with pytest.raises(store.TokenStoreError, match="0700"):
        store.load_or_create(token_path)
    os.chmod(token_path.parent, 0o700)
    other_user = os.geteuid() + 1
    monkeypatch.setattr(store.os, "geteuid", lambda: other_user)
    with pytest.raises(store.TokenStoreError, match="not owned"):
        store.load_or_create(token_path)


def test_symlinked_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    base = tmp_path / "home/Library/Application Support"
    base.mkdir(parents=True)
    (base / "camera-autopilot").symlink_to(real)
    with pytest.raises(store.TokenStoreError, match="not a private directory"):
        store.load_or_create(base / "camera-autopilot/api-token")


def test_symlinked_or_shared_ancestor_below_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    (home / "Library").mkdir(parents=True)
    elsewhere.mkdir()
    (home / "Library" / "Application Support").symlink_to(elsewhere)
    token_path = store.default_store_path({"HOME": str(home)}, "Darwin")
    with pytest.raises(store.TokenStoreError, match="not a private directory"):
        store.load_or_create(token_path, trusted_base=home)
    assert list(elsewhere.iterdir()) == [], "nothing may be created through the redirected ancestor"
    (home / "Library" / "Application Support").unlink()
    (home / "Library" / "Application Support").mkdir()
    os.chmod(home / "Library", 0o777)
    with pytest.raises(store.TokenStoreError, match="writable by group or others"):
        store.load_or_create(token_path, trusted_base=home)
    os.chmod(home / "Library", 0o755)
    state, _ = store.load_or_create(token_path, trusted_base=home)
    assert state == "created" and _mode(token_path.parent) == 0o700


def test_the_default_location_walks_from_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    base, parts = store.store_location()
    assert base == tmp_path and parts[-1] == "camera-autopilot"
    state, token = store.load_or_create()
    assert state == "created" and store.read_existing() == token
    assert store.default_store_path().read_text() == token + "\n"


def test_malformed_token_is_refused(token_path: Path) -> None:
    store.load_or_create(token_path)
    token_path.write_text("short\n")
    with pytest.raises(store.TokenStoreError, match="malformed"):
        store.load_or_create(token_path)


def test_losing_the_create_race_reuses_the_winner(token_path: Path, monkeypatch) -> None:
    winner = "ab" * 32
    real_link = os.link

    def racing_link(src, dst, **kwargs):
        # Another launcher finishes creating the file just before this one links its own.
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (winner + "\n").encode())
        os.close(fd)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(store.os, "link", racing_link)
    assert store.load_or_create(token_path) == ("reused", winner)
    assert sorted(os.listdir(token_path.parent)) == ["api-token"]


def test_cli_prints_state_and_token_only_on_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert store.main(["--load"]) == 0
    out, err = capsys.readouterr()
    state, token = out.split()
    assert state == "created" and len(token) == 64 and err == ""
    assert store.main(["--print-path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == store.default_store_path()
    os.chmod(store.default_store_path(), 0o644)
    assert store.main([]) == 1
    out, err = capsys.readouterr()
    assert out == "" and token not in err
