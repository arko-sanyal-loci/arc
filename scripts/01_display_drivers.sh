#!/usr/bin/env bash
# =============================================================================
# 01_display_drivers.sh — Display driver installation for Intel Arc B70
#                          (Xe2-HPG Battlemage) on Ubuntu 26.04 LTS
#
# This covers BOTH:
#   • Display output  (Wayland/Xorg modesetting via xe kernel driver)
#   • Compute/media   (OpenCL, Level Zero, VA-API, Vulkan)
#
# Run order: this script FIRST, then 02_grub_firmware.sh, then REBOOT
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GRN}[DRV]${NC} $*"; }
step()  { echo -e "${BLU}[===]${NC} $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
command -v lsb_release &>/dev/null || apt-get install -y lsb-release -qq

CODENAME=$(lsb_release -cs)
UBUNTU_VER=$(lsb_release -rs)
info "Ubuntu ${UBUNTU_VER} (${CODENAME})  |  kernel $(uname -r)"
echo ""

# ── STEP 1: Kernel driver check ───────────────────────────────────────────────
step "STEP 1/9  Kernel xe/i915 driver"
# Ubuntu 26.04 ships kernel 6.14 which includes xe (Battlemage/Xe2 support)
if lsmod | grep -q '^xe '; then
    info "  xe driver active  ✓"
elif modprobe xe 2>/dev/null; then
    info "  xe driver loaded  ✓"
else
    warn "  xe not available — trying i915 ..."
    modprobe i915 2>/dev/null \
        && warn "  i915 loaded (xe preferred for B70)" \
        || warn "  WARN: Neither xe nor i915 loaded"
fi

# ── STEP 2: Essential display packages ───────────────────────────────────────
step "STEP 2/9  Core display packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
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
    libdrm-dev
info "  Mesa + modesetting display stack installed  ✓"

# ── STEP 3: VA-API (hardware video encode/decode) ────────────────────────────
step "STEP 3/9  VA-API hardware video stack"
apt-get install -y --no-install-recommends \
    intel-media-va-driver-non-free \
    libva2 \
    libva-drm2 \
    libva-x11-2 \
    libva-glx2 \
    libva-wayland2 \
    vainfo \
    ffmpeg \
    gstreamer1.0-vaapi \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-good

# Test VA-API
export LIBVA_DRIVER_NAME=iHD
if vainfo &>/dev/null; then
    VA_PROFILES=$(vainfo 2>&1 | grep -c 'VAProfile' || echo 0)
    info "  VA-API iHD driver: ${VA_PROFILES} profiles  ✓"
else
    warn "  VA-API not responding yet — will work after reboot + xe load"
fi

# ── STEP 4: Vulkan (display + compute) ───────────────────────────────────────
step "STEP 4/9  Vulkan"
apt-get install -y --no-install-recommends \
    libvulkan1 \
    mesa-vulkan-drivers \
    vulkan-tools \
    vulkan-validationlayers \
    vulkan-validationlayers-dev \
    spirv-tools

info "  Vulkan stack installed  ✓"

# ── STEP 5: Intel Compute Runtime (OpenCL + Level Zero) ─────────────────────
step "STEP 5/9  Intel Compute Runtime + Level Zero"

INTEL_KEYRING=/usr/share/keyrings/intel-graphics-keyring.gpg
if [[ ! -f "$INTEL_KEYRING" ]]; then
    curl -fsSL https://repositories.intel.com/graphics/intel-graphics.key \
      | gpg --dearmor -o "$INTEL_KEYRING"
    info "  Intel graphics keyring installed  ✓"
fi

# Intel ships for noble (24.04); use compat on 26.04
INTEL_LIST=/etc/apt/sources.list.d/intel-graphics.list
cat > "$INTEL_LIST" <<LIST
# Intel GPU compute runtime — noble compat (use when 26.04 tier not yet published)
deb [arch=amd64 signed-by=${INTEL_KEYRING}] https://repositories.intel.com/graphics/ubuntu noble main
LIST

apt-get update -qq
apt-get install -y --no-install-recommends \
    intel-opencl-icd \
    intel-level-zero-gpu \
    level-zero \
    level-zero-dev \
    libmfx1 \
    libmfxgen1 \
    libvpl2 \
    libvpl-dev \
    || warn "  Some compute packages unavailable — Mesa OpenCL fallback active"

# Always install Mesa OpenCL as fallback
apt-get install -y --no-install-recommends \
    mesa-opencl-icd \
    ocl-icd-libopencl1 \
    ocl-icd-opencl-dev \
    clinfo

OPENCL_DEVS=$(clinfo -l 2>/dev/null | grep -c 'Device' || echo 0)
info "  OpenCL devices visible: ${OPENCL_DEVS}  ✓"

# ── STEP 6: oneAPI runtime (SYCL / IPEX-LM / bigDL) ─────────────────────────
step "STEP 6/9  Intel oneAPI runtime"

ONEAPI_LIST=/etc/apt/sources.list.d/oneapi.list
if [[ ! -f "$ONEAPI_LIST" ]]; then
    curl -fsSL https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
      | gpg --dearmor -o /usr/share/keyrings/oneapi-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] \
https://apt.repos.intel.com/oneapi all main" > "$ONEAPI_LIST"
    apt-get update -qq
fi

apt-get install -y --no-install-recommends \
    intel-oneapi-runtime-dpcpp-cpp \
    intel-oneapi-runtime-mkl \
    intel-oneapi-runtime-tbb \
    || warn "  oneAPI runtime packages not found — install manually from https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"

info "  oneAPI runtime installed  ✓"

# ── STEP 7: Firmware + tools ─────────────────────────────────────────────────
step "STEP 7/9  Firmware + GPU tools"
apt-get install -y --no-install-recommends \
    linux-firmware \
    firmware-linux-free \
    intel-gpu-tools \
    pciutils \
    hwinfo \
    lm-sensors \
    nvtop               # GPU monitor (supports Intel)

# Detect sensors
sensors-detect --auto 2>/dev/null || true
info "  Firmware + tools installed  ✓"

# ── STEP 8: Wayland compositor support ───────────────────────────────────────
step "STEP 8/9  Wayland display server support"
apt-get install -y --no-install-recommends \
    libwayland-client0 \
    libwayland-server0 \
    libwayland-egl1 \
    libwayland-egl-backend-dev \
    wayland-protocols \
    xwayland

# If GNOME is installed, ensure it uses Wayland by default
if command -v gnome-shell &>/dev/null; then
    # Force Wayland for GDM
    sed -i 's/#WaylandEnable=false/WaylandEnable=true/' /etc/gdm3/custom.conf 2>/dev/null || true
    info "  GNOME Wayland enabled  ✓"
fi

# If KDE/SDDM is installed
if command -v plasmashell &>/dev/null; then
    mkdir -p /etc/sddm.conf.d
    cat > /etc/sddm.conf.d/wayland.conf <<'SDDM'
[General]
DisplayServer=wayland
CompositorCommand=kwin_wayland --no-lockscreen
SDDM
    info "  KDE Wayland session configured  ✓"
fi

# ── STEP 9: User groups + udev rules ─────────────────────────────────────────
step "STEP 9/9  User groups + udev"
DEFAULT_USER=${SUDO_USER:-arko-san}
usermod -aG render,video,plugdev "$DEFAULT_USER" 2>/dev/null \
    && info "  User '${DEFAULT_USER}' added to render/video/plugdev  ✓" \
    || warn "  Could not add user — do manually: sudo usermod -aG render,video,plugdev $DEFAULT_USER"

cat > /etc/udev/rules.d/99-intel-arc.rules <<'UDEV'
# Intel Arc B70 — PCIe power management
SUBSYSTEM=="pci", ATTR{vendor}=="0x8086", ATTR{class}=="0x030200", ATTR{power/control}="auto"
SUBSYSTEM=="pci", ATTR{vendor}=="0x8086", ATTR{class}=="0x038000", ATTR{power/control}="auto"
# DRM render node — allow video group access
SUBSYSTEM=="drm", KERNEL=="renderD*", GROUP="render", MODE="0666"
SUBSYSTEM=="drm", KERNEL=="card*",    GROUP="video",  MODE="0660"
UDEV
udevadm control --reload-rules && udevadm trigger
info "  udev rules applied  ✓"

# ── Verification summary ──────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Display Driver Install — Summary"
echo "════════════════════════════════════════"
echo ""
echo "── GPU detected: ──"
lspci -nn | grep -iE "(vga|display|3d)" || echo "  (run after reboot)"
echo ""
echo "── DRM nodes: ──"
ls /dev/dri/ 2>/dev/null || echo "  none yet"
echo ""
echo "── Loaded GPU modules: ──"
lsmod | grep -E "^(xe|i915|drm)" | awk '{print "  "$1}' || echo "  none"
echo ""
echo "── OpenCL devices: ──"
clinfo -l 2>/dev/null | head -10 || echo "  (check after reboot)"
echo ""
echo "════════════════════════════════════════"
warn "REBOOT required for full driver activation."
echo ""
echo "Post-reboot verification commands:"
echo "  dmesg | grep -iE 'xe|arc|battlemage' | head -20"
echo "  lsmod | grep xe"
echo "  LIBVA_DRIVER_NAME=iHD vainfo"
echo "  clinfo -l"
echo "  vulkaninfo --summary 2>/dev/null | grep -A2 'GPU'"
echo "  intel_gpu_top   (live GPU usage monitor)"
echo "  nvtop           (combined GPU monitor)"
