"""Compact-perception role: CLIP taste retrieval and the subject class floor behind a private Unix socket.

    backend/.venv-perception/bin/python backend/role_perception.py [--socket PATH] [--no-warm]

One JSON line in, one JSON reply out, one request at a time (the encoder is not shared across threads).
The socket lives in a 0700 directory owned by this user and is itself 0600. The service never opens a
camera and never writes to the gallery; it only reads the image path control sends.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import runtime_roles

MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 10.0
MAX_K = 64
# A single CLIP encode on the CM5 CPU is expected in seconds. Past this the service is treated as hung:
# it answers an error and exits, so systemd (Restart=on-failure) starts a fresh process.
INFERENCE_TIMEOUT_S = runtime_roles.env_float("CAMERA_AUTOPILOT_PERCEPTION_HANG_S", 60.0, 5.0, 600.0)
# Loading the encoder and the mapped index; a load stuck past this exits for a restart.
WARM_TIMEOUT_S = runtime_roles.env_float("CAMERA_AUTOPILOT_PERCEPTION_WARM_S", 300.0, 10.0, 3600.0)
EXIT_HUNG = 70
HEARTBEAT_EVERY_S = 2.0


def _recommend(path: str, k: int) -> dict:
    import embed_index
    return embed_index.recommend(path, k)


def _classify(path: str) -> dict:
    import subject_recognition
    return subject_recognition._clip_classify(path)


def _classify_tiles(path: str) -> list:
    import subject_recognition
    return subject_recognition._clip_classify_tiles(path)


DEFAULT_HANDLERS = {"recommend": _recommend, "classify": _classify, "classify_tiles": _classify_tiles}


def _private_directory(directory: Path) -> None:
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError(f"perception socket directory must be a private 0700 directory owned by this user: {directory}")


class PerceptionServer:
    def __init__(self, socket_path, handlers):
        self.path = Path(socket_path)
        if not self.path.is_absolute():
            raise ValueError("perception socket path must be absolute")
        _private_directory(self.path.parent)
        if self.path.is_symlink() or (self.path.exists() and not stat.S_ISSOCK(self.path.lstat().st_mode)):
            raise ValueError(f"refusing to replace a non-socket at {self.path}")
        self.path.unlink(missing_ok=True)  # a stale socket from a previous run
        self.handlers = dict(handlers)
        self.inference_timeout_s = INFERENCE_TIMEOUT_S
        self.on_hang = lambda: os._exit(EXIT_HUNG)
        self.heartbeat = Path(str(self.path) + ".alive")
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            self._listener.bind(str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        self._listener.listen(8)
        self._listener.settimeout(0.5)

    def _answer(self, raw: bytes) -> dict:
        try:
            request = json.loads(raw)
            op, path = request["op"], request["path"]
        except (ValueError, KeyError, TypeError, RecursionError):
            return {"ok": False, "error": "malformed request"}
        if not isinstance(op, str) or op not in self.handlers:
            return {"ok": False, "error": f"unknown op {op!r}"}
        if not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file():
            return {"ok": False, "error": "path must be an absolute regular file"}
        args = (path,)
        if op == "recommend":
            k = request.get("k", 15)
            if type(k) is not int or not 1 <= k <= MAX_K:
                return {"ok": False, "error": f"k must be an integer 1..{MAX_K}"}
            args = (path, k)
        outcome = {}

        def infer():
            try:
                outcome["result"] = self.handlers[op](*args)
            except Exception as exc:  # noqa: BLE001 — every failure is reported, the service keeps running
                outcome["error"] = f"{type(exc).__name__}: {exc}"
        worker = threading.Thread(target=infer, daemon=True)
        worker.start()
        worker.join(self.inference_timeout_s)
        if worker.is_alive():
            self._hung = True
            return {"ok": False, "error": f"inference exceeded {self.inference_timeout_s:g} s; service restarting"}
        if "error" in outcome:
            return {"ok": False, "error": outcome["error"]}
        return {"ok": True, "result": outcome.get("result")}

    def _handle(self, connection: socket.socket) -> None:
        self._hung = False
        try:
            self._serve_connection(connection)
        finally:
            if self._hung:  # even when the caller already gave up and the reply could not be sent
                print("perception: inference hung; exiting for a restart", file=sys.stderr, flush=True)
                self.on_hang()

    def _serve_connection(self, connection: socket.socket) -> None:
        with connection:
            deadline = time.monotonic() + REQUEST_TIMEOUT_S  # for reading the whole request
            chunks, total = [], 0
            try:
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return
                    connection.settimeout(left)
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REQUEST_BYTES:
                        connection.sendall(b'{"ok": false, "error": "request too large"}')
                        return
                    chunks.append(chunk)
                try:
                    reply = self._answer(b"".join(chunks))
                    encoded = json.dumps(reply, allow_nan=False).encode()
                except Exception as exc:  # noqa: BLE001 — one bad request never stops the service
                    encoded = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}).encode()
                connection.settimeout(REQUEST_TIMEOUT_S)
                connection.sendall(encoded)
            except OSError:
                return  # the caller gave up; it already treats that as unavailable

    def _heartbeat_loop(self) -> None:
        # A side thread, so a long inference does not make a live service look dead.
        while not self._stop.is_set():
            runtime_roles.touch_heartbeat(self.heartbeat)  # control reads its age in /device-health
            self._stop.wait(HEARTBEAT_EVERY_S)

    def serve_forever(self, ready: threading.Event | None = None) -> None:
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        if ready is not None:
            ready.set()
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = self._listener.accept()
                except socket.timeout:
                    continue
                self._handle(connection)
        finally:
            self.close()

    def shutdown(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._listener.close()
        self.path.unlink(missing_ok=True)
        self.heartbeat.unlink(missing_ok=True)


def open_server(socket_path, handlers, warm, *, warm_timeout_s=None, on_hang=None) -> PerceptionServer:
    """Warm CLIP first, then bind: until the model is loaded a client gets ECONNREFUSED/ENOENT at
    once instead of spending its deadline in the listen backlog. A warm-up stuck past its bound
    exits (70) so systemd restarts the service."""
    loader = threading.Thread(target=warm, daemon=True)
    loader.start()
    loader.join(WARM_TIMEOUT_S if warm_timeout_s is None else warm_timeout_s)
    if loader.is_alive():
        print("perception: warm-up hung; exiting for a restart", file=sys.stderr, flush=True)
        (on_hang or (lambda: os._exit(EXIT_HUNG)))()
    return PerceptionServer(socket_path, handlers)


def _warm() -> None:
    try:
        import embed_index
        embed_index._get_index()
        embed_index._get_model()
        print("perception: CLIP encoder and taste index loaded", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — requests will report the same failure
        print(f"perception: warm-up failed ({type(exc).__name__}: {exc}); requests will report it",
              file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=os.environ.get(runtime_roles.PERCEPTION_SOCKET_ENV))
    parser.add_argument("--no-warm", action="store_true", help="load the encoder on the first request")
    args = parser.parse_args(argv)
    if not args.socket:
        parser.error(f"--socket or {runtime_roles.PERCEPTION_SOCKET_ENV} is required")
    runtime_roles.set_role("perception")
    socket_path = Path(args.socket)
    if socket_path.is_absolute() and not socket_path.parent.exists():
        socket_path.parent.mkdir(mode=0o700, parents=True)
    runtime_roles.require_memory_cap("perception")
    server = open_server(socket_path, DEFAULT_HANDLERS, (lambda: None) if args.no_warm else _warm)
    print(json.dumps({"role": "perception", "socket": str(socket_path)}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
