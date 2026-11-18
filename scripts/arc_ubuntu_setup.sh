#!/usr/bin/env bash
# =============================================================================
# arc_ubuntu_setup.sh — Intel Arc B70 (Xe2/Battlemage) full driver + runtime
# Target: Ubuntu 26.04 LTS (kernel 6.14+)
# Run as root (or sudo) AFTER first boot.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[ARC]${NC} $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

UBUNTU_VER=$(lsb_release -rs 2>/dev/null || echo "unknown")
KERNEL_VER=$(uname -r)
info "Ubuntu ${UBUNTU_VER}  |  kernel ${KERNEL_VER}"

# ── 1. Kernel / xe driver check ──────────────────────────────────────────────
info "1/8  Verifying xe (Xe2 Battlemage) kernel driver ..."
if lsmod | grep -q '^xe '; then
    info "    xe driver already loaded."
elif modinfo xe &>/dev/null; then
    modprobe xe && info "    xe driver loaded."
else
    warn "    xe module not found — trying i915 fallback ..."
    modprobe i915 2>/dev/null || warn "    Neither xe nor i915 loaded; install linux-oem-26.04 or linux-generic-hwe-26.04"
fi

# ── 2. Base packages ──────────────────────────────────────────────────────────
info "2/8  Installing base packages ..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    pciutils \
    vainfo \
    clinfo \
    vulkan-tools \
    mesa-vulkan-drivers \
    libvulkan1 \
    libvulkan-dev \
    vulkan-validationlayers \
    libdrm-dev \
    libdrm-intel1 \
    intel-gpu-tools \
    hwinfo \
    linux-firmware \
    firmware-linux-free

# ── 3. Intel Media / VA-API stack ─────────────────────────────────────────────
info "3/8  Installing Intel media driver (non-free iHD) ..."
apt-get install -y --no-install-recommends \
    intel-media-va-driver-non-free \
    libva2 \
    libva-drm2 \
    libva-x11-2 \
    libva-glx2 \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-vaapi \
    ffmpeg

# ── 4. Intel Compute Runtime (OpenCL + Level Zero) ───────────────────────────
info "4/8  Setting up Intel Compute Runtime repo ..."
# Intel's graphics PPA / deb repo
INTEL_KEYRING=/usr/share/keyrings/intel-graphics-keyring.gpg
if [[ ! -f "$INTEL_KEYRING" ]]; then
    curl -fsSL https://repositories.intel.com/graphics/intel-graphics.key \
      | gpg --dearmor -o "$INTEL_KEYRING"
fi

INTEL_LIST=/etc/apt/sources.list.d/intel-graphics.list
cat > "$INTEL_LIST" <<LIST
deb [arch=amd64 signed-by=${INTEL_KEYRING}] https://repositories.intel.com/graphics/ubuntu noble main
LIST
# noble = 24.04; Intel hasn't published 26.04 repo yet — use noble compat
# Change to "oracular" or "plucky" when Intel publishes for 25.x/26.x

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
    || warn "Some Intel compute packages not found — repo may not cover 26.04 yet; using Mesa OpenCL fallback"

# Mesa OpenCL fallback (always install as safety net)
apt-get install -y --no-install-recommends \
    mesa-opencl-icd \
    ocl-icd-libopencl1 \
    ocl-icd-opencl-dev

# ── 5. SYCL / oneAPI (optional — needed for IPEX-LM / bigDL) ─────────────────
info "5/8  Installing oneAPI base toolkit (SYCL for Arc compute) ..."
ONEAPI_LIST=/etc/apt/sources.list.d/oneapi.list
if [[ ! -f "$ONEAPI_LIST" ]]; then
    curl -fsSL https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
      | gpg --dearmor -o /usr/share/keyrings/oneapi-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] \
https://apt.repos.intel.com/oneapi all main" > "$ONEAPI_LIST"
    apt-get update -qq
fi
# Install only the runtime components (saves ~20 GB vs full SDK)
apt-get install -y --no-install-recommends \
    intel-oneapi-runtime-dpcpp-cpp \
    intel-oneapi-runtime-mkl \
    || warn "oneAPI runtime packages unavailable; skip SYCL for now"

# ── 6. User groups ────────────────────────────────────────────────────────────
info "6/8  Adding default user to render + video + plugdev groups ..."
DEFAULT_USER=${SUDO_USER:-$(logname 2>/dev/null || echo "")}
if [[ -n "$DEFAULT_USER" ]]; then
    usermod -aG render,video,plugdev "$DEFAULT_USER" \
        && info "    Added ${DEFAULT_USER} to render/video/plugdev."
else
    warn "    Could not determine default user — add yourself manually: sudo usermod -aG render,video,plugdev \$USER"
fi

# ── 7. udev rules (ReBAR + PCIe perf) ────────────────────────────────────────
info "7/8  Writing udev rules for Arc B70 ..."
cat > /etc/udev/rules.d/99-intel-arc.rules <<'UDEV'
# Intel Arc B70 (Battlemage Xe2-HPG) — PCIe power control
SUBSYSTEM=="pci", ATTR{vendor}=="0x8086", ATTR{class}=="0x030200", \
    ATTR{power/control}="auto"
# Allow render node access for video group
SUBSYSTEM=="drm", KERNEL=="renderD*", GROUP="render", MODE="0666"
UDEV
udevadm control --reload-rules && udevadm trigger

# ── 8. Verify ─────────────────────────────────────────────────────────────────
info "8/8  Verification ..."
echo ""
echo "──── lspci: Intel GPU ────"
lspci -nn | grep -iE "(vga|display|3d|intel.*arc)" || echo "(no match)"

echo ""
echo "──── DRM render nodes ────"
ls -la /dev/dri/ 2>/dev/null || echo "(no DRM nodes)"

echo ""
echo "──── xe / i915 driver ────"
lsmod | grep -E "^(xe|i915)" || echo "(neither xe nor i915 in lsmod)"

echo ""
echo "──── VA-API info ────"
LIBVA_DRIVER_NAME=iHD vainfo 2>&1 | head -20 || echo "(vainfo failed)"

echo ""
echo "──── OpenCL devices ────"
clinfo -l 2>&1 | head -20 || echo "(clinfo failed)"

echo ""
echo "──── Vulkan devices ────"
vulkaninfo --summary 2>&1 | head -30 || echo "(vulkaninfo failed)"

echo ""
info "Setup complete."
warn "REBOOT required for group membership + new udev rules to take effect."
echo ""
echo "Post-reboot checklist:"
echo "  1. Verify xe driver:     lsmod | grep xe"
echo "  2. VA-API encode test:   LIBVA_DRIVER_NAME=iHD vainfo"
echo "  3. OpenCL test:          clinfo -l"
echo "  4. Vulkan test:          vulkaninfo --summary"
echo "  5. BIOS: Enable ReBAR (Above 4G Decoding + Resizable BAR) + XMP I"
echo "  6. For LOCI inference server on Arc:  source /opt/intel/oneapi/setvars.sh"
echo "     then start: ONEAPI_DEVICE_SELECTOR=level_zero:0 python -m loci_server"
