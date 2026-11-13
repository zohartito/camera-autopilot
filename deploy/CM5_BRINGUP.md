# CM5 on the Waveshare CM5-NANO-A — flashing, first boot, day-one tests

The purchased board is a **Raspberry Pi CM5108016** (BCM2712, 8 GB, Wi-Fi/BT, 16 GB eMMC) on a
**Waveshare CM5-NANO-A** carrier. It ships around 2026-10-05. The **Raspberry Pi 5 8GB** is the
named fallback: [PI_BRINGUP.md](PI_BRINGUP.md) covers it and still applies to the CM5 once it has
booted.

**Nothing in this file has run on the board yet.** Every command was written from the official
Raspberry Pi and Waveshare pages cited below, and the scripts were tested only in dry-run and
against fakes on the Mac. The first real receipts come from lane 1H.

## What the carrier means for us

| Nano-A fact | Consequence |
|---|---|
| One USB 3.2 Gen1 Type-A port, limited to 2 A | The camera takes it. No ALFA adapter, keyboard or stick without a hub. |
| No HDMI and no Ethernet | `wlan0` is the only link. Wi-Fi, SSH and the hostname must be baked into the image. |
| USB-C is the 5 V input **and** the flashing port | Flash from the Mac over USB-C with BOOT held; run from the 27 W PSU afterwards. |
| TF slot disabled on eMMC modules | Storage is the 16 GB eMMC only (lane 1K decides what's next). |
| 4-pin PWM fan header, RTC header, 40-pin GPIO | Fan curve and I2C (for the future IMU) go in `config.txt`. |

Waveshare also says: **do not plug or unplug anything except USB while the board is powered**, and
**connect nothing else while flashing over USB-C**, because the flashing supply is marginal and the
board may not be recognised.

## 0. Before the board arrives (on the Mac, dry-run only)

```bash
bash deploy/pi_bringup.sh --dry-run --receipt-dir ~/cm5-dryrun    # every phase; runs and writes nothing
python3 deploy/cm5_nano.py render                                  # the config.txt fragment
python3 deploy/day_one.py autodetect --receipt-dir ~/cm5-dryrun    # each day-one test prints its plan
```

A dry run **writes nothing**: not the receipt dir, not a receipt, not a staged file. It prints
each receipt as a `RECEIPT <phase> <json>` line and each file as `WOULD-WRITE <path>`. Every
receipt or staging directory must be **outside the repository** (a staged AP passphrase must never
be committable) and outside system directories such as `/etc`, `/boot` and `/usr`. Naming one of
those needs `--apply --allow-system-dir` (`--run --allow-system-dir` for day_one.py);
`deploy/safe_paths.py` enforces both rules in both modes. It resolves the path once (`~` plus
realpath), and that same path is both checked and written. It compares with `samefile` as well
as by string, so a differently-cased spelling on the Mac's case-insensitive disk is caught.
Receipts are replaced by atomic rename and never written through a link. With `--apply`, a
receipt dir containing symlinks is refused. Nothing, bytecode included, is left in the repo.

With `--apply`, the `board-config` phase stages two review files under `<receipt-dir>/board-config/`
(mode 0600), written into a temporary sibling, fsynced and renamed into place with a `STAGED.json`
manifest of hashes. An existing `board-config/` only counts as staged if that manifest checks out;
a partial one fails the phase and asks you to delete it. If the render fails (in dry run or
apply), the phase exits non-zero and claims nothing:

- `config.txt.cm5-nano-a` turns on I2C and adds Waveshare's fan curve. The block is scoped to
  `[cm5] … [all]`, so a Pi 5 ignores it. Every line cites its source. It deliberately sets nothing
  for USB, `usb_max_current_enable` or the antenna, because no source supported a value. host-facts
  records the live `usb_max_current_enable` instead.
- `camera-puck.nmconnection` is the onboard-Wi-Fi access point (see section 5). A re-run keeps the
  existing file, so the passphrase never rotates silently. Delete the directory to regenerate it.

## 1. Build `rpiboot` on the Mac (official usbboot steps)

This kit installs nothing on the Mac. When you're ready, these are the official macOS build steps
from the [usbboot README](https://github.com/raspberrypi/usbboot#macos). They need Homebrew.

```bash
git clone --recurse-submodules --shallow-submodules --depth=1 https://github.com/raspberrypi/usbboot
cd usbboot
brew install libusb pkg-config
make INSTALL_PREFIX=/usr/local
# if the build can't find libusb.h:
#   export PKG_CONFIG_PATH="$(brew --prefix libusb)/lib/pkgconfig" && make INSTALL_PREFIX=/usr/local
```

Also install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) for macOS.

## 2. Put the CM5 into flashing mode and expose the eMMC

1. Seat the CM5 on the Nano-A and fit the passive cooler. **Nothing in the USB-A port**, nothing on
   the GPIO, fan or RTC headers.
2. On the Mac, start rpiboot first. It waits for the board:
   ```bash
   cd usbboot && sudo ./rpiboot -d mass-storage-gadget64
   ```
3. **Hold the Nano-A's BOOT button**, then plug a USB-C **data** cable from the Nano-A into the Mac.
   The Mac's port powers the board. Keep holding until rpiboot shows it has found the device, then
   let go. (Waveshare: press BOOT before power-on and connect over Type-C. On CM5 the ROM enters
   usbboot when nRPIBOOT, BCM2712 GPIO 20, is held at boot.)
4. When rpiboot finishes, macOS says *"The disk you inserted was not readable by this computer."*
   Click **Ignore**. The eMMC is now a USB mass-storage disk of about 16 GB.

If the Mac never sees it: plug directly into the Mac with no hub, try another cable or port, and
check the [usbboot troubleshooting guide](https://github.com/raspberrypi/usbboot/blob/master/docs/troubleshooting.md).

## 3. Flash Raspberry Pi OS Lite (64-bit) with the settings baked in

In Raspberry Pi Imager:

- **Device:** the Compute Module 5 entry if Imager lists one, otherwise *Raspberry Pi 5*. Waveshare's
  flashing page makes the same choice one generation back, picking "Pi 4" for a CM4.
- **OS:** Raspberry Pi OS **Lite (64-bit)**. Waveshare's FAQ says the CM5 needs Bookworm
  2024-11-19 or later.
- **Storage:** the ~16 GB rpiboot disk. Keep *Exclude system drives* on.
- **Customisation**, all of it, because the board has no display and no Ethernet:
  - **Hostname:** `camera-puck` (letters, numbers, hyphens). It is advertised over mDNS as `camera-puck.local`.
  - **Localisation:** your capital city. This also sets the **Wi-Fi regulatory domain**, and the
    Raspberry Pi docs note that dual-band Wi-Fi stays **disabled** until a country is set.
  - **User:** a username and password.
  - **Wi-Fi:** the home network's SSID and password (Imager pre-fills the Mac's current network).
    Tick *Hidden SSID* only if it really is hidden.
  - **Remote access:** enable SSH with **public-key authentication**, using the Mac's public key
    (`~/.ssh/id_ed25519.pub`).
  - Raspberry Pi Connect: leave it off. We don't need it.
- Write. When it's done, eject the disk and unplug the USB-C from the Mac. Don't hold BOOT from here on.
- Plug the Nano-A's USB-C into the **27 W PSU**. It boots from the eMMC.

## 4. Find the board and log in

```bash
ping camera-puck.local                # mDNS via Avahi on Raspberry Pi OS
ssh <user>@camera-puck.local
```

If `.local` doesn't resolve, look for `camera-puck` in the router's DHCP client list, or ping-scan
the subnet if nmap is installed (the Raspberry Pi *Find your IP address* page uses
`sudo nmap -sn 192.168.1.0/24`).

### If Wi-Fi setup failed: reflash

There is no screen and no network jack, so a board that never joins Wi-Fi can't be fixed in place.
macOS can't write the ext4 root partition either. The fix is to reflash:

1. Unplug the power. Repeat section 2 (rpiboot, hold BOOT, USB-C to the Mac).
2. Re-run Imager with corrected customisation. The usual causes are no Wi-Fi country, a wrong
   password, a hidden SSID without *Hidden SSID* ticked, or the wrong network name.
3. Boot from the PSU again.

Reflashing erases the eMMC. Nothing important lives there before lane 1H has run.

## 5. First boot, in order

```bash
sudo apt-get update && sudo apt-get full-upgrade && sudo reboot
# copy the repo onto the board (PI_BRINGUP.md section 2), then:
bash deploy/pi_bringup.sh --dry-run            # host-facts must say board=cm5, carrier=cm5-nano-a
bash deploy/pi_bringup.sh --apply --phase host-facts
bash deploy/pi_bringup.sh --apply --phase board-config   # stages the fragment + AP profile, 0600
```

Read `~/camera-autopilot-receipts/host-facts.json`. It records the model, the carrier **assumption** (software can't identify
the carrier), `usb_max_current_enable`, the root device (expect an eMMC `mmcblk…` partition),
thermals and throttle flags. Then apply what `board-config` staged, **after reading it**:

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
cat ~/camera-autopilot-receipts/board-config/config.txt.cm5-nano-a | sudo tee -a /boot/firmware/config.txt >/dev/null
sudo reboot
ls /dev/i2c-*                                  # I2C is on
sudo install -m 600 ~/camera-autopilot-receipts/board-config/camera-puck.nmconnection /etc/NetworkManager/system-connections/
sudo nmcli connection reload                   # loaded, NOT brought up (autoconnect=false)
```

Then continue with PI_BRINGUP.md sections 3–4: apt, udev, venv, readiness and service.

### The onboard-Wi-Fi access point, and why each setting was chosen

`docs/research/board-2026-09-22/failure_modes.md` records four brcmfmac access-point failures on
this chip family. The profile avoids each one:

| Setting | Avoids |
|---|---|
| `band=bg`, `channel=6` (1 / 6 / 11 allowed) | **DFS**: 2.4 GHz has no radar-detection channels, so the firmware can't get stuck in DFS-UNSET with no beacons ([forum t=392203](https://forums.raspberrypi.com/viewtopic.php?t=392203)). |
| `key-mgmt=wpa-psk`, `proto=rsn` (WPA2 only; no legacy WPA, no SAE, no WPA3, no transition mode) | the **firmware crash with WPA3/SAE** ([raspberrypi/linux#7528](https://github.com/raspberrypi/linux/issues/7528)). `proto=rsn` also excludes legacy WPA1. |
| `interface-name=wlan0`, no `uap0` | the **firmware crash on a virtual-AP interface** ([raspberrypi/firmware#1463](https://github.com/raspberrypi/firmware/issues/1463)). |
| one connection per device; `autoconnect=false` | **AP+STA concurrency and repeated reloads**, which wedge the driver with "HT Avail timeout" ([openwrt#23069](https://github.com/openwrt/openwrt/issues/23069)). |
| `ipv4.method=shared`, `192.168.77.1/24` | not a pitfall: this is the address `deploy/portable_bundle.py`'s Caddyfile serves. |

`pairwise`/`group` ciphers and PMF are left at NetworkManager's defaults. In the forum thread this
profile follows ([t=357998](https://forums.raspberrypi.com/viewtopic.php?t=357998)), the poster set
`proto rsn`, `group ccmp` and `pairwise ccmp` together, clients failed, and removing all three
fixed it. The thread never isolated which setting was to blame. So `proto=rsn` is set on its own:
it is what makes the profile WPA2-only, and it is **untested** against that failure. **Client
negotiation with this profile is untested** until the day-one AP soak. If a phone refuses the correct
password, try in this order, and note the change in the soak receipt:
1. `sudo nmcli connection modify camera-puck wifi-sec.proto ""`, which drops `proto=rsn` (the
   setting the forum thread suspects).
2. `sudo nmcli connection modify camera-puck wifi-sec.pmf disable`
   ([pimylifeup](https://pimylifeup.com/raspberry-pi-wireless-access-point/)).

**Bringing the AP up drops the client Wi-Fi, and with it your SSH session.** The Nano-A has no
other link. So always start the soak detached, then follow it from the AP side:

```bash
mkdir -p ~/day1
nohup python3 deploy/day_one.py ap-soak --run --receipt-dir ~/day1 > ~/day1/ap-soak.log 2>&1 &
# join the Mac and the phone to "Camera-Puck" (passphrase: the psk= line of the staged profile)
ssh <user>@192.168.77.1
```

When the soak ends it takes the AP down, and the board rejoins the home network. If the radio
wedges ("HT Avail timeout" in `journalctl -k`), the documented recovery is a **cold boot**: unplug
the USB-C power. Note that in the receipt. The soak reads the kernel log with `journalctl -k`.
If it can't (run it as a user in the `systemd-journal` or `adm` group), the receipt is
**inconclusive**, never a pass.

## 6. Day-one tests (BOARD_DECISION list), in order

Each test is a dry run unless you add `--run`. A dry run prints its plan and the receipt it
would write, and writes nothing. `--run` writes `<receipt-dir>/day1-<test>-<UTC>.json` (0600), and
exits 0 on a pass, 1 on a fail, 3 when inconclusive. Stop the service before tests 1, 3 and 3b,
because only one process can own the camera. Re-detect the port every time, because ports move on
re-enumeration. **Every camera receipt is keyed on the USB serial the body reports** (`identity`).
`--body` is only a claim, and a serial that contradicts it fails the test before anything fires.

```bash
# 1. auto-detect, per body. Identity is the USB serial, not the gphoto label.
python3 deploy/day_one.py autodetect --run --body a7iii --receipt-dir ~/day1
python3 deploy/day_one.py autodetect --run --body a7ii  --receipt-dir ~/day1
# 2. the existing 20-shot proof, per body, with throttle/temperature sampling (service running,
#    body Image Quality set to RAW+JPEG, RAW File Type Compressed). --shots must be >= 20.
python3 deploy/day_one.py capture-loop --run --body a7iii --model "Sony Alpha-A7 III (PC Control)" \
    --port usb:BBB,DDD --token-file ~/.config/camera-autopilot/api-token --receipt-dir ~/day1
# 3. USB replug with the service stopped and the body idle, then the recovery ladder (interactive)
python3 deploy/day_one.py usb-replug-idle --run --body a7iii --receipt-dir ~/day1
# 3b. body auto-power-off (sleep), wake, then the same ladder (service stopped; interactive)
python3 deploy/day_one.py sleep-wake --run --body a7iii --receipt-dir ~/day1
# 4. the 8-hour AP soak, detached (see above)
# 5. sustained 24 MP merges: open-air first, then in the closed box
python3 deploy/day_one.py merge-thermal --run --enclosure open-air --minutes 30 \
    f0.jpg f1.jpg f2.jpg --python backend/.venv/bin/python --receipt-dir ~/day1
```

Notes:
- `capture-loop` runs **on the board**, against the service on that same machine. `--host` must be
  `127.0.0.1` or `localhost` (IPv6 `::1` is refused because the proof builds an unbracketed URL).
  The serial is read from local sysfs and passed to the proof; the service checks it at the port
  on its own host, but the before/after identity and the throttle log are this machine's, so a
  remote host is refused.
- `capture-loop` calls `deploy/pi_bringup.sh --apply --phase proof --serial <serial>` with the serial
  sysfs reported at `--port`. The body at `--port`
  must report the USB serial of `--body` before anything fires, and the same serial must still be on
  the bus afterwards. `kill_criterion_passed` is true only when requested == recorded == HTTP 200 ==
  verified ≥ 20 (a 0/0 proof never counts), the serial matches, no throttle or under-voltage flag
  appears, and RAW transfer passes.
- **RAW transfer** (`deploy/raw_proof.py`). For each shot the service records, in
  `evidence.transport_receipt.downloaded_files`, the name, size and SHA-256 of every file the
  camera delivered (ARW and JPEG), hashed on the board from the bytes as stored, before it discards
  the ARW (the product keeps only the JPEG, so the hash proves what crossed USB, not a kept copy).
  The proof copies them into each row of `proof-shots.jsonl` and judges them in
  `proof.json` → `raw_transfer`: `passed` only when every accepted shot has exactly one ARW with a
  SHA-256 and a size inside the body's band (24,000,000–26,000,000 bytes for both bodies, from the
  owner's compressed ARWs), and no file hash repeats across shots. Otherwise the verdict names the
  defect: `raw_missing`, `raw_ambiguous`, `raw_unhashed`, `raw_implausible`, `duplicate_hash`,
  `raw_bounds_unknown` (a body with no band) or `no_accepted_shots`, and the proof fails. An
  uncompressed ARW (~49 MB) is outside the band and fails as `raw_implausible`. A `proof.json`
  without `raw_transfer` leaves `raw_plus_jpeg_download` unverified (inconclusive, exit 3).
- Running the proof by hand needs the serial too, because `/shoot` refuses a request that names a
  model and port without one:
  `bash deploy/pi_bringup.sh --apply --phase proof --model "Sony Alpha-A7 III (PC Control)" --port usb:BBB,DDD --serial A7III0SERIAL --token-file ~/.config/camera-autopilot/api-token`.
  Get the serial with `python3 deploy/day_one.py autodetect --run --body a7iii --receipt-dir ~/day1` (the receipt's
  `sony_sysfs` rows list each Sony body's port and serial), or, read-only from the repo root,
  `backend/.venv/bin/python -c 'import json, sonyremote; print(json.dumps(sonyremote.detect_identified(), indent=2))'`
  (the `serial` of the row at your port). `tools/inspect_usb_camera.py --list` shows model and port
  but not the serial.
- `usb-replug-idle` and `sleep-wake` never run `sudo`. As root they write sysfs `unbind`/`bind`
  themselves; otherwise they print the two commands and wait for you. `physical_power_cycle_needed`
  is the answer the battery puck needs.
- **Not covered:** a USB interruption **mid-session**, while the service is capturing. The only
  test stops the service and interrupts an idle body. Pulling power during a live exposure risks an
  uncertain shutter, and this tool can't coordinate that safely with the service's reconnect path,
  so the gap stays open until it is done by hand and recorded as a human-gated receipt.
- `sleep-wake` only reads passively while the body sleeps: sysfs, plus the `gphoto2 --auto-detect`
  label, which is believed not to wake the body (unverified). It records whether the body stayed
  on USB and what label gphoto2 gave it. An asleep A7 III has been seen listed as an A7 II.
- `ap-soak` needs `iw` (`command -v iw || sudo apt-get install -y iw`). It logs phones by a hashed
  station ID, never a MAC address, and rewrites its receipt every sample so a crash still leaves evidence.
- `merge-thermal` loops `tools/puck_workload_bench.py` on real, same-size 24 MP JPEGs from a bracket.

**Kill criterion** (unchanged): if either body fails the 20-shot proof because of the board or the
carrier (not the camera), fall back to the Raspberry Pi 5 8GB. If only the AP soak fails, add a
small hub plus the ALFA AWUS036ACHM on the single USB-A port, or move to a two-port carrier.

## Accepted residual risks

These are known, deliberately not engineered away, and recorded so nobody mistakes them for
guarantees:

- **"Local" means this machine.** `capture-loop` proves identity by reading the USB serial from
  this machine's sysfs and then firing the service at `127.0.0.1`/`localhost`. An SSH tunnel that
  forwards local port 8000 to another host would make a remote service look local and defeat that
  proof. Never use a tunnel or port-forward for the kill-criterion run: run it on the board, against
  the service on the board.
- **Ancestor-directory swap race.** `safe_paths` validates a receipt or staging path, then the kit
  writes it. Someone who can write to an ancestor directory could swap it for a symlink between
  those two steps. Doing that needs local write access on the owner's own board, so this is an
  accepted risk, not a fixed one. The atomic, non-following receipt writes and the planted-symlink
  refusal narrow the window without closing it.
- **`receipts/proof-shots.jsonl` is truncated in place** by the truncating redirect in
  `pi_bringup.sh`'s proof phase. Unlike the JSON receipts, which are replaced atomically, a
  pre-existing hard link, or a symlink planted after the directory scan, at that path would be
  written through. The same local write access is needed. Accepted for now.
- **Bytecode.** The entry points set `sys.dont_write_bytecode` as their first statement, and
  `pi_bringup.sh` exports `PYTHONDONTWRITEBYTECODE=1`. Run them **as scripts**
  (`python3 deploy/day_one.py …`), or set `PYTHONDONTWRITEBYTECODE=1`. `python -m deploy.…` and
  plain imports can write `__pycache__` before the flag runs.
- Path arguments containing control characters (newline included) are refused everywhere, and so is
  an output path whose resolved target contains one (a `--receipt-dir` symlink to `receipts\n` would
  otherwise be written as `receipts` after the shell strips the newline).

## Sources

- Waveshare CM5-NANO-A wiki (ports, 2 A USB-A limit, BOOT flashing, fan curve, TF-slot FAQ, Bookworm date): https://www.waveshare.com/wiki/CM5-NANO-A
- Waveshare "Compute Module Burn EMMC" (BOOT sequence, Imager device choice): https://www.waveshare.com/wiki/Compute_Module_Burn_EMMC
- Raspberry Pi usbboot README (macOS build, CM5 nRPIBOOT = GPIO 20): https://github.com/raspberrypi/usbboot
- Raspberry Pi docs, *Flash an image to a Compute Module* (macOS `rpiboot -d mass-storage-gadget64`, "Ignore" dialog): https://www.raspberrypi.com/documentation/computers/compute-module.html#flash-compute-module-emmc
- Raspberry Pi docs, *Install an operating system* (Imager customisation: hostname/mDNS, localisation sets the Wi-Fi regulatory domain, SSH public key): https://www.raspberrypi.com/documentation/computers/getting-started.html#install
- Raspberry Pi docs, *Configuring networking* (WLAN disabled until a country is set): https://www.raspberrypi.com/documentation/computers/configuration.html#wlan-country
- Raspberry Pi docs, *Find your IP address* (mDNS `.local`, nmap ping scan): https://www.raspberrypi.com/documentation/computers/remote-access.html#ip-address
- Raspberry Pi docs, `config.txt` conditional filters (`[cm5]`, `[all]`): https://www.raspberrypi.com/documentation/computers/config_txt.html#conditional-filters
- Raspberry Pi docs, `vcgencmd get_throttled` bit meanings: https://www.raspberrypi.com/documentation/computers/os.html
- Device-tree overlay README (`i2c_arm`, `fan_temp0..3`, `ant1/ant2`): https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/README
- brcmfmac AP pitfalls: see the table in section 5 and `docs/research/board-2026-09-22/failure_modes.md`.
