# FRC SystemCore on Raspberry Pi 5

Run [Limelight SystemCore OS](https://github.com/LimelightVision/systemcore-os-public) on a standard Raspberry Pi 5 Model B instead of the Compute Module 5 it was designed for.

## Quick Start

```bash
git clone https://github.com/netarcx/systemcore-rpi5-guide.git
cd systemcore-rpi5-guide
sudo ./build-image.sh
```

This produces `systemcore-pi5b-beta10-v1.img` — flash it to an SD card and boot:

```bash
sudo dd if=systemcore-pi5b-beta10-v1.img of=/dev/sdX bs=4M status=progress
```

Insert the SD card into your Pi 5 and power on. No further configuration needed.

## Patching new upstream releases (`patch-image.py`)

When WPI/Limelight ships a new SystemCore image, you can patch it directly without re-running the full build:

```bash
# GUI:
sudo python3 patch-image.py

# Or headless:
sudo python3 patch-image.py upstream.img -o patched.img
```

The patcher detects partition offsets dynamically (via `sfdisk`), so it survives layout changes between releases. It applies the same set of patches `build-image.sh` does, but skips the image download.

### CLI options

```
sudo python3 patch-image.py [input.img] [options]

  -o, --output PATH         Output image (default: <input>-pi5b.img)
  --dry-run                 Log everything but don't modify anything
  -v, --verbose             Show every shell command
  --backup                  Copy input to .bak before in-place patching
  --keep-mounted            Don't unmount partitions on success
  --no-cleanup-on-error     Leave mounts open if a patch fails (for debugging)
  --skip-b                  Patch A partitions only, skip B
  --validate                Re-mount the output and verify expected files
  --inspect                 Mount partitions, print paths, wait for ENTER
  --show-partitions         Print partition layout and exit
  --list-patches            List every patch with a description
  --only PATCH,PATCH        Apply only these patches (everything else off)
  --no-<patch>              Skip a single patch (e.g. --no-install-mrccan)
```

Run `sudo python3 patch-image.py --list-patches` for the full set of patch names.

### GUI

The GUI is a Tkinter app (`apt install python3-tk` if missing). Launch with no arguments to bring up the window, or pass `--gui` to force GUI even when positional args are given.

```bash
sudo python3 patch-image.py
```

The window is laid out top-to-bottom in seven sections:

**1. Image files** — pickers for the input image and the output image. Selecting an input auto-fills the output as `<input>-pi5b.img` next to it (override with the second Browse button).

**2. Source paths** — auto-filled from the repo:
- `flash-pico.sh`: path to the Pico flasher script that gets installed into `/usr/local/bin/`
- `wireless-regdb .deb`: path to the regdb Debian package extracted into `/usr/lib/firmware/`

Each path has a Browse button if the default isn't right.

**3. Boot partition patches (A + B)** — checkboxes for the boot-side patches:
- `Enable HDMI` — uncomment the display options in `config.txt`
- `Disable carrier SPI CAN overlays` — comment out the CM5 carrier's `dtoverlay=spi*` / `dtoverlay=sc-mcp2518` lines
- `Waveshare CAN FD HAT overlays` — append the `mcp251xfd` overlay block for the [Waveshare 2-CH CAN FD HAT](#waveshare-2-ch-can-fd-hat-can-fd)
- `Add panic=0 + US wifi regdom` — append kernel cmdline params

**4. Rootfs patches (A + B)** — checkboxes for the rootfs-side patches:
- `Install flash-pico.sh` — install the script + service override that lets external Picos be flashed
- `USB-CAN udev rule` — install `90-usb-can-rename.rules` (scoped to USB/SPI so vcan placeholders don't trigger restart loops)
- `canbusprocess override (vcan placeholders)` — install the override that names USB-CAN adapters and fills any missing `can_s0..can_s4` slot with a vcan interface (HAL requires all 5)
- `canbuswatchdog override` — install watchdog override that waits for any `can_s*` instead of requiring all 5
- `robot.service override` — 30-second CAN wait then start regardless
- `/dev/mrccan tmpfile (MrcCommDaemon fix)` — install `tmpfiles.d/mrccan.conf` so `MrcCommDaemon` can write its control files at boot (without this the robot program SIGABRTs ~10s after start)
- `Wireless regulatory database` — install `regulatory.db` so WiFi works on the US regdom
- `Dashboard: unlock WLAN0 AP` — patch the minified React JS to allow editing Access Point network config
- `Dashboard: fault count reset button` — add a "Reset Fault Counts" button to the fault tooltip in the header

Hover any checkbox for a tooltip explaining what that patch does.

**5. Debug + advanced** — toggles that change *how* patching runs rather than *what* it does:
- `Dry run (log only)` — log every operation but don't write anything to the image
- `Verbose log` — show every shell command (sfdisk, mount, dpkg-deb, etc.)
- `Backup input image` — copy input to `<input>.bak` before in-place patching (no-op if input ≠ output)
- `Keep mounted after` — leave the partition loop-mounts open on success so you can poke around with a file manager or shell. Cleanup is your problem.
- `No cleanup on error` — leave the loop-mounts open if a patch fails, so you can inspect partial state. Use for diagnosing why a patch broke.
- `Patch A only (skip B)` — only patches the A boot+rootfs, leaves B alone. Useful when testing a patch against just one boot slot.
- `Validate after patch` — re-mount the output image and verify the expected files (override.conf paths, tmpfile config, flash-pico.sh) are present in both rootfs A and B.

**6. Action buttons:**
- `Patch image` — apply all enabled patches. Disabled while a job is running.
- `Inspect (mount only)` — mount every partition (boot A/B, rootfs A/B) on the input or output image and show a dialog with the mount points. Click OK in the dialog when done to unmount everything.
- `Validate` — re-mount the output (or input) image and verify expected post-patch files are present. Pops up a dialog reporting any missing files.
- `Show partitions` — print the partition layout (offsets, sizes, detected filesystems, identified boot/root A/B mapping) to the log.
- `Cancel` — set a cancel flag the worker can observe. Subprocess calls already in flight aren't interrupted, so this is "best effort" rather than instant.
- `Quit` — close the window. Doesn't unmount anything — if you'd been using Keep mounted or No cleanup on error, run `umount` manually first.

**7. Progress bar + status + log viewer** — the bottom half is a scrolling log with timestamps. Errors render red, warnings amber, debug messages grey. `Clear` empties the log; `Save log...` writes it to a file (handy for sharing diagnostics).

## What `build-image.sh` does

The script automates everything needed to convert the upstream CM5 image into a Pi 5B-compatible image:

1. **Downloads** the upstream SystemCore Beta 10 image from GitHub (cached after first download)
2. **Patches both boot partitions** (A/B) — enables HDMI, disables the carrier's SPI CAN overlays, adds the Waveshare CAN FD HAT overlays, updates cmdline
3. **Patches both rootfs partitions** (A/B) — installs Pico flasher, CAN adapter support, dashboard patches

## What gets patched

### HDMI output

The stock image disables all display output (headless for Limelight hardware). The build script enables HDMI for debugging by commenting out `hdmi_ignore_hotplug`, `hdmi_blanking`, `ignore_lcd` and setting `display_auto_detect=1`.

### SPI CAN overlays disabled

The CM5 carrier board has 5 MCP2518FD SPI CAN controllers wired through Limelight's `sc-mcp2518` overlays. The Pi 5B has none of that wiring, so those overlay lines are commented out.

### Waveshare 2-CH CAN FD HAT (CAN FD)

For real CAN FD, use the [Waveshare 2-CH CAN FD HAT](https://www.waveshare.com/wiki/2-CH_CAN_FD_HAT): two MCP2518FD controllers on the 40-pin header, using the mainline `mcp251xfd` driver. The image appends this block to `config.txt` on both boot partitions:

```
dtparam=spi=on
dtoverlay=spi1-3cs
dtoverlay=mcp251xfd,spi0-0,interrupt=25
dtoverlay=mcp251xfd,spi1-0,interrupt=24
```

This matches the HAT's factory-default **dual-SPI "A" mode**: CAN_0 is on SPI0 CE0 with IRQ on GPIO25, and CAN_1 is on SPI1 CE0 with IRQ on GPIO24. The block is always on. Without the HAT, the driver probe just fails and no interface appears. GPIO 24/25 and the SPI0/SPI1 pins are claimed either way, so don't use them for anything else.

**Hardware setup:**
- Set the VIO jumper to **3.3V**. The Pi uses 3.3V logic.
- Fit the 120Ω termination jumper only if the HAT sits at an end of the bus.
- If you moved the 0R resistors to single-SPI **"B" mode**, edit `config.txt` on both boot partitions and change the last overlay line to `dtoverlay=mcp251xfd,spi0-1,interrupt=24`.

**Naming and mode:**
- CAN_0 → **`can_s0`** and CAN_1 → **`can_s1`**, always. They are matched by SPI device (`spi0.0`, then `spi1.0` or `spi0.1`), not by discovery order.
- When the HAT is present, USB adapters are allocated from `can_s2` upward. Any old `/etc/can_port_map` entry that put a USB port on `can_s0`/`can_s1` is reassigned automatically.
- HAT buses default to **CAN FD, 1 Mbps nominal / 2 Mbps data** (sample points 87.5% / 75%). That is the CTRE CANivore-standard timing. The MCP2518FD is a native ISO CAN FD controller and the driver enables transmitter delay compensation, so the gs_usb FD problems described below don't apply. To force classic CAN on a HAT channel, add `can_s0=classic` to `/etc/can_bus_mode`.
- Check it: `ip -d link show can_s0` should show `<FD>`, `bitrate 1000000`, `dbitrate 2000000`, and `state ERROR-ACTIVE`. `dmesg | grep mcp251xfd` should show both controllers detected.

### Pico flasher (`flash-pico.sh`)

The stock `picoflasherprocess` binary only flashes Pico microcontrollers connected to the Limelight's internal USB controller. On Pi 5B, external Picos are rejected.

The build script replaces it with `flash-pico.sh` via a systemd override. This script:
- Polls for any Pico in BOOTSEL mode on any USB port (vendor ID `2e8a`)
- Flashes `fw.uf2` via `dd` to the raw block device
- Works with RP2350 Picos (RP2040 not supported — firmware is RP2350-specific)
- After flashing, the Pico appears as `cafe:4011` ("Limelight RT Subsystem")

### Multi-adapter USB-CAN support (optional)

The stock image expects 5 SPI CAN interfaces (`can_s0` through `can_s4`). The build script adds support for any number of USB-to-CAN adapters, alongside the CAN FD HAT if one is fitted:

- **Udev rule** triggers the CAN service restart when an adapter is plugged in. The match is scoped to `SUBSYSTEMS=="usb"` so vcan placeholders (see below) don't re-trigger the service and cause an infinite restart loop.
- **canbusprocess override** discovers all CAN interfaces, renames them to `can_s0`, `can_s1`, etc., and configures each as **classic CAN at 1Mbps by default** — the conservative choice that works with any device. CAN FD is opt-in per bus via `/etc/can_bus_mode`. **Do not raise the FD data bitrate to 5Mbps** — the bits are too short for the device transceivers to decode, ACKs never come back, the bus silently goes `ERROR-PASSIVE`, and Phoenix 6 TX appears stuck (TX packets counter frozen, TX dropped climbing).
- **Persistent port mapping** — each USB port path is mapped to a stable `can_sN` index in `/etc/can_port_map`, so the same physical port always gets the same name regardless of plug order (works with USB hubs)
- **Discovery frame** — `cansend 000#00` sent on each bus after interface up
- **Hot-plug** — plugging in a new adapter triggers automatic naming and configuration
- **vcan placeholders auto-fill missing buses** — the WPILib HAL iterates `can_s0` through `can_s4` and aborts the robot program if any are missing (`ioctl(SIOCGIFINDEX) for CAN can_sN failed with No such device` → `Failed to initialize. Terminating`). After USB-CAN setup, the service creates vcan interfaces for whichever slots have no physical adapter, so 0–4 USB adapters all work.
- **canbuswatchdog/robot.service overrides** — wait for any CAN adapter, start regardless after 30s

If no CAN adapter is plugged in, all services time out gracefully and the robot starts anyway (vcan placeholders satisfy the HAL).

Compatible with any SocketCAN-supported USB adapter (candleLight/canable, PEAK, EMS, etc.).

> **WPILib version note.** Tested against WPILib 2027 Alpha 2 + Phoenix 6 25.90.0-alpha-2. The `can_s0..can_s4` naming, vcan-placeholder requirement, MrcCommDaemon control-file gate, and netcomm key all reflect that HAL surface — later alphas may rearrange or drop any of them. Re-validate when bumping WPILib.

#### Overriding the bus mode per bus

`/etc/can_bus_mode` is read at every `limelight_canbusprocess.service` start. Unlisted USB buses default to **classic CAN at 1Mbps**. Unlisted Waveshare HAT buses default to **CAN FD at 1Mbps/2Mbps**. Format:

```
can_sN=<classic|fd> [bitrate] [dbitrate]
```

- `mode`: `classic` or `fd` (default `classic` for USB, `fd` for the HAT)
- `bitrate`: nominal bitrate in bps (default `1000000`)
- `dbitrate`: data-phase bitrate for FD only (default `2000000`, the CTRE CANivore standard)

Examples:

```
# Bus 0 stays default classic 1Mbps (line not needed)
can_s1=fd                    # FD at 1Mbps/2Mbps — verify device responds before relying on this
can_s2=classic 500000        # classic at 500kbps
```

After editing, `sudo systemctl restart limelight_canbusprocess.service` (or reboot) to apply.

> **Why classic and not FD by default.** Every current Phoenix 6 device (TalonFX, Kraken, CANcoder, Pigeon 2, CANdle, CANdi, CANrange, TalonFXS) is CAN FD capable — but CTRE's FD implementation is built around the [CANivore](https://store.ctr-electronics.com/canivore/), their own USB-CAN adapter with an integrated CTRE-authored SocketCAN kernel driver. The generic candleLight-style `gs_usb` adapter most teams use is exactly the "hobbyist-style SocketCAN-USB product" CTRE built the CANivore to replace — they do not guarantee FD compatibility with it. Observed on this rig (gs_usb + CANdle + Phoenix 6 alpha-2): FD at 1M/5M OR 1M/2M lets the kernel TX frames but the device never acts on them. Likely culprits (not yet bisected): ISO vs non-ISO CRC mismatch, missing transceiver delay compensation (TDC) at 2 Mbps, or sample-point/SJW mismatch. Phoenix Tuner discovering the device is **not** sufficient verification — kernel TX counters can climb while the device silently ignores every frame. If you have a CANivore, use it for FD; if you have a gs_usb adapter, stay on classic.

### MrcCommDaemon unblock (`/dev/mrccan/`)

`MrcCommDaemon` is the userspace service that sets the NetworkTables key `/Netcomm/Control/ServerReady`. The WPILib HAL waits on this key during robot startup — if `MrcCommDaemon` isn't running, the Java robot program SIGABRTs ~10 seconds after launch with `Error: Waiting for server ready failed. Restarting app and retrying...` and `terminate called without an active exception`.

The daemon writes its state to `/dev/mrccan/controldata` and `/dev/mrccan/matchinfo`. On real SystemCore hardware that directory is created by a kernel module specific to the carrier board; on Pi 5B the module doesn't exist, so the daemon crash-loops with `Failed to open control data file`.

The build script installs `/etc/tmpfiles.d/mrccan.conf` so systemd-tmpfiles creates `/dev/mrccan/` early in boot, before `mrccomm.service` starts. Both files are then regular files written by the daemon itself.

### Dashboard patches

The build script patches the Limelight dashboard (minified React JS) to:

- **Unlock WLAN0 AP settings** — the stock dashboard disables editing Access Point network config. The patch removes the disabled flag and the forced IP/gateway overrides on save.
- **Fault count reset button** — adds a "Reset Fault Counts" button to the fault tooltip in the header. Uses a frontend-only baseline offset (no backend changes needed for the closed-source `diagnosticsprocess`).

### Wireless regulatory database

The stock image is missing `regulatory.db`. The build script installs the US regulatory database so WiFi works correctly (paired with `cfg80211.ieee80211_regdom=US` in the kernel cmdline).

## Known limitations

- **RP2350 firmware faults** — After flashing, the Pico firmware reports faults for hardware it expects on the carrier board (BROWNOUT, IMU, DISPLAY, CAN, RSL). These are cosmetic — USB communication works fine. The firmware is closed-source so these cannot be fixed. Use the "Reset Fault Counts" button to clear them.
- **RP2040 not supported** — `fw.uf2` is RP2350-specific. An RP2040 Pico will accept the copy but reboot back to BOOTSEL in a loop.
- **USB gadget mode** — The `dwc2` overlay behavior may differ between CM5 and Pi 5B.

## Project layout

```
build-image.sh          - End-to-end image builder (run with sudo)
patch-image.py          - Standalone patcher for new upstream releases (GUI + CLI)
patcher/                - Python package for patch-image.py
  core.py               - Mount + per-patch logic + orchestrator
  gui.py                - Tkinter GUI
  cli.py                - argparse entry point
  resources/            - Drop-in files installed into the rootfs
    90-usb-can-rename.rules
    canbusprocess-override.conf
    canbuswatchdog-override.conf
    robot-override.conf
    picoflasher-override.conf
    mrccan.conf
netboot/                - Network boot setup (development/debugging)
  flash-pico.sh         - Pico flasher replacement (installed into image)
  setup-netboot.sh      - Sets up TFTP + NFS on WSL2 for netboot
  cmdline_nfs.txt       - Kernel cmdline template for NFS root
```

Files not tracked in git (generated/downloaded):
```
cache/                            - Downloaded upstream image zip
systemcore-pi5b-beta10-v1.img     - Output image (~14GB)
netboot/tftpboot/                 - TFTP boot files (kernel, DTBs, overlays)
netboot/nfsroot/                  - NFS root mount point
```

## Network boot (development)

For iterative development, the Pi 5 can netboot from a WSL2 host via TFTP + NFS:

```bash
sudo ./netboot/setup-netboot.sh
```

This installs `tftpd-hpa` and `nfs-kernel-server`, configures exports, and prints Pi 5 EEPROM settings. WSL2 must use **mirrored networking** mode (set `networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`).

## Prerequisites

- Linux host for building (Ubuntu/Debian, WSL2 works)
- ~15GB free disk space (upstream image + patched output)
- `sudo` access (for loop-mounting image partitions)

## Tested on

- Raspberry Pi 5 Model B (4GB/8GB)
- SystemCore Beta 10 (`limelightosr-beta-10-139`)
- Kernel: stock upstream 16K-page kernel (works on Pi 5B as-is since Beta 10)
- Host: WSL2 on Windows 11

## License

The kernel is licensed under GPL-2.0 (same as the Linux kernel). Boot configuration files and scripts are provided as-is.
