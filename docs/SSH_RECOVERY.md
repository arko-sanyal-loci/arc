# SSH & Recovery Guide — LOCI Workstation

## Overview

The LOCI workstation has **two layers of SSH access**:

| Layer | When Available | Port | User | Purpose |
|-------|---------------|------|------|---------|
| **OpenSSH** | After OS boots | 22 | arko-san | Normal remote work |
| **Dropbear** | During initramfs (pre-OS) | 2222 | root | Recovery, emergency shell |

This means you can SSH into the machine **even when the OS fails to boot** — as long as the kernel and initramfs load, Dropbear is running.

---

## Normal SSH Access

### By hostname (mDNS — works on local network)
```bash
ssh arko-san@loci-workstation.local
```

### By IP
```bash
# Find the IP from another machine:
ping -c1 loci-workstation.local
# or:
avahi-resolve -n loci-workstation.local

ssh arko-san@192.168.x.x
```

### SSH config shortcut (add to your laptop's `~/.ssh/config`)
```
Host loci
    HostName loci-workstation.local
    User arko-san
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
    ServerAliveCountMax 3

Host loci-recovery
    HostName loci-workstation.local
    User root
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
```

Then just: `ssh loci` or `ssh loci-recovery`

---

## Pre-Boot / Recovery SSH (Dropbear)

Dropbear is baked into the initramfs. It starts **before** the root filesystem mounts, **before** systemd runs. This gives you:

- Emergency shell when OS won't boot
- Remote LUKS disk unlock (if disk encryption added later)
- Fix broken configs without a monitor/keyboard

### Connecting to Dropbear

```bash
# The machine must be powered on and past GRUB (kernel loading)
ssh root@loci-workstation.local -p 2222

# If mDNS isn't resolving, use the static IP:
ssh root@192.168.x.x -p 2222

# Host key will be different from the OS's key — add to known_hosts separately:
ssh -o StrictHostKeyChecking=accept-new root@loci-workstation.local -p 2222
```

### What you can do in Dropbear shell

```bash
# Check what's wrong
dmesg | tail -50
cat /proc/cmdline

# Mount the root filesystem manually
mkdir -p /mnt/root
mount /dev/mapper/ubuntu--vg-ubuntu--lv /mnt/root   # LVM
# or:
mount /dev/nvme0n1p3 /mnt/root                        # direct partition

# Chroot to fix things
chroot /mnt/root /bin/bash
  # Now you're inside the installed OS
  systemctl status <whatever>
  nano /etc/fstab
  nano /etc/ssh/sshd_config
  update-grub
  update-initramfs -u
  exit

# Continue booting (exit Dropbear shell to let initramfs proceed)
exit
```

### Timing window

Dropbear is available from the moment the kernel boots until the root filesystem is mounted and systemd takes over. If the machine boots normally, this window is ~5–15 seconds. To extend it for maintenance:

Add `break=premount` to the GRUB cmdline temporarily — this pauses initramfs before mounting root, giving unlimited time.

---

## Adding Your Laptop's SSH Key

After install, your laptop's key needs to be in `authorized_keys` on the workstation:

**From your laptop:**
```bash
# Copy your laptop's public key to the workstation
ssh-copy-id -i ~/.ssh/id_ed25519.pub arko-san@loci-workstation.local

# Verify it works:
ssh arko-san@loci-workstation.local 'echo "Key auth works"'
```

**Then disable password auth on the workstation:**
```bash
sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

**Update Dropbear authorized_keys too** (for pre-boot recovery):
```bash
sudo bash -c "cat /home/arko-san/.ssh/authorized_keys > /etc/dropbear/initramfs/authorized_keys"
sudo update-initramfs -u -k all
```

---

## fail2ban — Brute Force Protection

fail2ban is running and watches port 22 and 2222.

```bash
# Check ban status
sudo fail2ban-client status sshd

# Unban an IP (if you locked yourself out)
sudo fail2ban-client set sshd unbanip <your.ip.address>

# See recent bans
sudo journalctl -u fail2ban --since "1 hour ago"
```

Thresholds: 4 failed attempts → 2-hour ban. LAN (`192.168.0.0/16`) is ignored.

---

## Network Discovery

```bash
# Find the workstation from another machine on LAN
avahi-browse -t -r _ssh._tcp

# Or just:
ping loci-workstation.local
nmap -p 22,2222 loci-workstation.local
```

---

## Firewall Rules Summary

```
Port 22   TCP   anywhere        → OpenSSH (fail2ban guarded)
Port 2222 TCP   192.168.0.0/16  → Dropbear recovery (LAN only)
Port 5353 UDP   anywhere        → mDNS (avahi)
Port 8000 TCP   192.168.0.0/16  → Arc inference server
Port 11434 TCP  192.168.0.0/16  → Ollama
```

Check with: `sudo ufw status verbose`

---

## Recovery Scenarios

### "OS won't boot, stuck at black screen"
1. Machine is still reachable via Dropbear (port 2222) if kernel loaded
2. `ssh root@loci-workstation.local -p 2222`
3. Check `dmesg`, mount root, chroot, fix

### "Forgot the user password"
1. SSH in normally (key auth still works even without password)
2. `sudo passwd arko-san`

### "SSH key lost / locked out completely"
1. Physical access: boot from USB with Live Ubuntu
2. Mount the root partition
3. Add new key to `/home/arko-san/.ssh/authorized_keys`

### "Dropbear not starting at boot"
```bash
# Check it's in the initramfs:
lsinitramfs /boot/initrd.img-$(uname -r) | grep dropbear

# Rebuild if missing:
sudo update-initramfs -u -k all

# Verify GRUB has ip= param:
cat /proc/cmdline | grep ip
```

### "loci-workstation.local not resolving"
```bash
# Check avahi is running:
systemctl status avahi-daemon

# Manual resolution:
avahi-resolve -n loci-workstation.local

# Check nsswitch.conf has mdns4_minimal:
grep ^hosts /etc/nsswitch.conf
# Should show: hosts: files mdns4_minimal [NOTFOUND=return] dns
```

---

## Full Setup Order (Reference)

```bash
# After autoinstall ISO boots and completes:
ssh arko-san@loci-workstation.local   # password: loci2026

# 1. SSH persistence (do this first so you can work remotely for the rest)
sudo bash scripts/07_ssh_persistence.sh
# Add your laptop key, then reboot once to verify Dropbear works

# 2. Display drivers
sudo bash scripts/01_display_drivers.sh

# 3. GRUB + firmware (requires reboot)
sudo bash scripts/02_grub_firmware.sh
sudo reboot

# 4. Workspace
sudo bash scripts/03_workspace_setup.sh

# 5. Git
bash scripts/04_git_setup.sh

# 6. Verify everything
bash scripts/05_verify_all.sh
```
