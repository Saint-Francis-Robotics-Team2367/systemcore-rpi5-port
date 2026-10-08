#!/bin/bash
set -euo pipefail

PI5B_VERSION="v1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLASH_PICO="${SCRIPT_DIR}/netboot/flash-pico.sh"
RES="${SCRIPT_DIR}/patcher/resources"
REGDB_DEB="${SCRIPT_DIR}/beta9/wireless-regdb_2025.10.07-0ubuntu1~24.04.1_all.deb"

IMAGE_URL="https://github.com/LimelightVision/systemcore-os-public/releases/download/limelightosr-beta-10-139/limelightsystemcorebetacm5-limelightosr-beta-10.zip"
IMAGE_ZIP="${SCRIPT_DIR}/cache/limelightsystemcorebetacm5-limelightosr-beta-10.zip"
BUILD_IMG="${SCRIPT_DIR}/systemcore-pi5b-beta10.img"
OUTPUT_IMG="${SCRIPT_DIR}/systemcore-pi5b-beta10-${PI5B_VERSION}.img"

# Beta 10 partition layout:
#   p1: boot selector (FAT32, 16M)  — autoboot.txt, config.txt (empty)
#   p2: boot A (FAT32, 64M)         — config.txt, cmdline.txt -> rootfs p5
#   p3: boot B (FAT32, 64M)         — config.txt, cmdline.txt -> rootfs p6
#   p4: extended
#   p5: rootfs A (ext4, 7G)
#   p6: rootfs B (ext4, 7G)
BOOT_A_OFF=$((34816 * 512))
BOOT_B_OFF=$((165888 * 512))
ROOT_A_OFF=$((299008 * 512))
ROOT_B_OFF=$((14981120 * 512))

# --- Step 1: Preflight ---

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo $0)"
    exit 1
fi

for cmd in wget unzip mount umount sed; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Required tool not found: $cmd"
        exit 1
    fi
done

if [ ! -f "$FLASH_PICO" ]; then
    echo "ERROR: netboot/flash-pico.sh not found"
    exit 1
fi

if [ ! -d "$RES" ]; then
    echo "ERROR: patcher/resources not found"
    exit 1
fi

echo "=== SystemCore Pi 5B Image Builder (Beta 10) ==="
echo ""

# --- Step 2: Download upstream image ---
# NOTE: Upstream Beta 10+ ships a 16K-page kernel with matching userspace.
# We no longer replace the kernel — the stock one works on Pi 5B as-is.

mkdir -p "${SCRIPT_DIR}/cache"

if [ ! -f "$IMAGE_ZIP" ]; then
    echo "[1/5] Downloading upstream SystemCore Beta 10 image..."
    wget -c -O "$IMAGE_ZIP" "$IMAGE_URL"
else
    echo "[1/5] Upstream image already cached."
fi

# --- Step 3: Extract image ---

rm -f "$BUILD_IMG" "$OUTPUT_IMG"

echo "[2/5] Extracting image from zip..."
INNER_IMG=$(unzip -l "$IMAGE_ZIP" | grep -oP '\S+\.img$' | head -1)
if [ -z "$INNER_IMG" ]; then
    echo "ERROR: No .img file found inside zip"
    exit 1
fi
echo "  Found: $INNER_IMG"
unzip -p "$IMAGE_ZIP" "$INNER_IMG" > "$BUILD_IMG"
echo "  Extracted to: $BUILD_IMG ($(du -h "$BUILD_IMG" | cut -f1))"

# --- Step 3: Patch boot partitions (A and B) ---

patch_boot() {
    local MNT="$1"
    local LABEL="$2"

    # Enable HDMI output
    sed -i 's/^hdmi_ignore_hotplug=1/#hdmi_ignore_hotplug=1/' "$MNT/config.txt"
    sed -i 's/^hdmi_ignore_edid=0xa5000080/#hdmi_ignore_edid=0xa5000080/' "$MNT/config.txt"
    sed -i 's/^hdmi_blanking=2/#hdmi_blanking=2/' "$MNT/config.txt"
    sed -i 's/^ignore_lcd=1/#ignore_lcd=1/' "$MNT/config.txt"
    sed -i 's/^display_auto_detect=0/display_auto_detect=1/' "$MNT/config.txt"

    # Comment out the CM5 carrier's SPI CAN overlays (sc-mcp2518 + its SPI
    # buses). Drop any previous HAT block first so a re-run doesn't comment
    # out our own dtoverlay=spi1-3cs line.
    sed -i '/^# BEGIN waveshare-canfd-hat/,/^# END waveshare-canfd-hat/d' "$MNT/config.txt"
    sed -i '/^dtoverlay=spi[0-9]/s/^/#/' "$MNT/config.txt"
    sed -i '/^dtoverlay=sc-mcp2518/s/^/#/' "$MNT/config.txt"

    # Waveshare 2-CH CAN FD HAT (2x MCP2518FD). Harmless without the HAT.
    cat "$RES/canfd-hat-config.txt" >> "$MNT/config.txt"
    for ovl in mcp251xfd spi1-3cs; do
        [ -f "$MNT/overlays/$ovl.dtbo" ] || echo "  [$LABEL] WARNING: overlays/$ovl.dtbo missing — CAN FD HAT will not probe"
    done

    # Add panic=0 and wifi regdom to cmdline if not already present
    if ! grep -q "panic=" "$MNT/cmdline.txt"; then
        sed -i 's/$/ panic=0/' "$MNT/cmdline.txt"
    fi
    if ! grep -q "cfg80211" "$MNT/cmdline.txt"; then
        sed -i 's/$/ cfg80211.ieee80211_regdom=US/' "$MNT/cmdline.txt"
    fi

    echo "  [$LABEL] HDMI enabled, carrier SPI CAN disabled, CAN FD HAT overlays added, cmdline updated"
}

echo "[3/5] Patching boot partitions..."

BOOT_A_MNT=$(mktemp -d)
mount -o loop,offset=${BOOT_A_OFF} "$BUILD_IMG" "$BOOT_A_MNT"
patch_boot "$BOOT_A_MNT" "boot_a"
umount "$BOOT_A_MNT"
rmdir "$BOOT_A_MNT"

BOOT_B_MNT=$(mktemp -d)
mount -o loop,offset=${BOOT_B_OFF} "$BUILD_IMG" "$BOOT_B_MNT"
patch_boot "$BOOT_B_MNT" "boot_b"
umount "$BOOT_B_MNT"
rmdir "$BOOT_B_MNT"

# --- Step 4: Patch rootfs A and B ---

patch_rootfs() {
    local MNT="$1"
    local LABEL="$2"

    # Pico flasher
    cp "$FLASH_PICO" "$MNT/usr/local/bin/flash-pico.sh"
    chmod +x "$MNT/usr/local/bin/flash-pico.sh"

    mkdir -p "$MNT/etc/systemd/system/limelight_picoflasherprocess.service.d"
    cp "$RES/picoflasher-override.conf" "$MNT/etc/systemd/system/limelight_picoflasherprocess.service.d/override.conf"
    echo "  [$LABEL] Installed flash-pico.sh + override"

    # CAN support: Waveshare CAN FD HAT (pinned to can_s0/can_s1) + any number
    # of USB-CAN adapters, optional with graceful timeout. See the comments in
    # each resource file for the reasoning.
    cp "$RES/90-usb-can-rename.rules" "$MNT/etc/udev/rules.d/90-usb-can-rename.rules"
    mkdir -p "$MNT/etc/systemd/system/limelight_canbusprocess.service.d" \
             "$MNT/etc/systemd/system/limelight_canbuswatchdog.service.d" \
             "$MNT/etc/systemd/system/robot.service.d"
    cp "$RES/canbusprocess-override.conf" "$MNT/etc/systemd/system/limelight_canbusprocess.service.d/override.conf"
    cp "$RES/canbuswatchdog-override.conf" "$MNT/etc/systemd/system/limelight_canbuswatchdog.service.d/override.conf"
    cp "$RES/robot-override.conf" "$MNT/etc/systemd/system/robot.service.d/override.conf"
    echo "  [$LABEL] Installed CAN FD HAT + multi-adapter USB-CAN support (optional, 30s timeout)"

    # MrcCommDaemon directory (see patcher/resources/mrccan.conf).
    mkdir -p "$MNT/etc/tmpfiles.d"
    cp "$RES/mrccan.conf" "$MNT/etc/tmpfiles.d/mrccan.conf"
    echo "  [$LABEL] Created /dev/mrccan tmpfile (unblocks MrcCommDaemon)"

    # Wireless regulatory database
    if [ -f "$REGDB_DEB" ]; then
        REGDB_TMP=$(mktemp -d)
        dpkg-deb -x "$REGDB_DEB" "$REGDB_TMP"
        mkdir -p "$MNT/usr/lib/firmware"
        cp "$REGDB_TMP/lib/firmware/"* "$MNT/usr/lib/firmware/"
        rm -rf "$REGDB_TMP"
        echo "  [$LABEL] Installed wireless-regdb (regulatory.db)"
    fi

    # Unlock WLAN0 Access Point settings in dashboard
    local DASHBOARD_JS=$(find "$MNT/var/www/html/static/js" -name 'main.*.js' 2>/dev/null | head -1)
    if [ -n "$DASHBOARD_JS" ]; then
        # Unlock wlan0 fields (disabled:o||a -> disabled:o where a="wlan0"===e)
        sed -i 's/disabled:o||a/disabled:o/g' "$DASHBOARD_JS"
        # Remove forced wlan0 overrides on save (let user-entered values persist)
        sed -i 's/,{static_ip:"172\.30\.0\.1",gateway:"172\.30\.0\.1",use_dhcp:!1}/,{}/g' "$DASHBOARD_JS"
        echo "  [$LABEL] Unlocked WLAN0 AP settings in dashboard"

        # Add fault count reset button to header fault tooltip
        sed -i 's/faultCounts:t\.fc||\[0,0,0,0,0,0\]/faultCounts:(window.__rawFC=t.fc||[0,0,0,0,0,0]).map(function(v,j){return Math.max(0,v-((window.__faultBL||[])[j]||0))})/g' "$DASHBOARD_JS"
        sed -i 's/"historical-"\.concat(t))}))\]/"historical-".concat(t))})),\(0,xo.jsx\)("div",{style:{marginTop:"8px",textAlign:"center"},children:\(0,xo.jsx\)("button",{onClick:function(){window.__faultBL=window.__rawFC?window.__rawFC.slice():[]},style:{fontSize:"11px",padding:"2px 8px",cursor:"pointer",background:"#333",color:"#fff",border:"1px solid #666",borderRadius:"3px"},children:"Reset Fault Counts"}\)}\)]/g' "$DASHBOARD_JS"
        echo "  [$LABEL] Added fault count reset button"
    fi
}

echo "[4/5] Patching rootfs A..."
ROOT_A_MNT=$(mktemp -d)
mount -o loop,offset=${ROOT_A_OFF} "$BUILD_IMG" "$ROOT_A_MNT"
patch_rootfs "$ROOT_A_MNT" "rootfs_a"
umount "$ROOT_A_MNT"
rmdir "$ROOT_A_MNT"

echo "[4/5] Patching rootfs B..."
ROOT_B_MNT=$(mktemp -d)
mount -o loop,offset=${ROOT_B_OFF} "$BUILD_IMG" "$ROOT_B_MNT"
patch_rootfs "$ROOT_B_MNT" "rootfs_b"
umount "$ROOT_B_MNT"
rmdir "$ROOT_B_MNT"

# --- Step 5: Done ---

mv "$BUILD_IMG" "$OUTPUT_IMG"

echo "[5/5] Done!"
echo ""
echo "============================================"
echo "  SystemCore Pi 5B image ready! (Beta 10 ${PI5B_VERSION})"
echo "============================================"
echo ""
echo "  Image:   $OUTPUT_IMG"
echo "  Size:    $(du -h "$OUTPUT_IMG" | cut -f1)"
echo ""
echo "  Patches applied:"
echo "    - HDMI output enabled"
echo "    - Carrier-board SPI CAN overlays disabled"
echo "    - Waveshare 2-CH CAN FD HAT overlays (can_s0/can_s1, CAN FD 1M/2M by default)"
echo "    - flash-pico.sh (auto-flashes RP2350 Pico on any USB port)"
echo "    - USB-CAN multi-adapter support (next free can_sN, classic CAN 1Mbps by default; opt into CAN FD per-bus via /etc/can_bus_mode after verifying device support)"
echo "    - vcan placeholders auto-fill missing can_s0-s4 (HAL requires all 5)"
echo "    - CAN is optional (30s timeout, robot starts regardless)"
echo "    - Hot-plug: new adapters auto-named and configured"
echo "    - /dev/mrccan tmpfile (unblocks MrcCommDaemon -> robot.service)"
echo "    - Wireless regulatory database (US WiFi channels)"
echo ""
echo "  Flash to SD card:"
echo "    sudo dd if=$OUTPUT_IMG of=/dev/sdX bs=4M status=progress"
echo ""
echo "  After flashing, just insert SD and power on the Pi 5."
echo "  No further configuration needed."
echo ""
