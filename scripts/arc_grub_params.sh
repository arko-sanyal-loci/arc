#!/usr/bin/env bash
# =============================================================================
# arc_grub_params.sh — GRUB + initramfs tuning for Intel Arc B70 (Xe2)
# Run once after arc_ubuntu_setup.sh, then reboot.
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

GRUB_CFG=/etc/default/grub
BACKUP="${GRUB_CFG}.bak.$(date +%s)"
cp "$GRUB_CFG" "$BACKUP"
echo "Backed up $GRUB_CFG → $BACKUP"

# Kernel parameters needed for Arc B70:
# i915.force_probe=*       — force-probe any unsupported Arc PCI IDs (xe fallback)
# xe.force_probe=*         — same for the new xe driver
# pcie_aspm=off            — disable ASPM to avoid Arc PCIe power-state bugs
# iommu=pt                 — passthrough IOMMU mode (better for GPU memory)
# amd_iommu=pt / intel_iommu=on igfx_off — if dual GPU: disable iGPU
# transparent_hugepage=always — improves VRAM bandwidth for large model weights
# mitigations=off          — optional perf boost on private workstation (YMMV)
NEW_PARAMS="i915.force_probe=* xe.force_probe=* pcie_aspm=off iommu=pt transparent_hugepage=always"

# Read current CMDLINE
CURRENT=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_CFG" | sed 's/GRUB_CMDLINE_LINUX_DEFAULT=//;s/"//g')

# Merge (avoid duplicates)
MERGED="$CURRENT"
for P in $NEW_PARAMS; do
    KEY="${P%%=*}"
    # Remove any existing entry with same key
    MERGED=$(echo "$MERGED" | sed "s/${KEY}=[^ ]*//g; s/${KEY}//g" | tr -s ' ')
    MERGED="$MERGED $P"
done
MERGED=$(echo "$MERGED" | xargs)  # trim

sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"${MERGED}\"|" "$GRUB_CFG"
echo "Updated GRUB_CMDLINE_LINUX_DEFAULT → \"${MERGED}\""

# Ensure xe module loads early
MODULES_FILE=/etc/initramfs-tools/modules
grep -q '^xe' "$MODULES_FILE" 2>/dev/null || echo "xe" >> "$MODULES_FILE"
grep -q '^i915' "$MODULES_FILE" 2>/dev/null || echo "i915" >> "$MODULES_FILE"
echo "Updated initramfs modules: xe, i915"

# Rebuild initramfs + GRUB
update-initramfs -u -k all
update-grub

echo ""
echo "Done. REBOOT to apply."
echo "Verify after reboot:"
echo "  cat /proc/cmdline"
echo "  lsmod | grep -E 'xe|i915'"
echo "  dmesg | grep -i 'xe\|arc\|battlemage' | head -20"
