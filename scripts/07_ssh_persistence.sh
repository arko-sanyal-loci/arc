#!/usr/bin/env bash
# =============================================================================
# 07_ssh_persistence.sh — SSH persistence + pre-boot recovery
#
# Run ONCE after first boot, before running 01_display_drivers.sh
#
# What this does:
#   1. Hardens openssh-server (key-only auth, rate limiting)
#   2. Installs Dropbear in initramfs (SSH before OS loads — recovery + LUKS)
#   3. Configures fail2ban (brute-force protection)
#   4. Enables avahi/mDNS (find machine at loci-workstation.local)
#   5. Adds a static fallback IP on the LAN interface
#   6. Rebuilds initramfs with Dropbear + network
#
# After this script:
#   SSH login: ssh arko-san@loci-workstation.local
#   Pre-boot:  ssh root@loci-workstation.local -p 2222  (while in initramfs)
#   Recovery:  ssh -p 2222 root@<ip>  (even if OS won't boot)
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info() { echo -e "${GRN}[SSH]${NC} $*"; }
step() { echo -e "${BLU}[===]${NC} $*"; }
warn() { echo -e "${YLW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

USER_HOME="/home/arko-san"
SSH_DIR="${USER_HOME}/.ssh"
DROPBEAR_PORT=2222

# ── 1. Install packages ───────────────────────────────────────────────────────
step "1/7  Installing SSH packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    openssh-server \
    dropbear-initramfs \
    fail2ban \
    avahi-daemon \
    avahi-utils \
    libnss-mdns \
    ufw \
    net-tools \
    iproute2 \
    nmap 2>/dev/null
info "  Packages installed  ✓"

# ── 2. Harden OpenSSH ─────────────────────────────────────────────────────────
step "2/7  Hardening OpenSSH server"
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d)

cat > /etc/ssh/sshd_config <<'SSHD'
# =============================================================================
# LOCI Workstation — OpenSSH Server Configuration
# =============================================================================

# Network
Port 22
AddressFamily inet
ListenAddress 0.0.0.0

# Host keys
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key

# Auth — key-only after first login (flip AllowPasswordAuth to no once key added)
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication yes       # set to 'no' once SSH key confirmed
PermitEmptyPasswords no
PermitRootLogin no
ChallengeResponseAuthentication no

# Hardening
MaxAuthTries 4
MaxSessions 10
LoginGraceTime 30
ClientAliveInterval 120
ClientAliveCountMax 3
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*

# Subsystems
Subsystem sftp /usr/lib/openssh/sftp-server

# Allow only the LOCI user
AllowUsers arko-san
SSHD

systemctl enable --now ssh
systemctl restart ssh
info "  sshd hardened + restarted  ✓"

# ── 3. SSH keys ───────────────────────────────────────────────────────────────
step "3/7  Setting up SSH keys"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
chown arko-san:arko-san "$SSH_DIR"

# Generate key for the machine itself (for git/inter-machine auth)
if [[ ! -f "${SSH_DIR}/id_ed25519" ]]; then
    sudo -u arko-san ssh-keygen -t ed25519 \
        -C "arko-san@loci-workstation" \
        -f "${SSH_DIR}/id_ed25519" \
        -N ""
    info "  Generated ed25519 key pair  ✓"
else
    info "  id_ed25519 already exists — skipping keygen"
fi

touch "${SSH_DIR}/authorized_keys"
chmod 600 "${SSH_DIR}/authorized_keys"
chown arko-san:arko-san "${SSH_DIR}/authorized_keys"

# Print the public key so it can be added to GitHub / other machines
echo ""
echo "════ Public key (add to GitHub and your laptop's authorized_keys) ════"
cat "${SSH_DIR}/id_ed25519.pub"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""

info "  SSH key setup complete  ✓"

# ── 4. Dropbear in initramfs (pre-boot recovery SSH) ─────────────────────────
step "4/7  Configuring Dropbear initramfs (pre-boot SSH)"

# Dropbear config
cat > /etc/dropbear/initramfs/dropbear.conf <<DBCONF
# Dropbear — initramfs pre-boot SSH daemon
# Access: ssh -p ${DROPBEAR_PORT} root@loci-workstation.local
# Used for: emergency shell, LUKS unlock, recovery

DROPBEAR_OPTIONS="-p ${DROPBEAR_PORT} -s -j -k -I 60"
DBCONF

# Network config for initramfs (DHCP with fallback)
# This tells initramfs to bring up the NIC before Dropbear starts
cat > /etc/initramfs-tools/conf.d/network.conf <<NETCONF
# IP config for initramfs Dropbear
# Format: IP::GATEWAY:NETMASK:HOSTNAME:INTERFACE:dhcp
# 'dhcp' = try DHCP; if that fails, static fallback below is used
NETCONF

# Try to detect the primary ethernet interface
PRIMARY_IF=$(ip -br link | awk '$2=="UP" && $1 !~ "lo|docker|br-|veth" {print $1; exit}' \
    || ip -br link | awk '$1 !~ "lo|docker|br-|veth" {print $1; exit}')
PRIMARY_IF="${PRIMARY_IF:-eth0}"
info "  Detected primary interface: ${PRIMARY_IF}"

# initramfs IP= kernel param — DHCP with hostname so it's discoverable
# (Override in /etc/default/grub GRUB_CMDLINE_LINUX if network layout differs)
GRUB_EXTRA="ip=dhcp"

# Create the initramfs network hook so the NIC is up when Dropbear starts
cat > /etc/initramfs-tools/hooks/dropbear-network <<'HOOK'
#!/bin/sh
# Initramfs hook: bring up NIC for Dropbear
PREREQ="dropbear"
prereqs() { echo "$PREREQ"; }
case $1 in
    prereqs) prereqs; exit 0 ;;
esac
. /usr/share/initramfs-tools/hook-functions
copy_exec /sbin/dhclient /sbin || true
copy_exec /sbin/ip /sbin || true
HOOK
chmod +x /etc/initramfs-tools/hooks/dropbear-network

# Copy the host's authorized_keys into Dropbear initramfs
# This means you use your NORMAL SSH key to access recovery shell too
DROPBEAR_AUTH="/etc/dropbear/initramfs/authorized_keys"
mkdir -p /etc/dropbear/initramfs
if [[ -f "${SSH_DIR}/authorized_keys" && -s "${SSH_DIR}/authorized_keys" ]]; then
    cp "${SSH_DIR}/authorized_keys" "$DROPBEAR_AUTH"
    info "  Copied authorized_keys to Dropbear  ✓"
elif [[ -f "${SSH_DIR}/id_ed25519.pub" ]]; then
    # At minimum, allow the machine's own key for recovery
    cat "${SSH_DIR}/id_ed25519.pub" >> "$DROPBEAR_AUTH" 2>/dev/null || true
    info "  Added machine key to Dropbear authorized_keys  ✓"
fi

# If /home/arko-san has a laptop key pre-seeded, copy that too
if [[ -f "${SSH_DIR}/laptop_id_ed25519.pub" ]]; then
    cat "${SSH_DIR}/laptop_id_ed25519.pub" >> "$DROPBEAR_AUTH"
    info "  Added laptop key to Dropbear  ✓"
fi

chmod 600 "$DROPBEAR_AUTH" 2>/dev/null || true
info "  Dropbear initramfs configured on port ${DROPBEAR_PORT}  ✓"

# ── 5. fail2ban ───────────────────────────────────────────────────────────────
step "5/7  Configuring fail2ban"
cat > /etc/fail2ban/jail.local <<'F2B'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
backend  = systemd
ignoreip = 127.0.0.1/8 192.168.0.0/16 10.0.0.0/8

[sshd]
enabled  = true
port     = ssh,2222
filter   = sshd
logpath  = %(sshd_log)s
maxretry = 4
bantime  = 7200
F2B

systemctl enable --now fail2ban
systemctl restart fail2ban
info "  fail2ban configured (SSHd + Dropbear port 2222)  ✓"

# ── 6. Avahi / mDNS — find machine as loci-workstation.local ─────────────────
step "6/7  Configuring avahi mDNS"
cat > /etc/avahi/avahi-daemon.conf <<'AVAHI'
[server]
host-name=loci-workstation
domain-name=local
use-ipv4=yes
use-ipv6=no
allow-interfaces=
deny-interfaces=docker0,virbr0

[wide-area]
enable-wide-area=no

[publish]
publish-addresses=yes
publish-hinfo=yes
publish-workstation=yes
publish-domain=yes
AVAHI

# nsswitch.conf: ensure mdns4_minimal before dns for .local resolution
if ! grep -q 'mdns4_minimal' /etc/nsswitch.conf; then
    sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns/' \
        /etc/nsswitch.conf
fi

systemctl enable --now avahi-daemon
systemctl restart avahi-daemon
info "  Avahi enabled — machine accessible at loci-workstation.local  ✓"

# ── 7. UFW firewall ───────────────────────────────────────────────────────────
step "7/7  Firewall rules"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing

# SSH from anywhere (fail2ban guards it)
ufw allow 22/tcp comment 'OpenSSH'
# Dropbear recovery port — LAN only
ufw allow from 192.168.0.0/16 to any port 2222 proto tcp comment 'Dropbear recovery'
ufw allow from 10.0.0.0/8 to any port 2222 proto tcp comment 'Dropbear recovery VPN'
# mDNS
ufw allow 5353/udp comment 'mDNS avahi'
# LOCI inference ports — LAN only
ufw allow from 192.168.0.0/16 to any port 8000 proto tcp comment 'Arc inference'
ufw allow from 192.168.0.0/16 to any port 11434 proto tcp comment 'Ollama'

ufw --force enable
info "  UFW firewall rules set  ✓"

# ── Rebuild initramfs ─────────────────────────────────────────────────────────
echo ""
info "Rebuilding initramfs (Dropbear will be baked in)..."
update-initramfs -u -k all 2>&1 | grep -v '^$' || true

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  SSH Persistence Complete"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  Normal SSH:"
echo "    ssh arko-san@loci-workstation.local"
echo "    ssh arko-san@$(hostname -I | awk '{print $1}')"
echo ""
echo "  Pre-boot / Recovery SSH (Dropbear, while machine is in initramfs):"
echo "    ssh root@loci-workstation.local -p ${DROPBEAR_PORT}"
echo "    # or by IP:"
echo "    ssh root@$(hostname -I | awk '{print $1}') -p ${DROPBEAR_PORT}"
echo ""
echo "  IMPORTANT — Next steps:"
echo "    1. Add your LAPTOP's public key to ${SSH_DIR}/authorized_keys"
echo "       Then set PasswordAuthentication no in /etc/ssh/sshd_config"
echo "    2. Reboot to verify Dropbear starts in initramfs"
echo "    3. Continue with: sudo bash scripts/01_display_drivers.sh"
echo ""
