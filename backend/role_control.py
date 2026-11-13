"""Camera-control role: the API and the only process that opens a camera.

``run.sh`` starts this instead of ``uvicorn`` when ``CAMERA_AUTOPILOT_RUNTIME=split`` and passes the same
Uvicorn arguments. It refuses the CLIP stack in-process and queues merges and encodes for ``role_jobs.py``.
"""
from __future__ import annotations

import sys

import runtime_roles


def prepare() -> None:
    runtime_roles.set_role("control")
    runtime_roles.install_control_import_guard()


def main(argv: list[str] | None = None) -> None:
    prepare()
    from uvicorn.main import main as uvicorn_main
    uvicorn_main(args=list(sys.argv[1:] if argv is None else argv), prog_name="role_control")


if __name__ == "__main__":
    main()
