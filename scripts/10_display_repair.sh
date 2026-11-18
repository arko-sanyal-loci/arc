#!/usr/bin/env bash
# =============================================================================
# 10_display_repair.sh — Emergency display driver teardown + full reinstall
#
# USE WHEN: Display is glitched, blank, frozen, torn, or wrong resolution
#           after a kernel update, power event, or driver corruption.
#
# Covers both GPUs:
#   • Intel Arc B70 (Xe2-HPG Battlemage) — xe kernel module
#   • NVIDIA RTX 5060 Ti (Blackwell GB206) — nvidia-open kernel module
#
# Run from TTY (Ctrl+Alt+F3) or SSH if display is unusable:
#   sudo bash scripts/10_display_repair.sh
#
# After it finishes: REBOOT. The display should come back cleanly.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLU='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${GRN}[FIX]${NC}  $*"; }
step()  { echo -e "${BLU}[===]${NC}  $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

TARGET_USER="${SUDO_USER:-arko-san}"
KERNEL=$(uname -r)

echo ""
echo -e "${CYN}════════════════════════════════════════════════════════${NC}"
echo -e "${CYN}  LOCI Display Repair — full teardown + reinstall       ${NC}"
echo -e "${CYN}  Kernel: ${KERNEL}   User: ${TARGET_USER}              ${NC}"
echo -e "${CYN}════════════════════════════════════════════════════════${NC}"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 0 — Stop the display server so drivers can be unloaded cleanly
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 0 — Stop display managers"

for DM in gdm3 sddm lightdm xdm; do
    if systemctl is-active --quiet "$DM" 2>/dev/null; then
        systemctl stop "$DM" && info "  Stopped $DM"
    fi
done

# Kill any stray Xorg / Wayland compositors
pkill -15 Xorg   2>/dev/null || true
pkill -15 Xwayland 2>/dev/null || true
pkill -15 gnome-shell 2>/dev/null || true
pkill -15 plasmashell 2>/dev/null || true
sleep 2

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — Unload GPU kernel modules (in correct reverse-dependency order)
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 1 — Unload GPU modules"

unload_module() {
    local mod=$1
    if lsmod | grep -q "^${mod} "; then
        if rmmod "$mod" 2>/dev/null; then
            info "  Unloaded: $mod"
        else
            warn "  Could not unload $mod (in use — will reinstall anyway)"
        fi
    fi
}

# Intel stack (reverse dependency order)
for m in xe_display xe guc_huc_fw i915 i915_dma drm_xe intel_vsec intel_pmt \
          intel_th_gth intel_th_msu intel_uncore drm_display_helper \
          drm_buddy drm_ttm_helper ttm drm_suballoc_helper drm; do
    unload_module "$m"
done

# NVIDIA stack
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    unload_module "$m"
done

info "  Module unload phase complete"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — Purge stale/conflicting display packages
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 2 — Purge stale display packages"

apt-get update -qq

# Purge any manually-installed (non-distro) Mesa that may conflict
apt-get purge -y --auto-remove \
    mesa-* libmesa-* 2>/dev/null \
    || warn "  Some mesa packages not found — skipping purge"

# Purge old NVIDIA closed-source driver if present (we use nvidia-open)
apt-get purge -y --auto-remove \
    nvidia-driver-* \
    libcuda1 \
    libcudart* \
    2>/dev/null || true

# Remove Intel GPU compute packages that will be reinstalled fresh
apt-get purge -y --auto-remove \
    intel-opencl-icd \
    intel-level-zero-gpu \
    level-zero \
    libmfx1 libmfxgen1 libvpl2 \
    intel-media-va-driver-non-free \
    2>/dev/null || true

apt-get autoremove -y -qq
apt-get clean -qq
info "  Stale packages removed"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 3 — Reinstall Intel Arc display + compute stack
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 3 — Reinstall Intel Arc B70 display stack"

# Intel GPU keyring
INTEL_KEYRING=/usr/share/keyrings/intel-graphics-keyring.gpg
if [[ ! -f "$INTEL_KEYRING" ]]; then
    curl -fsSL https://repositories.intel.com/graphics/intel-graphics.key \
      | gpg --dearmor -o "$INTEL_KEYRING"
fi
cat > /etc/apt/sources.list.d/intel-graphics.list <<LIST
deb [arch=amd64 signed-by=${INTEL_KEYRING}] https://repositories.intel.com/graphics/ubuntu noble main
LIST

apt-get update -qq

# Core Mesa (display, EGL, Vulkan, VA-API)
apt-get install -y \
    xserver-xorg-video-modesetting \
    libgl1-mesa-dri \
    libglx-mesa0 \
    libgles2-mesa \
    libgbm1 \
    libegl-mesa0 \
    libegl1 \
    libglvnd-dev \
    mesa-utils \
    mesa-utils-extra \
    mesa-va-drivers \
    mesa-vdpau-drivers \
    mesa-vulkan-drivers \
    libvulkan1 \
    libvulkan-dev \
    vulkan-tools \
    vulkan-validationlayers \
    libdrm2 \
    libdrm-intel1 \
    libdrm-amdgpu1 \
    libdrm-dev \
    spirv-tools

info "  Mesa display stack reinstalled"

# VA-API (hardware video)
apt-get install -y \
    intel-media-va-driver-non-free \
    libva2 libva-drm2 libva-x11-2 libva-glx2 libva-wayland2 \
    vainfo

info "  VA-API stack reinstalled"

# Intel Compute Runtime (OpenCL + Level Zero)
apt-get install -y \
    intel-opencl-icd \
    intel-level-zero-gpu \
    level-zero \
    level-zero-dev \
    libmfx1 \
    libmfxgen1 \
    libvpl2 \
    libvpl-dev \
    mesa-opencl-icd \
    ocl-icd-libopencl1 \
    clinfo \
    || warn "  Some Intel compute packages unavailable"

info "  Intel Compute Runtime reinstalled"

# Firmware
apt-get install -y \
    linux-firmware \
    firmware-linux-free \
    intel-gpu-tools \
    nvtop \
    lm-sensors

info "  Firmware + GPU tools reinstalled"

# Wayland stack
apt-get install -y \
    libwayland-client0 libwayland-server0 libwayland-egl1 \
    libwayland-egl-backend-dev wayland-protocols xwayland

info "  Wayland stack reinstalled"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 4 — Reinstall NVIDIA RTX 5060 Ti (Blackwell / open kernel module)
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 4 — Reinstall NVIDIA RTX 5060 Ti (open kernel module)"

# Determine best available NVIDIA open driver version
NVIDIA_PKG=""
for ver in 570 565 560 555 550; do
    if apt-cache show "nvidia-driver-${ver}-open" &>/dev/null 2>&1; then
        NVIDIA_PKG="nvidia-driver-${ver}-open"
        break
    fi
done

if [[ -n "$NVIDIA_PKG" ]]; then
    apt-get install -y \
        "$NVIDIA_PKG" \
        nvidia-cuda-toolkit \
        cuda-toolkit \
        libnvidia-compute-"${ver/nvidia-driver-/}" \
        libnvidia-gl-"${ver/nvidia-driver-/}" \
        2>/dev/null || apt-get install -y "$NVIDIA_PKG" || warn "NVIDIA driver install failed"
    info "  Installed $NVIDIA_PKG"
else
    warn "  No nvidia-driver-*-open found in apt — adding CUDA keyring"
    # Add NVIDIA CUDA repo for Ubuntu 24.04 compat
    CUDA_PIN=/etc/apt/preferences.d/cuda-repository-pin-600
    curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
        -o /tmp/cuda-keyring.deb && dpkg -i /tmp/cuda-keyring.deb
    apt-get update -qq
    apt-get install -y cuda-toolkit-12-9 2>/dev/null || warn "  CUDA install failed — check NVIDIA apt source"
fi

# NVENC/NVDEC firmware check
if modprobe nvidia 2>/dev/null; then
    info "  nvidia module loaded OK"
fi

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 5 — Rebuild initramfs with xe + nvidia modules
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 5 — Rebuild initramfs"

# Ensure xe + nvidia are in initramfs
MODULES_FILE=/etc/initramfs-tools/modules
for mod in xe nvidia nvidia_drm nvidia_modeset; do
    grep -qxF "$mod" "$MODULES_FILE" || echo "$mod" >> "$MODULES_FILE"
done

# Force xe to load before display manager
cat > /etc/modprobe.d/loci-display.conf <<'MODPROBE'
# LOCI display priority: xe (Arc B70) loads first
# Options for stability on Xe2-HPG Battlemage
options xe enable_guc=3 force_probe=*
options nvidia NVreg_OpenRmEnableUnsupportedGpus=1
# Blacklist old i915 for primary display (xe takes over)
# Comment out if you have an older Intel iGPU that needs i915
# blacklist i915
MODPROBE

update-initramfs -u -k all
info "  initramfs rebuilt with xe + nvidia"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 6 — GRUB parameters for clean display init
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 6 — GRUB display parameters"

GRUB_CFG=/etc/default/grub
cp -f "$GRUB_CFG" "${GRUB_CFG}.bak.repair.$(date +%Y%m%d%H%M%S)"

# Current cmdline
CURRENT=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_CFG" | cut -d'"' -f2)

# Params to add for display stability
DISPLAY_PARAMS="i915.enable_psr=0 intel_iommu=on iommu=pt drm.debug=0 \
xe.enable_guc=3 nomodeset_start splash"
# Remove nomodeset if accidentally set (it disables KMS and breaks Arc)
CURRENT=$(echo "$CURRENT" | sed 's/\bnomodeset\b//g')

# Build new cmdline (deduplicated)
NEW_PARAMS="$CURRENT"
for p in $DISPLAY_PARAMS; do
    KEY="${p%%=*}"
    # Remove old value if present
    NEW_PARAMS=$(echo "$NEW_PARAMS" | sed "s/\b${KEY}=[^ ]*//g")
    NEW_PARAMS="$NEW_PARAMS $p"
done
NEW_PARAMS=$(echo "$NEW_PARAMS" | tr -s ' ' | sed 's/^ //;s/ $//')

sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"${NEW_PARAMS}\"|" "$GRUB_CFG"
update-grub
info "  GRUB updated — PSR disabled (fixes many Arc glitch reports)"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 7 — Display manager config (force clean Wayland session)
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 7 — Display manager reset"

# GNOME / GDM3
if [[ -f /etc/gdm3/custom.conf ]]; then
    # Ensure Wayland is on and no forced Xorg fallback
    sed -i 's/^#*WaylandEnable=.*/WaylandEnable=true/'  /etc/gdm3/custom.conf
    sed -i '/^#*DefaultSession=/d'                       /etc/gdm3/custom.conf
    info "  GDM3: Wayland enabled"
fi

# KDE / SDDM
if command -v plasmashell &>/dev/null; then
    mkdir -p /etc/sddm.conf.d
    cat > /etc/sddm.conf.d/wayland.conf <<'SDDM'
[General]
DisplayServer=wayland
CompositorCommand=kwin_wayland --no-lockscreen
SDDM
    info "  SDDM: Wayland session set"
fi

# X11 fallback Xorg config (ensures modesetting driver used if Wayland fails)
mkdir -p /etc/X11/xorg.conf.d
cat > /etc/X11/xorg.conf.d/10-modesetting.conf <<'XORG'
Section "Device"
    Identifier  "Intel Arc B70"
    Driver      "modesetting"
    Option      "AccelMethod" "glamor"
    Option      "DRI"         "3"
EndSection

Section "Screen"
    Identifier  "Default Screen"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
    EndSubSection
EndSection
XORG
info "  Xorg modesetting fallback config written"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 8 — User groups + udev rules
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 8 — User groups + udev"

usermod -aG render,video,plugdev "$TARGET_USER" 2>/dev/null || true

cat > /etc/udev/rules.d/99-loci-display.rules <<'UDEV'
# Arc B70 PCIe power + render node access
SUBSYSTEM=="pci",  ATTR{vendor}=="0x8086", ATTR{class}=="0x030200", ATTR{power/control}="auto"
SUBSYSTEM=="pci",  ATTR{vendor}=="0x8086", ATTR{class}=="0x038000", ATTR{power/control}="auto"
SUBSYSTEM=="drm",  KERNEL=="renderD*",     GROUP="render",  MODE="0666"
SUBSYSTEM=="drm",  KERNEL=="card*",        GROUP="video",   MODE="0660"
# RTX 5060 Ti
SUBSYSTEM=="pci",  ATTR{vendor}=="0x10de",                 ATTR{power/control}="auto"
UDEV

udevadm control --reload-rules
udevadm trigger
info "  udev rules updated"

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 9 — Restart display manager
# ═══════════════════════════════════════════════════════════════════════════
step "PHASE 9 — Restart display manager"

for DM in gdm3 sddm lightdm; do
    if systemctl list-unit-files "${DM}.service" &>/dev/null 2>&1; then
        systemctl start "$DM" && info "  Started $DM" && break
    fi
done

# ═══════════════════════════════════════════════════════════════════════════
# DONE — Diagnostic summary
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GRN}════════════════════════════════════════════════════════${NC}"
echo -e "${GRN}  Display Repair Complete — REBOOT TO APPLY             ${NC}"
echo -e "${GRN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "── GPU hardware detected: ──"
lspci -nn | grep -iE "(vga|display|3d|nvidia|intel)" || echo "  (check with lspci)"
echo ""
echo "── DRM nodes now: ──"
ls -la /dev/dri/ 2>/dev/null || echo "  none active yet"
echo ""
echo "── xe module status: ──"
lsmod | grep -E "^xe" && echo "  ✓ xe loaded" || echo "  xe not loaded yet (loads on reboot)"
echo ""
echo "── NVIDIA module status: ──"
lsmod | grep -E "^nvidia" && echo "  ✓ nvidia loaded" || echo "  nvidia not loaded yet"
echo ""
echo "── Post-reboot verification: ──"
echo "  dmesg | grep -iE '(xe|arc|battlemage|nvidia|drm)' | head -30"
echo "  lsmod | grep -E '(xe|nvidia)'"
echo "  LIBVA_DRIVER_NAME=iHD vainfo"
echo "  clinfo -l"
echo "  vulkaninfo --summary 2>/dev/null | grep -A2 'GPU'"
echo "  nvidia-smi"
echo "  intel_gpu_top"
echo "  nvtop"
echo ""
echo -e "${YLW}  → sudo reboot${NC}"
echo ""
