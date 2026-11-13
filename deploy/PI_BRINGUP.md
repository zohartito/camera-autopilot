# Raspberry Pi 5 / CM5 bring-up (stage F)

> **The purchased board is a CM5 on a Waveshare CM5-NANO-A.** Flash and first-boot it with
> [CM5_BRINGUP.md](CM5_BRINGUP.md) (rpiboot, headless Wi-Fi/SSH, `config.txt`, onboard-Wi-Fi AP,
> day-one tests), then continue here. The Raspberry Pi 5 below is the named fallback.

One script takes an owned board from a fresh **Raspberry Pi OS (64-bit, Bookworm
or newer)** to a running `camera-autopilot` service and a **20-shot no-drop
capture proof**. Every phase is idempotent, dry-run by default, and writes a JSON
receipt under `--receipt-dir` (default `~/camera-autopilot-receipts`).

Nothing here is a board decision. A green proof is software-side field evidence;
whole-device power, weight, heat under enclosure, and battery endurance are still
separate physical gates.

> The script never runs `sudo`. The apt and udev phases print the exact root command for you to run, record the gap in their receipts, and continue; re-run the phase after installing.

## Shopping list

- **Board:** Raspberry Pi 5 (8 GB) — or CM5 (8/16 GB) on an official CM5 IO board.
  8 GB because the **measured** resident taste/CLIP path is ≈ 1.58 GB (Mac mapped;
  see `deploy/RUNTIME_PACKAGE.md`) and must coexist with the OS, service, a bounded
  merge and page cache. The merge working set itself is no longer the driver — the
  tiled/streaming rewrite cut it to ≈ 0.39 GB and a 4-frame 24 MP merge now completes
  (measured 2026-09-17; the old "3-frame HDR exceeds 4 GiB" claim is superseded). Full
  RAM-floor arithmetic and the puck-vs-phone split: `docs/TARGET_PREPARATION_2026-09-17.md`.
- **Power:** official Raspberry Pi 27 W USB-C PSU (under-voltage throttles capture).
- **Cooling:** the official active cooler (the proof records thermals; sustained
  capture must not throttle).
- **Storage:** A2 microSD ≥ 64 GB, or an NVMe SSD on the Pi 5 / CM5 IO board.
- **Camera link:** USB-C (or USB-A) cable to the Sony body's data port. For **two
  bodies**, add a **powered** USB hub — the Pi cannot bus-power two cameras reliably.

## 1. Flash the OS

1. Raspberry Pi Imager → **Raspberry Pi OS (64-bit)**, Bookworm or newer.
2. In Imager's settings: set hostname, enable SSH, set the user, set Wi-Fi/locale.
3. Boot, then `sudo apt-get update && sudo apt-get full-upgrade` and reboot.

## 2. Get the repo onto the Pi

Pick whichever the deploy README already supports:

- **LAN clone from the Mac** (simplest for development):
  ```bash
  git clone <mac-lan-or-git-remote>/camera-autopilot.git ~/camera-autopilot
  cd ~/camera-autopilot
  ```
- **Offline bundle** (no network in the field): on the Mac render the candidate host
  bundle with `deploy/portable_bundle.py` and copy the reviewed repository plus the
  prepared offline package to the Pi as described in `deploy/README.md` and
  `deploy/RUNTIME_PACKAGE.md`. Do **not** copy the Mac virtualenv — this script
  builds a fresh Linux one.

## 3. Dry-run everything (runs nothing side-effecting)

```bash
bash deploy/pi_bringup.sh --dry-run
```

A dry run writes nothing, not even the receipt directory. Each phase prints its receipt as a
`RECEIPT <phase> <json>` line and the path it would write. The host-facts receipt records the
model, OS, kernel, arm64, RAM, free disk, thermal zones and `vcgencmd get_throttled` warnings.
Every other phase prints the exact command it *would* run. `--receipt-dir` must be outside the
repository and outside system directories (`--apply --allow-system-dir` to name one explicitly).

The **udev/USB** phase is advisory in both modes: a dry run prints the proposed rule,
`--apply` stages it in the receipt dir (0600), and both print the privileged commands for you
to run — it never edits `/etc` or calls sudo:

```bash
sudo usermod -aG plugdev "$USER"        # then log out and back in
sudo cp ~/camera-autopilot-receipts/99-camera-autopilot-sony.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# Raspberry Pi OS Desktop only: stop the auto-mounter stealing the camera from gphoto2
systemctl --user mask gvfs-gphoto2-volume-monitor.service   # or: sudo apt-get remove gvfs-backends
```

## 4. Apply the host-side work

```bash
bash deploy/pi_bringup.sh --apply
```

This runs, in order: **apt** (installs the libs behind the pinned wheels), **venv**
(creates `backend/.venv`, installs `requirements.txt`, validates pinned model bytes
offline via `tools/prepare_taste_runtime.py --offline`), **readiness**
(`tools/deployment_preflight.py`, and `deploy/host_readiness.py` when you pass
`--manifest`/`--artifact-root`/`--token-file`), and **service** (renders and installs
the systemd **user** unit via `deploy/provision.py --install`). The service is
installed **stopped** — provision.py never starts it and refuses to overwrite an
existing unit.

Before the service will start you must create the private
`~/.config/camera-autopilot/runtime.env` (mode 0600) with `CAMERA_ENGINE=real`, an
exact `CAMERA_MODEL`, `CAMERA_PORT` when two identical bodies are attached, and a
`CAMERA_AUTOPILOT_API_TOKEN` of ≥ 24 characters — see `deploy/README.md` step 2.
Then, once camera ownership is clear:

```bash
systemctl --user start camera-autopilot
systemctl --user status camera-autopilot
```

## 5. The 20-shot no-drop proof

With the service running and the camera connected, give the exact body and USB port
(`tools/inspect_usb_camera.py --list` prints them), the body's USB serial (`/shoot` refuses a
capture without it; see "Running the proof by hand" in [CM5_BRINGUP.md](CM5_BRINGUP.md) for how
to read it) and the token file:

```bash
bash deploy/pi_bringup.sh --apply --phase proof \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,014 --serial A7III0SERIAL \
  --token-file ~/.config/camera-autopilot/api-token
```

It loops 20 `POST /shoot` calls through `curl` against `127.0.0.1:8000` with the
`X-Camera-Autopilot-Token` header, recording per shot: HTTP status, image name,
elapsed time, and `settings_readback.after_final`; plus the service's peak
`/proc/<pid>/status` VmHWM and thermal readings before/after.

### What pass looks like

`proof.json` reports `"pass": true` only when all 20 shots return **HTTP 200**, each
with a non-empty `image` and `settings_readback.after_final == "verified"`, and
**zero re-enumeration failures** (no 409/503, no `retry_capture:false`, no
`may_have_fired`). Anything else is a fail — record it; the script never retries a
partial capture into a success.

## 6. Measurements that feed the board decision

`soak.json` collects the numbers the puck decision needs, per operation:

| Measurement | Field | Source |
| --- | --- | --- |
| Time per shot | `elapsed_s` (min/max/mean) | curl `%{time_total}` |
| Peak RAM | `peak_vmhwm_kib` | `/proc/<pid>/status` VmHWM |
| Heat | `peak_temp_before_c` / `peak_temp_after_c` | `/sys/class/thermal` |
| Power / throttle | `throttled_after` | `vcgencmd get_throttled` |

These are software-side field evidence for one board. The paired candidate receipt
that actually decides the SoM still needs measured whole-device power, temperature
under enclosure, mass, and battery endurance — none of which this script claims.

## Flags

`--phase <name>` runs one of `host-facts board-config apt udev venv readiness service proof soak`.
`--receipt-dir`, `--model`, `--port`, `--host`, `--http-port`, `--token-file`,
`--shots`, `--env-file`, `--manifest`, `--artifact-root`, `--service-pid`,
`--with-ap` (adds caddy for the AP/local-HTTPS lane), `--carrier` (CM5 carrier assumption,
default `auto` = CM5-NANO-A). `--help` lists them all.
