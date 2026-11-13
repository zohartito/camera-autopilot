"""Control-side client for the perception role's Unix socket (docs/RUNTIME_SPLIT_2026-09-28.md).

Any failure raises :class:`PerceptionUnavailable`; callers already treat that as "CLIP unavailable" and
label the result, so a dead perception service never masquerades as a CLIP answer.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import socket
import time
from pathlib import Path

import runtime_roles

MAX_REPLY_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 20.0


class PerceptionUnavailable(RuntimeError):
    pass


class PerceptionOpUnsupported(PerceptionUnavailable):
    """A healthy service answered that it does not serve this op (an older perception build).
    That is a definite, prompt answer, so it does not latch the shot as unavailable."""


# One deadline per shot: [monotonic deadline, whether a request already failed in this shot].
_SHOT = contextvars.ContextVar("perception_shot", default=None)


@contextlib.contextmanager
def shot_deadline():
    """Every perception request inside shares one budget; after one fails the rest fail at once.
    A nested context belongs to the same shot and reuses its budget and failure."""
    if _SHOT.get() is not None:
        yield
        return
    token = _SHOT.set([time.monotonic() + _timeout(), False])
    try:
        yield
    finally:
        _SHOT.reset(token)


def remote() -> bool:
    """Forward CLIP work only from the camera-control role. A Mac that happens to source the
    puck's runtime.env keeps in-process CLIP; the perception role never forwards to itself."""
    return runtime_roles.role() == "control"


def _timeout() -> float:
    try:
        value = float(os.environ.get("CAMERA_AUTOPILOT_PERCEPTION_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if math.isfinite(value) and 0 < value <= 120 else DEFAULT_TIMEOUT_S


def _call(payload: dict, validate):
    """One request plus validation of its reply; any failure latches the shot as unavailable."""
    shot = _SHOT.get()
    if shot is not None and shot[1]:
        raise PerceptionUnavailable("perception already unavailable for this shot")
    try:
        return validate(_request_once(payload, shot))
    except PerceptionOpUnsupported:
        raise
    except PerceptionUnavailable:
        if shot is not None:
            shot[1] = True
        raise


def _request_once(payload: dict, shot) -> object:
    path = os.environ.get(runtime_roles.PERCEPTION_SOCKET_ENV)
    if not path:
        raise PerceptionUnavailable(f"perception service not configured ({runtime_roles.PERCEPTION_SOCKET_ENV})")
    data = (json.dumps(payload) + "\n").encode()
    deadline = time.monotonic() + _timeout()  # one deadline for the whole request, not per read
    if shot is not None:
        deadline = min(deadline, shot[0])

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("perception request deadline exceeded")
        return left

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            connection.connect(path)
            connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
            chunks, total = [], 0
            while True:
                connection.settimeout(remaining())
                chunk = connection.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_REPLY_BYTES:
                    raise PerceptionUnavailable("perception reply exceeds its size bound")
                chunks.append(chunk)
    except OSError as exc:
        raise PerceptionUnavailable(f"perception service unreachable ({type(exc).__name__}: {exc})") from exc
    try:
        reply = json.loads(b"".join(chunks))
    except (ValueError, RecursionError) as exc:
        raise PerceptionUnavailable("perception reply is not JSON") from exc
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        error = reply.get("error") if isinstance(reply, dict) else None
        # Only the optional tile op may be missing from an older service; any other op failing is
        # still a dead or broken service and latches the shot.
        if payload.get("op") == "classify_tiles" and error == "unknown op 'classify_tiles'":
            raise PerceptionOpUnsupported(f"perception does not serve {payload.get('op')!r}")
        raise PerceptionUnavailable(f"perception failed: {error or 'malformed reply'}")
    return reply.get("result")


def _image_path(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_absolute():
        raise PerceptionUnavailable("perception needs an absolute image path")
    try:
        return str(path.resolve())
    except (OSError, RuntimeError) as exc:
        raise PerceptionUnavailable(f"image path does not resolve ({type(exc).__name__})") from exc


def _finite(value, kind) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(kind)
        return float(value)
    except (OverflowError, ValueError) as exc:
        raise PerceptionUnavailable(f"perception returned a non-finite {kind}") from exc


def recommend(image_path: str, k: int) -> dict:
    """``embed_index.recommend`` run by the perception role."""
    def validate(result):
        if not isinstance(result, dict):
            raise PerceptionUnavailable("perception recommend reply is malformed")
        try:
            return {"f_number": _finite(result["f_number"], "f_number"),
                    "iso": int(_finite(result["iso"], "iso")),
                    "focal_mm": int(_finite(result["focal_mm"], "focal_mm"))}
        except KeyError as exc:
            raise PerceptionUnavailable(f"perception recommend reply lacks {exc}") from exc
    return _call({"op": "recommend", "path": _image_path(image_path), "k": int(k)}, validate)


def classify(image_path: str) -> dict[str, float]:
    """``subject_recognition._clip_classify`` run by the perception role."""
    def validate(result):
        if not isinstance(result, dict) or not result:
            raise PerceptionUnavailable("perception classify reply is malformed")
        import subject_recognition  # the classes this control build can floor (a service may be newer)
        unknown = sorted(str(name) for name in result if name not in subject_recognition.SUBJECT_PROMPTS)
        if unknown:
            raise PerceptionUnavailable(f"perception returned unknown subject classes {unknown[:3]}")
        return {str(name): _finite(value, "probability") for name, value in result.items()}
    return _call({"op": "classify", "path": _image_path(image_path)}, validate)


MAX_TILES = 16


def classify_tiles(image_path: str) -> list[dict]:
    """``subject_recognition._clip_classify_tiles`` run by the perception role."""
    def validate(result):
        import subject_recognition
        known = set(subject_recognition.SUBJECT_PROMPTS) | set(subject_recognition.TILE_DISTRACTOR_PROMPTS)
        if not isinstance(result, list) or not 1 <= len(result) <= MAX_TILES:
            raise PerceptionUnavailable("perception classify_tiles reply is malformed")
        tiles = []
        for tile in result:
            box = tile.get("box") if isinstance(tile, dict) else None
            probabilities = tile.get("probabilities") if isinstance(tile, dict) else None
            if (not isinstance(box, list) or len(box) != 4
                    or not isinstance(probabilities, dict) or not probabilities):
                raise PerceptionUnavailable("perception classify_tiles reply is malformed")
            box = [_finite(value, "tile box") for value in box]
            left, top, right, bottom = box
            if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
                raise PerceptionUnavailable("perception returned a tile box outside the frame")
            unknown = sorted(str(name) for name in probabilities if name not in known)
            if unknown:
                raise PerceptionUnavailable(f"perception returned unknown tile classes {unknown[:3]}")
            probabilities = {str(name): _finite(value, "probability") for name, value in probabilities.items()}
            # One softmax per tile: every value a probability, together summing to one.
            if (not all(0.0 <= value <= 1.0 for value in probabilities.values())
                    or abs(sum(probabilities.values()) - 1.0) > 1e-3):
                raise PerceptionUnavailable("perception returned tile scores that are not probabilities")
            tiles.append({"box": box, "probabilities": probabilities})
        return tiles
    return _call({"op": "classify_tiles", "path": _image_path(image_path)}, validate)
