# Portable Linux preparation

These files prepare the existing runtime for a Linux systemd **user** service. No Pi has been provisioned or tested by creating them. The service runs as its owner, binds only to loopback, preserves USB access, and uses the existing recording/authentication gates. It does not install packages, configure radio/pairing, enable root access, or start automatically.

1. Copy the reviewed repository and a platform-appropriate, approved dependency environment to the Linux host. The launcher expects `backend/.venv`. A macOS virtualenv cannot be copied to Linux. Install the platform's gphoto2/libgphoto2 separately and use its normal USB permissions; do not run the camera backend as root. Only one camera-owning process may run at a time.
2. Create `~/.config/camera-autopilot/runtime.env` as an owned regular file with mode `0600`. Set `CAMERA_ENGINE=real`, `CAMERA_AUTOPILOT_EXPOSURE=local`, and an exact `CAMERA_MODEL` from `tools/inspect_usb_camera.py --list`. Add `CAMERA_PORT` when more than one identical body is attached. Ports may change after reconnection; inspect again before updating a selector. Set `CAMERA_AUTOPILOT_API_TOKEN` to a freshly generated private token of at least 24 characters. Do not commit it or put it in a URL. Systemd EnvironmentFile syntax is `KEY=value`; shell variable substitutions do not execute.
3. Render and inspect: `bash deploy/provision.sh --output /tmp/camera-autopilot.service`. The JSON lists missing prerequisites without reading a camera. Validate the unit on Linux with `systemd-analyze --user verify /tmp/camera-autopilot.service`.
4. Once prerequisites pass, explicitly install: `bash deploy/provision.sh --install`. It refuses to overwrite an existing service and reloads user units, but leaves the service stopped. Start only after resolving camera ownership: `systemctl --user start camera-autopilot`. Check `systemctl --user status camera-autopilot` and `journalctl --user -u camera-autopilot`. Stop using `systemctl --user stop camera-autopilot`. Enabling at login is a separate operator choice; this script does not enable it or configure user lingering.
5. Run the camera-free meter benchmark with the actual deployment interpreter: `backend/.venv/bin/python deploy/deployment_bench.py --output artifacts/falsification/portable-meter.json`. Compare the same fixture/iterations on Mac and Linux. Reported peak RSS includes fixture construction and imports; it is not full-system RAM or the final product's requirement.

An owned Pi must still demonstrate exact-body capture, motion-limited exposure, file identity, unplug/reconnect recovery, startup/shutdown, authenticated phone access through trusted HTTPS, full model/index memory, latency, power, heat, and endurance. The synthetic benchmark cannot establish those outcomes. Board selection and final enclosure/power design wait for those measurements. No Wi-Fi AP, BLE pairing, IMU reading, or battery proof is claimed.

The template deliberately accepts only local exposure. A phone requires a separately reviewed trusted HTTPS proxy; never bind Uvicorn directly to a LAN address. Service startup creates a private token file whose location is logged by the launcher, but the token itself is not logged. Existing per-image privacy/deletion behavior remains owned by the application.

## Portable host bundle (implementation wave)

`portable_bundle.py` now renders a concrete Pi 5 / CM5 / Jetson hybrid-host candidate. It executes no networking, package installation, privilege escalation, service start, camera access, or downloads. Pi 5 is the first validation profile; no profile is a measured shipping-board decision. The default reports a dry run without printing credentials:

```bash
.venv/bin/python -m deploy.portable_bundle --profile pi5-hybrid
.venv/bin/python -m deploy.portable_bundle --profile pi5-hybrid --interface wlan0 --country US --output /tmp/camera-puck-review
```

The second command creates a **new** 0700 directory with 0600 files, unique API/Wi-Fi credentials, a loopback backend service, NetworkManager AP keyfile, alternative hostapd/dnsmasq configs, Caddy local HTTPS proxy, offline environment, and a profile manifest. Existing output directories are refused so credentials cannot silently rotate. This is rendering, not Linux provisioning success. The backend service has bounded restart, task, file-descriptor, startup, and shutdown policies while retaining USB access.

On a Linux target, review actual interface name, regulatory country, USB ownership, camera selector, installed runtime, and conflicting services first. Choose **one** AP implementation. NetworkManager's candidate is deliberately `autoconnect=false`; its shared mode provides DHCP and may forward upstream traffic if available. Essential operations still have to work with upstream disconnected. The hostapd alternative requires assigning `192.168.77.1/24`, an installed kernel AP-capable Wi-Fi driver, and the paired dnsmasq DHCP config; it does not set up routing or provide DNS. These host changes are operator actions and were not performed on the Mac.

Review and validate on the target with the actual installed binaries before activation:

```bash
systemd-analyze --user verify /tmp/camera-puck-review/camera-autopilot.service
caddy validate --config /tmp/camera-puck-review/Caddyfile --adapter caddyfile
dnsmasq --test --conf-file=/tmp/camera-puck-review/dnsmasq.conf
```

There is no installed Linux/container validation runtime in this Mac environment, so only configuration contract tests ran here. NetworkManager keyfiles must be installed with root ownership and mode 0600 in its system-connections directory, then explicitly loaded/activated by the operator. Hostapd and NetworkManager must not own the same radio simultaneously. The generated Caddyfile uses `tls internal`; the phone must trust that instance's local CA before transmitting a token. After verifying HTTPS, explicitly set `CAMERA_AUTOPILOT_EXPOSURE=lan-proxy`, `CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1`, `CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://192.168.77.1`, and `CAMERA_AUTOPILOT_CORS_ORIGINS=https://192.168.77.1` in the private environment. The original `provision.py` local-only install gate intentionally does not auto-install a phone-facing configuration. Copying an AP config does not prove radio operation, phone pairing, TLS trust, or reconnect behavior.

BLE is optional and disabled in the generated profile. No advertising/GATT provisioning path is claimed: the first candidate uses Wi-Fi and manually transferred private credentials. A BLE setup flow must be designed and verified on an actual radio before it can become a user-facing discovery claim.

### Offline artifacts and health

The compact deterministic intent profile and offline library flags are included in `runtime.env`. See [the runtime package contract](RUNTIME_PACKAGE.md) for exact prepared index files and platform-specific dependency limitations. Capture never obtains missing models through this deployment tooling. Generate a local checksum inventory from the **actual** candidate runtime archive, every selected model file, and each prepared index member. Explicit file selection avoids accidentally including photographs or credentials:

```bash
.venv/bin/python -m deploy.artifact_manifest --root /path/to/prepared-package --artifact runtime-linux-aarch64.tar --artifact data/taste_index.compact/manifest.json --artifact data/taste_index.compact/embeddings.npy --output /tmp/offline-artifacts.json
.venv/bin/python -m deploy.host_readiness --storage /path/to/shots --token-file /tmp/camera-puck-review/api-token --manifest /tmp/offline-artifacts.json --artifact-root /path/to/prepared-package
```

The three artifact names above illustrate CLI syntax; they are not a complete distribution. Include all six compact arrays, the canonical source manifest, pinned CLIP encoder if enabled, and complete target runtime/dependencies. Validation hashes all declared local files without executing them. Missing, changed, escaping, malformed, or duplicate artifacts fail readiness; an empty generated manifest also fails. A pass proves only the declared bytes exist and match, not that the target runtime or all desired capabilities work.

`backend.device_health.device_health(shots_directory)` reports readable Linux battery capacity/power, thermal zones, and free disk. Missing readings stay `null`; mains power cannot be mistaken for infinite battery endurance. `portable_readiness` adds private-token, free-space and offline-artifact gates. Software readiness can pass without battery telemetry, but `field_ready` remains false because physical power/thermal/endurance testing is separate.

### IMU contract and physical gate

`backend.imu.LinuxIIOIMU` reads an already configured Linux kernel IIO device, including supported I2C sensors. It never probes an I2C bus, writes sensor registers, installs a driver, or invents calibration. Supply a measured `Calibration` with exact sensor name, mounted rig identity, finite gyro bias in rad/s, a proper orthonormal sensor-to-camera rotation, and measurement timestamp. Raw IIO axes use `(raw + offset) * scale`; gyro uses rad/s and acceleration m/s². Each read has a bounded caller timeout and only one read can be pending. A failed/timed-out reader is quarantined. An uninterruptible kernel read still requires supervised process shutdown; caller timeout does not claim kernel cancellation. Host timestamps are acquisition times, not synchronized camera/sensor timestamps.

`analyze_stability` rejects discontinuous timestamps and bounds traces to 10,000 samples. It reports angular RMS/peak and vibration relative to the trace's gravity mean. Stationary tilt does not create fake vibration; rotating gravity remains motion. It explicitly never claims a verified camera exposure factor. Fixture tests verify scales, orientation, missing calibration, identity mismatch, read timeout, stationary tilt, rotation, and malformed traces. No physical IMU, vibration transfer, mount calibration, or shutter-synchronized handheld exposure proof was available in this wave.

Sources: [Linux IIO sysfs ABI](https://www.kernel.org/doc/Documentation/ABI/testing/sysfs-bus-iio), [NetworkManager keyfile reference](https://networkmanager.dev/docs/api/latest/nm-settings-keyfile.html).

## Pi bring-up (stage F)

`pi_bringup.sh` takes an owned Raspberry Pi 5 / CM5 from a fresh Raspberry Pi OS (64-bit, Bookworm or newer) to a running service and a **20-shot no-drop capture proof**. It is dry-run by default (a dry run writes nothing and prints each receipt), idempotent, and with `--apply` writes a JSON receipt per phase under `--receipt-dir` (default `~/camera-autopilot-receipts`; refused inside the repo or a system directory). It does not run sudo itself: apt and udev/gvfs changes are printed as exact operator commands (the udev rule is staged into the receipt dir), and it reuses the existing tooling rather than reimplementing it — `tools/prepare_taste_runtime.py --offline`, `tools/deployment_preflight.py`, `deploy/host_readiness.py`, and `deploy/provision.py --install` (which still installs the systemd **user** unit stopped and refuses to overwrite).

```bash
bash deploy/pi_bringup.sh --dry-run      # inspect every phase, run nothing
bash deploy/pi_bringup.sh --apply        # apt + venv + readiness + service
bash deploy/pi_bringup.sh --apply --phase proof \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,014 --serial A7III0SERIAL \
  --token-file ~/.config/camera-autopilot/api-token
```

Full shopping list, flashing, copy-paste sequence, pass criteria, and the board-decision measurements (time, peak RAM, power, heat per operation) are in [PI_BRINGUP.md](PI_BRINGUP.md). A green proof is software-side field evidence for one board; whole-device power, mass, thermal-under-enclosure and battery endurance remain separate physical gates.
