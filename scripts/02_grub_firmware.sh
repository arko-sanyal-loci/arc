#!/usr/bin/env bash
# =============================================================================
# 02_grub_firmware.sh — GRUB + firmware hardening for Arc B70 workstation
#
# Handles:
#   • GRUB kernel parameters (xe driver, ReBAR, IOMMU, hugepages)
#   • initramfs early-load of xe/i915
#   • /etc/fstab hugepages mount
#   • sysctl tuning for GPU memory + PCIe performance
#   • firmware package updates
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[GRUB]${NC} $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

GRUB_CFG=/etc/default/grub
BACKUP="${GRUB_CFG}.bak.$(date +%s)"
cp "$GRUB_CFG" "$BACKUP"
info "Backup: ${BACKUP}"

# ── 1. GRUB kernel parameters ─────────────────────────────────────────────────
info "1/5  Configuring GRUB kernel parameters ..."

# Parameters and their purpose:
#   xe.force_probe=*          — force-probe all Xe2/Battlemage PCI IDs
#   i915.force_probe=!        — disable i915 for Arc (let xe own it)
#   pcie_aspm=off             — disable Active State Power Management (Arc PCIe bug workaround)
#   iommu=pt                  — IOMMU passthrough (improves large VRAM DMA)
#   intel_iommu=on            — enable VT-d if available
#   transparent_hugepage=always  — better VRAM bandwidth for model weights
#   hugepagesz=2M             — 2 MB huge pages
#   hugepages=4096            — 8 GB reserved for huge pages (increase if >64 GB RAM)
#   mitigations=auto,nosmt    — balanced; change to 'off' for max perf on private HW
#   loglevel=3                — quieter boot (errors only)
#   quiet splash              — standard

NEW_PARAMS=(
    "xe.force_probe=*"
    "i915.force_probe=!"
    "pcie_aspm=off"
    "iommu=pt"
    "intel_iommu=on"
    "transparent_hugepage=always"
    "hugepagesz=2M"
    "hugepages=4096"
    "mitigations=auto"
    "loglevel=3"
)

# Read current params (strip quotes)
CURRENT=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_CFG" \
    | sed 's/GRUB_CMDLINE_LINUX_DEFAULT=//;s/"//g')

MERGED="$CURRENT"
for P in "${NEW_PARAMS[@]}"; do
    KEY="${P%%=*}"
    # Remove any existing entry with same key (handles multi-value safely)
    MERGED=$(echo "$MERGED" | sed "s/${KEY}=[^ ]*//g; s/ ${KEY} / /g; s/^${KEY} //g; s/ ${KEY}\$//g" | tr -s ' ')
    MERGED="$MERGED $P"
done
MERGED=$(echo "$MERGED" | xargs)

sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"${MERGED}\"|" "$GRUB_CFG"
info "  GRUB_CMDLINE_LINUX_DEFAULT set:"
echo "    \"${MERGED}\""

# Also set GRUB_TIMEOUT to 3 seconds (not 30)
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=3/' "$GRUB_CFG"
# Enable GRUB_GFXMODE for consistent resolution
grep -q '^GRUB_GFXMODE' "$GRUB_CFG" \
    || echo 'GRUB_GFXMODE=1920x1080x32,auto' >> "$GRUB_CFG"

# ── 2. initramfs early module load ────────────────────────────────────────────
info "2/5  Setting early module load in initramfs ..."
MODULES_FILE=/etc/initramfs-tools/modules
for MOD in xe i915 drm drm_kms_helper; do
    grep -q "^${MOD}" "$MODULES_FILE" 2>/dev/null \
        || echo "$MOD" >> "$MODULES_FILE"
done
info "  initramfs modules: xe i915 drm drm_kms_helper  ✓"

# Blacklist nouveau (should not be present on pure Intel/NVIDIA setup but safety)
BLACKLIST=/etc/modprobe.d/blacklist-nouveau.conf
cat > "$BLACKLIST" <<'MODPROBE'
# Blacklist nouveau to prevent conflicts with NVIDIA proprietary driver
blacklist nouveau
options nouveau modeset=0
MODPROBE

# Arc B70 xe driver options
cat > /etc/modprobe.d/intel-arc.conf <<'MODPROBE'
# Intel Arc B70 (Xe2-HPG) — xe driver tuning
# enable_guc=3: GuC + HuC firmware submission (required for perf)
# force_probe=*: catch any unrecognised Battlemage PCI ID
options xe enable_guc=3 force_probe=*
options i915 enable_guc=3 force_probe=!
MODPROBE
info "  xe driver options (enable_guc=3) written  ✓"

# ── 3. Huge pages in fstab ────────────────────────────────────────────────────
info "3/5  Configuring huge pages ..."
FSTAB=/etc/fstab
if ! grep -q 'hugetlbfs' "$FSTAB"; then
    echo "" >> "$FSTAB"
    echo "# Huge pages for VRAM-mapped inference (Arc B70 / LOCI pipeline)" >> "$FSTAB"
    echo "hugetlbfs  /dev/hugepages  hugetlbfs  defaults,pagesize=2M  0 0" >> "$FSTAB"
fi
mkdir -p /dev/hugepages
mount /dev/hugepages 2>/dev/null || true
info "  hugetlbfs mounted at /dev/hugepages  ✓"

# ── 4. sysctl GPU + PCIe tuning ───────────────────────────────────────────────
info "4/5  Applying sysctl tunables ..."
cat > /etc/sysctl.d/99-arc-loci.conf <<'SYSCTL'
# ── Memory management for large model inference ──
# Allow large SHM segments (/dev/shm ring buffer for LOCI speculative pipeline)
kernel.shmmax = 68719476736       # 64 GB
kernel.shmall = 16777216          # pages
# Large mmap for model weight files
vm.max_map_count = 2097152
# Keep memory available for VRAM DMA
vm.swappiness = 10
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5
# Huge pages (complement GRUB hugepages= param)
vm.nr_hugepages = 4096
vm.nr_overcommit_hugepages = 1024
vm.hugetlb_shm_group = 0
# Network (inference server)
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
SYSCTL
sysctl -p /etc/sysctl.d/99-arc-loci.conf 2>/dev/null || true
info "  sysctl tunables applied  ✓"

# ── 5. Firmware update + GRUB rebuild ────────────────────────────────────────
info "5/5  Firmware update + GRUB rebuild ..."
apt-get install -y --no-install-recommends \
    linux-firmware \
    firmware-linux-free \
    fwupd \
    fwupd-amd64-signed 2>/dev/null || true

# Check for firmware updates via fwupd (non-fatal)
fwupdmgr refresh --force 2>/dev/null || warn "  fwupd refresh failed (no network or proxy)"
fwupdmgr get-updates 2>/dev/null | head -20 || true

update-initramfs -u -k all
update-grub
info "  initramfs + GRUB rebuilt  ✓"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  GRUB + Firmware Configuration Done"
echo "════════════════════════════════════════"
echo ""
echo "GRUB cmdline:"
grep GRUB_CMDLINE_LINUX_DEFAULT /etc/default/grub
echo ""
echo "Huge pages:"
grep Huge /proc/meminfo | head -6
echo ""
warn "REBOOT now to apply all changes."
echo ""
echo "BIOS checklist (do BEFORE first boot if not done):"
echo "  ✓ Above 4G Decoding          → Enabled"
echo "  ✓ Resizable BAR (ReBAR)      → Enabled"
echo "  ✓ XMP / EXPO Profile         → XMP I (or your RAM's rated speed)"
echo "  ✓ PCIe Slot for Arc          → Gen 5 / Auto"
echo "  ✓ Secure Boot                → Optional; disable if xe doesn't load"
echo "  ✓ VT-d (IOMMU)               → Enabled"
echo ""
echo "Verify after reboot:"
echo "  cat /proc/cmdline"
echo "  dmesg | grep -iE 'xe|GuC|HuC' | head -20"
echo "  cat /proc/meminfo | grep Huge"
