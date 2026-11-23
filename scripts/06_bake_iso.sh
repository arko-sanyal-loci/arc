#!/usr/bin/env bash
# =============================================================================
# 06_bake_iso.sh — Bake autoinstall.yaml into Ubuntu 26.04 ISO
#
# Creates a custom ISO that installs FULLY UNATTENDED:
#   • No keypresses required after BIOS selects USB boot
#   • autoinstall.yaml + SSH keys embedded into the ISO
#   • GRUB timeout = 0 (immediate boot into installer)
#
# Usage:
#   sudo bash 06_bake_iso.sh \
#     --iso  /path/to/ubuntu-26.04.1-desktop-amd64.iso \
#     --out  /path/to/ubuntu-26.04-loci-autoinstall.iso \
#     --key  /path/to/your_id_ed25519.pub      # optional — pre-seeds SSH
#
# Then flash to USB:
#   sudo dd if=ubuntu-26.04-loci-autoinstall.iso of=/dev/sdX bs=4M status=progress
#   sync
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info() { echo -e "${GRN}[ISO]${NC} $*"; }
step() { echo -e "${BLU}[===]${NC} $*"; }
warn() { echo -e "${YLW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0 [args]"

# ── Parse args ────────────────────────────────────────────────────────────────
ISO_IN=""
ISO_OUT=""
SSH_PUB_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --iso) ISO_IN="$2"; shift 2 ;;
        --out) ISO_OUT="$2"; shift 2 ;;
        --key) SSH_PUB_KEY="$2"; shift 2 ;;
        *) die "Unknown arg: $1" ;;
    esac
done

# Defaults
ISO_IN="${ISO_IN:-/home/arko-san/arkos-loci-repos/Downloads/ubuntu-26.04.1-desktop-amd64.iso}"
ISO_OUT="${ISO_OUT:-/home/arko-san/arkos-loci-repos/Downloads/ubuntu-26.04-loci-autoinstall.iso}"

[[ -f "$ISO_IN" ]]  || die "ISO not found: ${ISO_IN}"
info "Source ISO : ${ISO_IN}"
info "Output ISO : ${ISO_OUT}"

# ── Dependencies ──────────────────────────────────────────────────────────────
step "1/6  Installing dependencies"
apt-get install -y --no-install-recommends \
    xorriso \
    squashfs-tools \
    isolinux \
    syslinux-utils \
    grub-efi-amd64-bin \
    mtools \
    dosfstools 2>/dev/null
info "  xorriso + tools installed  ✓"

# ── Working dirs ──────────────────────────────────────────────────────────────
step "2/6  Extracting ISO"
WORK=$(mktemp -d /tmp/loci-iso.XXXXXX)
MNT="${WORK}/mnt"
CUSTOM="${WORK}/custom"
mkdir -p "$MNT" "$CUSTOM"
trap "umount ${MNT} 2>/dev/null; rm -rf ${WORK}" EXIT

mount -o loop,ro "$ISO_IN" "$MNT"
rsync -a --exclude=/casper/filesystem.squashfs "$MNT/" "$CUSTOM/"
# Copy the squashfs without modification (too large to repack — we inject via nocloud)
cp "$MNT/casper/filesystem.squashfs" "$CUSTOM/casper/filesystem.squashfs"
umount "$MNT"
info "  ISO extracted to ${CUSTOM}  ✓"

# ── Inject autoinstall.yaml (nocloud datasource) ──────────────────────────────
step "3/6  Injecting autoinstall config"
NOCLOUD_DIR="${CUSTOM}/nocloud"
mkdir -p "$NOCLOUD_DIR"

# Read SSH key if provided
SSH_KEY_LINE=""
if [[ -n "$SSH_PUB_KEY" && -f "$SSH_PUB_KEY" ]]; then
    KEY_CONTENT=$(cat "$SSH_PUB_KEY")
    SSH_KEY_LINE="    authorized-keys: [\"${KEY_CONTENT}\"]"
    info "  SSH public key will be pre-seeded in install  ✓"
elif [[ -f /home/arko-san/.ssh/id_ed25519.pub ]]; then
    KEY_CONTENT=$(cat /home/arko-san/.ssh/id_ed25519.pub)
    SSH_KEY_LINE="    authorized-keys: [\"${KEY_CONTENT}\"]"
    info "  Found /home/arko-san/.ssh/id_ed25519.pub — pre-seeding  ✓"
else
    SSH_KEY_LINE="    authorized-keys: []  # add key then run 07_ssh_persistence.sh"
    warn "  No SSH public key found — add manually after install"
fi

# Generate password hash placeholder (user should replace)
# Default password: "loci2026" — change immediately after first login
PW_HASH='$6$rounds=4096$locisalt$h5e7e3Yjk9q5J3w9Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e1Q5e'
# Use python3 to generate a real hash if available
if command -v python3 &>/dev/null; then
    PW_HASH=$(python3 -c "import crypt; print(crypt.crypt('loci2026', crypt.mksalt(crypt.METHOD_SHA512)))")
fi

cat > "${NOCLOUD_DIR}/user-data" <<YAML
#cloud-config
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard: {layout: us, variant: ""}

  network:
    version: 2
    ethernets:
      enp0s3:   {dhcp4: true}
      eno1:     {dhcp4: true}
      enp6s0:   {dhcp4: true}

  storage:
    layout:
      name: lvm
      sizing-policy: all

  identity:
    hostname: loci-workstation
    username: arko-san
    password: "${PW_HASH}"
    realname: "Arko Sanyal"

  ssh:
    install-server: true
${SSH_KEY_LINE}
    allow-pw: true   # set to false after adding SSH key

  packages:
    - git
    - curl
    - wget
    - build-essential
    - python3-pip
    - python3-venv
    - python3-dev
    - htop
    - tmux
    - vim
    - pciutils
    - hwinfo
    - linux-firmware
    - intel-gpu-tools
    - mesa-utils
    - vulkan-tools
    - vainfo
    - net-tools
    - avahi-daemon
    - openssh-server
    - fail2ban
    - ufw
    - rsync
    - jq

  late-commands:
    # /data layout
    - curtin in-target -- mkdir -p /data/models/arc /data/models/rtx /data/datasets /data/checkpoints /data/cache/huggingface /data/logs
    - curtin in-target -- chown -R arko-san:arko-san /data
    # Workspace dirs
    - curtin in-target -- su -c 'mkdir -p ~/arkos-loci-repos' arko-san
    # SSH hardening (applied now; 07_ssh_persistence.sh adds more after reboot)
    - curtin in-target -- bash -c "sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config"
    - curtin in-target -- bash -c "sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config"
    - curtin in-target -- systemctl enable ssh
    # UFW: allow SSH from local network
    - curtin in-target -- ufw allow ssh
    - curtin in-target -- ufw allow from 192.168.0.0/16 to any port 22
    - curtin in-target -- ufw --force enable
    # mDNS (find machine at loci-workstation.local on LAN)
    - curtin in-target -- systemctl enable avahi-daemon
    # Disable sleep
    - curtin in-target -- systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    # Clone arc repo
    - curtin in-target -- su -c 'git clone https://github.com/arko-sanyal-loci/arc.git ~/arkos-loci-repos/arc' arko-san || true

  updates: security
  shutdown: reboot
YAML

# cloud-init meta-data (required by nocloud)
echo "instance-id: loci-workstation-$(date +%s)" > "${NOCLOUD_DIR}/meta-data"
info "  autoinstall user-data written  ✓"

# ── Patch GRUB to auto-boot with autoinstall + ds=nocloud ─────────────────────
step "4/6  Patching GRUB (zero-keypress autoinstall)"

# GRUB EFI config
GRUB_CFG="${CUSTOM}/boot/grub/grub.cfg"
if [[ -f "$GRUB_CFG" ]]; then
    # Backup
    cp "$GRUB_CFG" "${GRUB_CFG}.bak"

    # Set timeout=0, default=0
    sed -i 's/set timeout=.*/set timeout=0/' "$GRUB_CFG"
    sed -i 's/set default=.*/set default=0/' "$GRUB_CFG"

    # Add autoinstall ds=nocloud to first menuentry's linux line
    # Ubuntu autoinstall: kernel needs 'autoinstall ds=nocloud;s=/cdrom/nocloud/'
    sed -i '/^\s*linux\s.*casper\/vmlinuz/s/$/ autoinstall ds=nocloud;s=\/cdrom\/nocloud\//' "$GRUB_CFG"

    info "  grub.cfg patched (timeout=0, autoinstall args)  ✓"
else
    warn "  grub.cfg not found at expected path — checking alternative locations"
    find "$CUSTOM" -name "grub.cfg" | head -5
fi

# Also patch isolinux if present (BIOS/legacy boot)
ISOLINUX_CFG="${CUSTOM}/isolinux/isolinux.cfg"
if [[ -f "$ISOLINUX_CFG" ]]; then
    sed -i 's/^TIMEOUT .*/TIMEOUT 0/' "$ISOLINUX_CFG"
    sed -i '/append.*initrd/s/$/ autoinstall ds=nocloud;s=\/cdrom\/nocloud\//' "$ISOLINUX_CFG"
    info "  isolinux.cfg patched (BIOS boot)  ✓"
fi

# ── Fix checksums ─────────────────────────────────────────────────────────────
step "5/6  Updating ISO checksums"
pushd "$CUSTOM" > /dev/null
# Ubuntu checks md5sum.txt
find . -type f -not -name "md5sum.txt" \
    | xargs md5sum 2>/dev/null > md5sum.txt
popd > /dev/null
info "  md5sum.txt rebuilt  ✓"

# ── Repack ISO ────────────────────────────────────────────────────────────────
step "6/6  Repacking ISO with xorriso"

# Get volume label from original ISO
VOL_LABEL=$(isoinfo -d -i "$ISO_IN" 2>/dev/null | grep 'Volume id' | awk -F': ' '{print $2}' || echo "Ubuntu 26.04 LTS LOCI")

xorriso -as mkisofs \
    -r \
    -V "${VOL_LABEL} LOCI" \
    -J \
    -joliet-long \
    -l \
    -iso-level 3 \
    --grub2-mbr "${CUSTOM}/boot/grub/i386-pc/boot_hybrid.img" \
    -partition_offset 16 \
    --mbr-force-bootable \
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b "${CUSTOM}/boot/grub/efi.img" \
    -appended_part_as_gpt \
    -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7 \
    -c '/boot.catalog' \
    -b '/boot/grub/i386-pc/eltorito.img' \
    -no-emul-boot \
    -boot-load-size 4 \
    -boot-info-table \
    --grub2-boot-info \
    -eltorito-alt-boot \
    -e '--interval:appended_partition_2:::' \
    -no-emul-boot \
    -o "$ISO_OUT" \
    "$CUSTOM" 2>&1 | tail -5 || {

    warn "  xorriso with hybrid MBR failed — trying simpler repack ..."
    xorriso -as mkisofs \
        -r -V "${VOL_LABEL} LOCI" -J -l \
        -b isolinux/isolinux.bin \
        -c isolinux/boot.cat \
        -no-emul-boot -boot-load-size 4 -boot-info-table \
        -eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot \
        -o "$ISO_OUT" "$CUSTOM" 2>&1 | tail -5
}

ISO_SIZE=$(du -sh "$ISO_OUT" 2>/dev/null | cut -f1)
info "  Custom ISO created: ${ISO_OUT}  (${ISO_SIZE})  ✓"

echo ""
echo "════════════════════════════════════════════"
echo "  ISO Ready for Flashing"
echo "════════════════════════════════════════════"
echo ""
echo "Flash to USB (replace /dev/sdX with your USB device):"
echo "  lsblk   ← find your USB device first"
echo "  sudo dd if=${ISO_OUT} of=/dev/sdX bs=4M status=progress"
echo "  sync"
echo ""
echo "Boot sequence:"
echo "  1. Set USB as first boot device in BIOS"
echo "  2. Power on — installer starts immediately (no keypresses)"
echo "  3. Installation takes ~10–15 min"
echo "  4. Machine reboots into Ubuntu 26.04"
echo "  5. SSH in: ssh arko-san@loci-workstation.local"
echo "     (password: loci2026 — change immediately)"
echo ""
echo "After first SSH login:"
echo "  cd ~/arkos-loci-repos/arc"
echo "  sudo bash scripts/01_display_drivers.sh"
echo "  sudo bash scripts/02_grub_firmware.sh && sudo reboot"
echo "  sudo bash scripts/03_workspace_setup.sh"
echo "  sudo bash scripts/07_ssh_persistence.sh"
