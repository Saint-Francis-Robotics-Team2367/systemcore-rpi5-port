"""Image patching core: mounting, individual patches, and orchestration.

The patcher operates on a working copy of the image. Each patch is a small
function that takes a mount point + options and mutates files inside it. The
orchestrator wires patches up to options, mounts each partition once, runs the
patches that apply, and unmounts. Mounts are tracked in a registry so that
even on hard failure the cleanup path can unmount everything (unless
`--no-cleanup-on-error` is set, in which case mounts are left in place for
inspection).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import dashboard


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCES = PACKAGE_ROOT / "resources"
PROJECT_ROOT = PACKAGE_ROOT.parent

# Resource files copied verbatim into the rootfs.
RES_UDEV_CAN = RESOURCES / "90-usb-can-rename.rules"
RES_CANBUSPROCESS = RESOURCES / "canbusprocess-override.conf"
RES_CANBUSWATCHDOG = RESOURCES / "canbuswatchdog-override.conf"
RES_ROBOT = RESOURCES / "robot-override.conf"
RES_PICOFLASHER = RESOURCES / "picoflasher-override.conf"
RES_MRCCAN = RESOURCES / "mrccan.conf"
RES_CANFD_HAT = RESOURCES / "canfd-hat-config.txt"
RES_STOCK_CAN_NAMES = RESOURCES / "70-can-interface-names.rules"

# Beta 15+ ships robot_heartbeat.ko, which creates /dev/mrccan/* itself. The
# Beta 10 tmpfiles.d workaround must NOT be installed alongside it (a
# pre-created /dev/mrccan dir lets MrcCommDaemon create plain files there,
# which block the module's device nodes).
HEARTBEAT_MODULE_GLOB = "lib/modules/*/kernel/net/can/robot_heartbeat.ko*"

# Waveshare 2-CH CAN FD HAT config.txt block (delimited by BEGIN/END markers
# so re-patching replaces it instead of appending a second copy).
CANFD_HAT_BEGIN = "# BEGIN waveshare-canfd-hat"
CANFD_HAT_END = "# END waveshare-canfd-hat"
# Beta 15 routes I2C1 onto GPIO10/11 — the SPI0 MOSI/SCLK pins the CAN FD
# HAT's CAN_0 uses. Comment it out whenever the HAT block is installed.
I2C1_ON_SPI0_RE = r"^(dtoverlay=i2c1,pins_10_11.*)$"
CANFD_HAT_BLOCK_RE = re.compile(
    rf"^{re.escape(CANFD_HAT_BEGIN)}$.*?^{re.escape(CANFD_HAT_END)}$\n?",
    re.MULTILINE | re.DOTALL,
)

# Project-relative defaults the user can override.
DEFAULT_FLASH_PICO = PROJECT_ROOT / "netboot" / "flash-pico.sh"
DEFAULT_REGDB_DEB = (
    PROJECT_ROOT / "beta9" / "wireless-regdb_2025.10.07-0ubuntu1~24.04.1_all.deb"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PatcherError(Exception):
    """A patch step failed in a way that should abort the run."""


class PreflightError(PatcherError):
    """A pre-run check failed — typically missing tools, missing root, or
    a malformed source image."""


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class PatchOptions:
    """All knobs exposed by the GUI and CLI.

    Patch toggles default to True so the default behavior matches what
    `build-image.sh` does. Set any to False to skip that patch.
    """

    # --- I/O ---
    input_image: Path = Path("")
    output_image: Path = Path("")
    flash_pico_path: Path = DEFAULT_FLASH_PICO
    regdb_deb_path: Path = DEFAULT_REGDB_DEB

    # --- Boot partition patches ---
    enable_hdmi: bool = True
    disable_spi_can: bool = True
    enable_canfd_hat: bool = True
    update_cmdline: bool = True

    # --- Rootfs patches ---
    install_flash_pico: bool = True
    install_can_udev: bool = True
    install_canbusprocess: bool = True
    install_canbuswatchdog: bool = True
    install_robot_override: bool = True
    install_mrccan: bool = True
    install_regdb: bool = True
    patch_dashboard_wlan: bool = True
    patch_dashboard_faults: bool = True

    # --- Operational flags ---
    dry_run: bool = False
    verbose: bool = False
    backup: bool = False
    keep_mounted: bool = False
    cleanup_on_error: bool = True
    validate_after: bool = False
    skip_b_partitions: bool = False  # only patch A, leave B alone

    def patch_names(self) -> list[str]:
        """Names of every patch toggle, used for --list-patches and the GUI."""
        return [
            "enable_hdmi",
            "disable_spi_can",
            "enable_canfd_hat",
            "update_cmdline",
            "install_flash_pico",
            "install_can_udev",
            "install_canbusprocess",
            "install_canbuswatchdog",
            "install_robot_override",
            "install_mrccan",
            "install_regdb",
            "patch_dashboard_wlan",
            "patch_dashboard_faults",
        ]


PATCH_DESCRIPTIONS: dict[str, str] = {
    "enable_hdmi": "Uncomment HDMI display options in config.txt",
    "disable_spi_can": "Comment out the CM5 carrier's spi/sc-mcp2518 overlays",
    "enable_canfd_hat": "Add mcp251xfd overlays for the Waveshare 2-CH CAN FD HAT",
    "update_cmdline": "Add panic=0 and cfg80211.ieee80211_regdom=US to cmdline.txt",
    "install_flash_pico": "Install flash-pico.sh + picoflasherprocess override",
    "install_can_udev": "Install CAN hot-plug udev rule; disable stock carrier CAN naming",
    "install_canbusprocess": "Install canbusprocess override with vcan placeholders",
    "install_canbuswatchdog": "Install canbuswatchdog override (waits for any can_s*)",
    "install_robot_override": "Install robot.service override (30s CAN wait, optional)",
    "install_mrccan": "Pre-Beta 15 only: /etc/tmpfiles.d/mrccan.conf (skipped if robot_heartbeat.ko exists)",
    "install_regdb": "Install wireless-regdb so WiFi works on US regulatory domain",
    "patch_dashboard_wlan": "Unlock WLAN0 AP settings in the dashboard JS",
    "patch_dashboard_faults": "Add a 'Reset Fault Counts' button to the dashboard",
}


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    log: logging.Logger,
    check: bool = True,
    capture: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess command with consistent logging.

    `dry_run=True` only logs the command and returns a fake CompletedProcess —
    use this for any command that mutates state. Read-only commands (sfdisk,
    file, parted) should pass dry_run=False even in dry-run mode so the run
    can still inspect the image.
    """
    log.debug("$ %s", " ".join(str(c) for c in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=capture,
    )


def run_text(cmd: list[str], log: logging.Logger) -> str:
    """Run a read-only command and return stdout as text."""
    res = run(cmd, log, capture=True)
    return res.stdout


# ---------------------------------------------------------------------------
# Partition discovery
# ---------------------------------------------------------------------------


@dataclass
class Partition:
    index: int          # 1-based partition number
    start_bytes: int    # byte offset of partition start within the image
    size_bytes: int     # partition size in bytes
    fs: str             # detected filesystem ("vfat", "ext4", "extended", "unknown")

    @property
    def end_bytes(self) -> int:
        return self.start_bytes + self.size_bytes


@dataclass
class ImageLayout:
    """Identified partitions of interest inside an image file."""

    image: Path
    partitions: list[Partition]
    boot_a: Optional[Partition] = None
    boot_b: Optional[Partition] = None
    root_a: Optional[Partition] = None
    root_b: Optional[Partition] = None


def detect_layout(image: Path, log: logging.Logger) -> ImageLayout:
    """Detect partition offsets and identify the boot/root A/B partitions.

    Beta 10 layout (boot selector + A/B kernel/rootfs) has 6 partitions; we
    identify them by size and filesystem rather than by index so that the
    patcher survives layout changes in future upstream releases.
    """
    log.info("Detecting partition layout of %s", image)

    # sfdisk -d gives a stable, parseable dump of the partition table.
    dump = run_text(["sfdisk", "-d", str(image)], log)

    partitions: list[Partition] = []
    line_re = re.compile(
        r"^\S+\s*:\s*start=\s*(\d+),\s*size=\s*(\d+),.*type=([0-9a-fA-Fx]+)"
    )
    for line in dump.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        start_sec, size_sec, ptype = m.groups()
        start = int(start_sec) * 512
        size = int(size_sec) * 512
        fs = _detect_fs(image, start, ptype, log)
        partitions.append(
            Partition(index=len(partitions) + 1, start_bytes=start, size_bytes=size, fs=fs)
        )

    if not partitions:
        raise PreflightError(f"sfdisk found no partitions in {image}")

    # Identify by signature: there are exactly two ~64M FAT32 partitions
    # (boot A/B) and two ~7G ext4 partitions (rootfs A/B).
    fat_parts = [p for p in partitions if p.fs == "vfat" and p.size_bytes < 200 * 1024 * 1024]
    ext_parts = [p for p in partitions if p.fs == "ext4"]

    # Sort by start offset so A always comes before B.
    fat_parts.sort(key=lambda p: p.start_bytes)
    ext_parts.sort(key=lambda p: p.start_bytes)

    # The first FAT might be the small "boot selector" (16M). Drop anything
    # smaller than 30M from the boot candidates so we only keep the two ~64M
    # kernel boot partitions.
    boot_candidates = [p for p in fat_parts if p.size_bytes >= 30 * 1024 * 1024]

    layout = ImageLayout(image=image, partitions=partitions)
    if len(boot_candidates) >= 1:
        layout.boot_a = boot_candidates[0]
    if len(boot_candidates) >= 2:
        layout.boot_b = boot_candidates[1]
    if len(ext_parts) >= 1:
        layout.root_a = ext_parts[0]
    if len(ext_parts) >= 2:
        layout.root_b = ext_parts[1]

    for label, part in [
        ("boot_a", layout.boot_a),
        ("boot_b", layout.boot_b),
        ("root_a", layout.root_a),
        ("root_b", layout.root_b),
    ]:
        if part:
            log.info(
                "  %s -> partition %d, %s, offset=%d, size=%.1f MB",
                label,
                part.index,
                part.fs,
                part.start_bytes,
                part.size_bytes / (1024 * 1024),
            )
        else:
            log.warning("  %s -> NOT FOUND", label)

    return layout


def _detect_fs(image: Path, offset: int, ptype: str, log: logging.Logger) -> str:
    """Identify the filesystem at a given offset using `file -s` semantics.

    We dd a small slice to a temp file and run `file` on it — that's the
    only way to identify FS type without setting up a loop device.
    """
    if ptype.lower() in ("5", "0x5", "f", "0xf"):
        # Extended partition container; no real filesystem.
        return "extended"

    # Peek at the first 8KB — enough for superblocks of common filesystems.
    with open(image, "rb") as f:
        f.seek(offset)
        head = f.read(8192)

    # ext2/3/4 magic: 0xEF53 at offset 0x438 from start of filesystem.
    if len(head) >= 0x43A and head[0x438:0x43A] == b"\x53\xef":
        return "ext4"
    # FAT: look for "MSWIN", "FAT16", "FAT32", or the OEM name at offset 3.
    if b"FAT32" in head[:90] or b"FAT16" in head[:90] or b"mkfs.fat" in head[:90]:
        return "vfat"
    if head[510:512] == b"\x55\xaa":
        # Has a boot signature but no recognized FS — might still be FAT
        # (some images don't write the FAT32 string in the OEM field).
        return "vfat"

    return "unknown"


# ---------------------------------------------------------------------------
# Mount tracking
# ---------------------------------------------------------------------------


@dataclass
class MountTracker:
    """Tracks active mounts so they can be torn down in LIFO order on cleanup."""

    mounts: list[Path] = field(default_factory=list)

    def push(self, path: Path) -> None:
        self.mounts.append(path)

    def cleanup(self, log: logging.Logger) -> None:
        # Unmount in reverse order in case of nested mounts.
        while self.mounts:
            mnt = self.mounts.pop()
            try:
                run(["umount", str(mnt)], log)
                mnt.rmdir()
            except subprocess.CalledProcessError:
                log.warning("Failed to unmount %s (already gone?)", mnt)
            except OSError:
                log.warning("Mount point %s couldn't be removed", mnt)


@contextmanager
def mount_partition(
    image: Path, part: Partition, log: logging.Logger, tracker: MountTracker
) -> Iterator[Path]:
    """Loop-mount a partition at `part.start_bytes` and yield the mount point.

    The mount point is registered with `tracker` so a top-level finally block
    can guarantee cleanup even when an inner patch raises. If the mount call
    itself fails, the temp directory is removed before re-raising so we don't
    leak `/tmp/patcher-mnt-*` directories.
    """
    mnt = Path(tempfile.mkdtemp(prefix="patcher-mnt-"))
    log.debug("Mounting partition %d at %s", part.index, mnt)
    try:
        run(
            [
                "mount",
                "-o",
                f"loop,offset={part.start_bytes},sizelimit={part.size_bytes}",
                str(image),
                str(mnt),
            ],
            log,
        )
    except Exception:
        mnt.rmdir()
        raise
    tracker.push(mnt)
    # Caller may want to keep mounts open (--keep-mounted). The tracker is
    # the source of truth — if we removed `mnt` from it the cleanup phase
    # will skip it, so the with-block exit doesn't need to unmount here.
    yield mnt


# ---------------------------------------------------------------------------
# File mutation helpers
# ---------------------------------------------------------------------------


def sed_inplace(path: Path, pattern: str, replacement: str, log: logging.Logger,
                dry_run: bool = False) -> int:
    """Apply a single regex substitution to a file in-place. Returns
    the number of substitutions made. Uses re.MULTILINE so `^` and `$` match
    line boundaries the same way `sed -i 's/^.../.../'` does.
    """
    if not path.exists():
        log.warning("sed: %s does not exist, skipping", path)
        return 0
    original = path.read_text(encoding="utf-8", errors="surrogateescape")
    patched, count = re.subn(pattern, replacement, original, flags=re.MULTILINE)
    if count == 0:
        log.debug("sed: no matches for %r in %s", pattern, path.name)
        return 0
    log.info("sed: %d substitution(s) in %s", count, path.name)
    if not dry_run:
        path.write_text(patched, encoding="utf-8", errors="surrogateescape")
    return count


def copy_into(src: Path, dst: Path, log: logging.Logger, dry_run: bool = False,
              mode: Optional[int] = None) -> None:
    """Copy `src` to `dst`, creating parents as needed."""
    log.info("Installing %s -> %s", src.name, dst)
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if mode is not None:
        dst.chmod(mode)


# ---------------------------------------------------------------------------
# Boot partition patches
# ---------------------------------------------------------------------------


def patch_boot_partition(mount: Path, opts: PatchOptions, log: logging.Logger,
                          label: str) -> None:
    """Apply all enabled boot-partition patches."""
    log.info("=== Patching boot partition %s ===", label)

    config = mount / "config.txt"
    cmdline = mount / "cmdline.txt"

    if opts.enable_hdmi:
        log.info("[%s] Enabling HDMI", label)
        for pat, repl in [
            (r"^hdmi_ignore_hotplug=1", "#hdmi_ignore_hotplug=1"),
            (r"^hdmi_ignore_edid=0xa5000080", "#hdmi_ignore_edid=0xa5000080"),
            (r"^hdmi_blanking=2", "#hdmi_blanking=2"),
            (r"^ignore_lcd=1", "#ignore_lcd=1"),
            (r"^display_auto_detect=0", "display_auto_detect=1"),
        ]:
            sed_inplace(config, pat, repl, log, opts.dry_run)

    if opts.disable_spi_can:
        log.info("[%s] Commenting out SPI CAN overlays", label)
        sed_inplace(config, r"^(dtoverlay=spi[0-9].*)$", r"#\1", log, opts.dry_run)
        sed_inplace(config, r"^(dtoverlay=sc-mcp2518.*)$", r"#\1", log, opts.dry_run)

    if opts.enable_canfd_hat:
        log.info("[%s] Adding Waveshare CAN FD HAT overlays", label)
        for ovl in ("mcp251xfd", "spi1-3cs"):
            if not (mount / "overlays" / f"{ovl}.dtbo").exists():
                log.warning("[%s] overlays/%s.dtbo missing — CAN FD HAT will "
                            "not probe", label, ovl)
        sed_inplace(config, I2C1_ON_SPI0_RE, r"#\1", log, opts.dry_run)
        if not opts.dry_run:
            text = CANFD_HAT_BLOCK_RE.sub("", config.read_text(encoding="utf-8"))
            if not text.endswith("\n"):
                text += "\n"
            config.write_text(text + RES_CANFD_HAT.read_text(encoding="utf-8"))

    if opts.update_cmdline and cmdline.exists():
        content = cmdline.read_text(encoding="utf-8").rstrip("\n")
        changed = False
        if "panic=" not in content:
            content += " panic=0"
            changed = True
        if "cfg80211" not in content:
            content += " cfg80211.ieee80211_regdom=US"
            changed = True
        if changed:
            log.info("[%s] Updating cmdline.txt", label)
            if not opts.dry_run:
                cmdline.write_text(content + "\n")


# ---------------------------------------------------------------------------
# Rootfs partition patches
# ---------------------------------------------------------------------------


def patch_rootfs_partition(mount: Path, opts: PatchOptions, log: logging.Logger,
                           label: str) -> None:
    """Apply all enabled rootfs patches."""
    log.info("=== Patching rootfs %s ===", label)

    if opts.install_flash_pico:
        if not opts.flash_pico_path.exists():
            log.warning("[%s] flash-pico.sh not found at %s, skipping",
                        label, opts.flash_pico_path)
        else:
            dst_script = mount / "usr/local/bin/flash-pico.sh"
            copy_into(opts.flash_pico_path, dst_script, log, opts.dry_run, mode=0o755)
            dst_override = (
                mount / "etc/systemd/system/limelight_picoflasherprocess.service.d/override.conf"
            )
            copy_into(RES_PICOFLASHER, dst_override, log, opts.dry_run)

    if opts.install_can_udev:
        copy_into(RES_UDEV_CAN, mount / "etc/udev/rules.d/90-usb-can-rename.rules",
                  log, opts.dry_run)
        # Same filename as the stock rule so it replaces it (spi1.0 -> can_s3
        # would otherwise grab the CAN FD HAT's CAN_1).
        copy_into(RES_STOCK_CAN_NAMES,
                  mount / "etc/udev/rules.d/70-can-interface-names.rules",
                  log, opts.dry_run)

    if opts.install_canbusprocess:
        copy_into(
            RES_CANBUSPROCESS,
            mount / "etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_canbuswatchdog:
        copy_into(
            RES_CANBUSWATCHDOG,
            mount / "etc/systemd/system/limelight_canbuswatchdog.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_robot_override:
        copy_into(
            RES_ROBOT,
            mount / "etc/systemd/system/robot.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_mrccan:
        tmpfile = mount / "etc/tmpfiles.d/mrccan.conf"
        if list(mount.glob(HEARTBEAT_MODULE_GLOB)):
            log.info("[%s] robot_heartbeat.ko present — not installing mrccan tmpfile",
                     label)
            if tmpfile.exists() and not opts.dry_run:
                tmpfile.unlink()
        else:
            copy_into(RES_MRCCAN, tmpfile, log, opts.dry_run)

    if opts.install_regdb:
        if not opts.regdb_deb_path.exists():
            log.warning("[%s] wireless-regdb .deb not found at %s, skipping",
                        label, opts.regdb_deb_path)
        else:
            _install_regdb(mount, opts.regdb_deb_path, log, opts.dry_run, label)

    if opts.patch_dashboard_wlan or opts.patch_dashboard_faults:
        _patch_dashboard(mount, opts, log, label)


def _install_regdb(mount: Path, deb: Path, log: logging.Logger,
                   dry_run: bool, label: str) -> None:
    log.info("[%s] Installing wireless-regdb from %s", label, deb.name)
    if dry_run:
        return
    with tempfile.TemporaryDirectory(prefix="regdb-") as tmpdir:
        run(["dpkg-deb", "-x", str(deb), tmpdir], log)
        fw_src = Path(tmpdir) / "lib" / "firmware"
        fw_dst = mount / "usr/lib/firmware"
        fw_dst.mkdir(parents=True, exist_ok=True)
        for f in fw_src.iterdir():
            shutil.copy2(f, fw_dst / f.name)


def _patch_dashboard(mount: Path, opts: PatchOptions, log: logging.Logger,
                     label: str) -> None:
    """Apply sed-style patches to the minified React dashboard JS."""
    js_files = list((mount / "var/www/html/static/js").glob("main.*.js"))
    if not js_files:
        log.warning("[%s] Dashboard main.*.js not found, skipping dashboard patches",
                    label)
        return
    js = js_files[0]
    log.info("[%s] Patching dashboard at %s", label, js.name)
    if opts.dry_run:
        return
    text = js.read_text(encoding="utf-8")
    js.write_text(dashboard.patch(text, log, wlan=opts.patch_dashboard_wlan,
                                  faults=opts.patch_dashboard_faults),
                  encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(layout: ImageLayout, log: logging.Logger) -> list[str]:
    """Mount each rootfs and verify expected post-patch files exist.

    Returns a list of human-readable problems. Empty list = clean validation.
    """
    log.info("=== Validating patched image ===")
    problems: list[str] = []
    tracker = MountTracker()
    try:
        for label, part in [("boot_a", layout.boot_a), ("boot_b", layout.boot_b)]:
            if part is None:
                continue
            with mount_partition(layout.image, part, log, tracker) as mnt:
                config = mnt / "config.txt"
                if not config.exists() or CANFD_HAT_BEGIN not in config.read_text(
                        encoding="utf-8", errors="replace"):
                    problems.append(f"[{label}] config.txt missing CAN FD HAT block")
        for label, part in [("rootfs_a", layout.root_a), ("rootfs_b", layout.root_b)]:
            if part is None:
                continue
            with mount_partition(layout.image, part, log, tracker) as mnt:
                expected = [
                    "etc/udev/rules.d/90-usb-can-rename.rules",
                    "etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
                    "etc/systemd/system/robot.service.d/override.conf",
                    "usr/local/bin/flash-pico.sh",
                ]
                if not list(mnt.glob(HEARTBEAT_MODULE_GLOB)):
                    expected.append("etc/tmpfiles.d/mrccan.conf")
                for rel in expected:
                    p = mnt / rel
                    if not p.exists():
                        problems.append(f"[{label}] missing: {rel}")
                    else:
                        log.debug("[%s] ok: %s", label, rel)
    finally:
        tracker.cleanup(log)
    if not problems:
        log.info("Validation passed: all expected files present in rootfs A/B")
    else:
        for p in problems:
            log.error(p)
    return problems


def inspect(layout: ImageLayout, log: logging.Logger) -> dict[str, Path]:
    """Mount every known partition and return their mount points. Caller is
    responsible for unmounting (the tracker is returned via the dict's
    `_tracker` key).
    """
    log.info("=== Inspecting image (mounts left open) ===")
    tracker = MountTracker()
    mounts: dict[str, Path] = {}
    for label, part in [
        ("boot_a", layout.boot_a),
        ("boot_b", layout.boot_b),
        ("root_a", layout.root_a),
        ("root_b", layout.root_b),
    ]:
        if part is None:
            continue
        ctx = mount_partition(layout.image, part, log, tracker)
        mnt = ctx.__enter__()  # we won't __exit__ — caller cleans up
        mounts[label] = mnt
        log.info("  %s -> %s", label, mnt)
    mounts["_tracker"] = tracker  # type: ignore[assignment]
    return mounts


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def preflight(opts: PatchOptions, log: logging.Logger) -> None:
    """Verify environment + inputs before doing any work."""
    if sys.platform == "win32":
        raise PreflightError(
            "This patcher requires Linux (mount, sfdisk, dpkg-deb). "
            "On Windows, run `python patch-image-win.py` instead."
        )
    if os.geteuid() != 0:
        raise PreflightError("Must run as root (sudo) — mount and dpkg-deb need root.")

    for cmd in ("sfdisk", "mount", "umount", "dpkg-deb", "file"):
        if not shutil.which(cmd):
            raise PreflightError(f"Required command not found: {cmd}")

    if not opts.input_image.exists():
        raise PreflightError(f"Input image not found: {opts.input_image}")
    if opts.input_image.stat().st_size < 100 * 1024 * 1024:
        raise PreflightError(
            f"Input image suspiciously small ({opts.input_image.stat().st_size} bytes)"
        )

    if not opts.output_image.parent.exists():
        raise PreflightError(f"Output directory does not exist: {opts.output_image.parent}")

    log.info("Preflight checks passed")


def patch_image(opts: PatchOptions, log: logging.Logger,
                progress: Optional[Callable[[str, int, int], None]] = None) -> Path:
    """Apply all enabled patches. Returns the path to the patched image.

    `progress` is called with (phase_name, current_step, total_steps) so the
    GUI can drive a progress bar.
    """
    preflight(opts, log)

    # If the user picked a .zip output target, force it to .img — we can't
    # patch partitions inside a zip and won't repack at the end.
    if opts.output_image.suffix.lower() == ".zip":
        new_out = opts.output_image.with_suffix(".img")
        log.warning("Output is a .zip; switching to %s (cannot repack a zip)", new_out)
        opts.output_image = new_out

    # Stage 1: copy input -> working file, then -> output at the end. If a
    # patch fails partway through, the user's input image is never touched.
    working = opts.output_image.with_suffix(opts.output_image.suffix + ".partial")

    if opts.backup and opts.input_image == opts.output_image:
        bak = opts.input_image.with_suffix(opts.input_image.suffix + ".bak")
        log.info("Creating backup: %s", bak)
        if not opts.dry_run:
            shutil.copy2(opts.input_image, bak)

    input_is_zip = opts.input_image.suffix.lower() == ".zip"
    if input_is_zip:
        log.info("Extracting %s -> %s", opts.input_image, working)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            with zipfile.ZipFile(opts.input_image) as zf:
                imgs = [n for n in zf.namelist() if n.lower().endswith(".img")]
                if not imgs:
                    raise RuntimeError(
                        f"No .img file inside {opts.input_image}: {zf.namelist()}"
                    )
                if len(imgs) > 1:
                    log.warning("Multiple .img members found, using first: %s", imgs)
                member = imgs[0]
                log.info("Extracting member %s", member)
                with zf.open(member) as src, open(working, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        log.info("Copying %s -> %s", opts.input_image, working)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            shutil.copy2(opts.input_image, working)

    layout = detect_layout(working if not opts.dry_run else opts.input_image, log)

    # Stage 2: patch each partition.
    tracker = MountTracker()
    try:
        steps: list[tuple[str, Partition, Callable[[Path, PatchOptions, logging.Logger, str], None]]] = []
        if layout.boot_a:
            steps.append(("boot_a", layout.boot_a, patch_boot_partition))
        if layout.boot_b and not opts.skip_b_partitions:
            steps.append(("boot_b", layout.boot_b, patch_boot_partition))
        if layout.root_a:
            steps.append(("rootfs_a", layout.root_a, patch_rootfs_partition))
        if layout.root_b and not opts.skip_b_partitions:
            steps.append(("rootfs_b", layout.root_b, patch_rootfs_partition))

        for i, (label, part, fn) in enumerate(steps, start=1):
            if progress:
                progress(label, i, len(steps))
            if opts.dry_run:
                log.info("[dry-run] Would patch partition %s (offset=%d)",
                         label, part.start_bytes)
                # In dry-run we still walk through to log the sed/copy ops,
                # but we mount the *input* image as read-only just so the
                # patch functions can see the real config.txt.
                # For simplicity in dry-run we don't actually mount anything.
                continue
            with mount_partition(working, part, log, tracker) as mnt:
                fn(mnt, opts, log, label)

    except Exception:
        if opts.cleanup_on_error:
            tracker.cleanup(log)
        else:
            log.error(
                "Patch failed — leaving %d mount(s) open for inspection: %s",
                len(tracker.mounts),
                [str(m) for m in tracker.mounts],
            )
        # Clean up partial output unless the user wanted to keep it for debugging.
        if working.exists() and opts.cleanup_on_error and not opts.keep_mounted:
            log.info("Removing partial output %s", working)
            working.unlink()
        raise

    if opts.keep_mounted:
        log.warning(
            "Patch complete, but mounts left open (--keep-mounted). "
            "Run `umount %s` for each before flashing.",
            " ".join(str(m) for m in tracker.mounts),
        )
    else:
        tracker.cleanup(log)

    if opts.dry_run:
        log.info("[dry-run] Would rename %s -> %s", working, opts.output_image)
        return opts.output_image

    log.info("Renaming %s -> %s", working, opts.output_image)
    if opts.output_image.exists():
        opts.output_image.unlink()
    working.rename(opts.output_image)

    if opts.validate_after:
        layout = detect_layout(opts.output_image, log)
        validate(layout, log)

    log.info("Done. Patched image: %s", opts.output_image)
    return opts.output_image
