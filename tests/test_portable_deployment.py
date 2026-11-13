"""Deployment preparation must not claim camera hardware or silently start services."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from unittest.mock import patch

from deploy.provision import check_environment, main, render_unit


def test_unit_keeps_loopback_launcher_non_root_and_usb_visible():
    unit = render_unit(Path('/tmp/a "quoted" % repo'), Path('/tmp/private.env'))
    assert 'ExecStart="/tmp/a \\"quoted\\" %% repo/run.sh"' in unit
    assert "UMask=0077" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "\nPrivateDevices=yes" not in unit
    assert "--host 0.0.0.0" not in unit
    assert "User=root" not in unit


def test_environment_validation_does_not_print_secret_and_rejects_permissive_file():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "private.env"
        path.write_text("CAMERA_ENGINE=real\nCAMERA_MODEL=Sony ILCE-7M2 (Control)\n"
                        "CAMERA_AUTOPILOT_API_TOKEN=" + "a" * 48 + "\n")
        path.chmod(0o600)
        assert check_environment(path) == []
        path.chmod(0o644)
        assert "0600" in check_environment(path)[0]


def test_render_on_mac_reports_blockers_without_system_mutation():
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "candidate.service"
        output = io.StringIO()
        with patch("deploy.provision.platform.system", return_value="Darwin"), \
             patch("deploy.provision.subprocess.run") as command, redirect_stdout(output):
            assert main(["--env-file", str(Path(temporary) / "missing.env"),
                         "--output", str(target)]) == 0
        command.assert_not_called()
        report = json.loads(output.getvalue())
        assert report["ready_to_install"] is False
        assert report["installed"] is False
        assert report["started"] is False
        assert target.is_file()


def test_install_with_missing_prerequisites_fails_without_system_mutation():
    with tempfile.TemporaryDirectory() as temporary, \
         patch("deploy.provision.subprocess.run") as command, redirect_stdout(io.StringIO()):
        assert main(["--env-file", str(Path(temporary) / "missing.env"), "--install"]) == 1
        command.assert_not_called()
