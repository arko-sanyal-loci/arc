# Ubuntu 26.04 LTS — Arc B70 + LOCI Workstation Setup

> **TL;DR**: Run the five scripts in `scripts/0x_*.sh` in order. Reboot once after step 2. Done.

---

## 0. Before You Boot the ISO

**BIOS settings (critical — do these first):**

| Setting | Value | Why |
|---|---|---|
| Above 4G Decoding | **Enabled** | Arc 32 GB VRAM needs >4 GB PCI space |
| Resizable BAR (ReBAR) | **Enabled** | Full VRAM bandwidth to CPU |
| PCIe Slot (Arc) | **Gen 5 / Auto** | PCIe 5.0 × 16 for 128 GB/s |
| XMP / EXPO Profile | **XMP I** | DDR5 at rated speed |
| VT-d (IOMMU) | **Enabled** | Required for `iommu=pt` param |
| Secure Boot | **Optional** | Disable if xe driver fails to load |

---

## 1. Install Ubuntu 26.04

### Option A — Automated (autoinstall)

```bash
# Burn ISO to USB
sudo dd if=ubuntu-26.04.1-desktop-amd64.iso of=/dev/sdX bs=4M status=progress

# Mount USB and place autoinstall config at root
cp scripts/00_ubuntu_autoinstall.yaml /path/to/usb/autoinstall.yaml
```

Then boot from USB. The installer will run unattended.

### Option B — Manual GUI install

Normal Ubuntu installer. During partitioning, recommended layout:
- `/boot/efi` — 1 GB  (FAT32)
- `/boot`      — 2 GB  (ext4)
- `/`          — 80 GB (ext4, on LVM)
- `/home`      — 100 GB (ext4, on LVM)
- `/data`      — rest  (ext4 — models, datasets, cache)

---

## 2. Post-Install Script Order

Run as root on the freshly installed system:

```bash
cd ~/arkos-loci-repos/arc  # or wherever you cloned

# Step 1: Display drivers + GPU compute stack
sudo bash scripts/01_display_drivers.sh

# Step 2: GRUB kernel params + firmware + sysctl
sudo bash scripts/02_grub_firmware.sh

# ── REBOOT HERE ──────────────────────────────
sudo reboot

# Step 3: Workspace dirs + Python venv + services
sudo bash scripts/03_workspace_setup.sh

# Step 4: Git config + SSH key + clone all repos
sudo bash scripts/04_git_setup.sh

# Step 5: Full verification
bash scripts/05_verify_all.sh
```

---

## 3. What Each Script Does

### `01_display_drivers.sh`
Installs the full display + compute stack for Intel Arc B70:

| Component | Package(s) | Purpose |
|---|---|---|
| Display | `xserver-xorg-video-modesetting`, `libgl1-mesa-dri` | Wayland + X11 output |
| Kernel driver | `xe` module (kernel built-in) | Xe2/Battlemage GPU management |
| VA-API | `intel-media-va-driver-non-free`, `libva2` | HW video encode/decode |
| Vulkan | `mesa-vulkan-drivers`, `vulkan-tools` | Graphics + compute via Vulkan |
| OpenCL | `intel-opencl-icd`, `level-zero` | GPGPU compute |
| oneAPI | `intel-oneapi-runtime-dpcpp-cpp` | SYCL / IPEX-LM inference |
| Tools | `intel-gpu-tools`, `nvtop`, `clinfo`, `vainfo` | Monitoring + verification |

### `02_grub_firmware.sh`
Kernel boot parameters added:

| Parameter | Effect |
|---|---|
| `xe.force_probe=*` | Probe all Xe2 PCI IDs with xe driver |
| `i915.force_probe=!` | Disable i915 for Arc (let xe own it) |
| `pcie_aspm=off` | Workaround for Arc PCIe power-state bug |
| `iommu=pt` | IOMMU passthrough (better VRAM DMA) |
| `transparent_hugepage=always` | Faster model weight loading |
| `hugepages=4096` | 8 GB huge page reservation |

Also: xe GuC firmware options, udev PCIe power rules, sysctl memory tuning.

### `03_workspace_setup.sh`
- `/data/models/{arc,rtx,shared}` — model storage
- `/data/cache/huggingface` — HF cache (symlinked from `~/.cache`)
- `/dev/shm/loci_pipeline` — cross-GPU ring buffer
- Python venv at `~/arkos-loci-repos/arc/.venv`
- Systemd services: `ollama` (RTX) + `loci-arc-inference` (Arc port 8000)

### `04_git_setup.sh`
- Global git config (name, email, lfs)
- SSH key generation + GitHub config
- Clones `arc`, `LOCI-MEMORY-ENGINE`, `loci-coordination-bus`
- Global `.gitignore` (excludes `.gguf`, `.safetensors`, `.env`, venvs)

---

## 4. GPU Lane Assignments

```
Arc B70 (32 GB GDDR6, 608 GB/s)   →  LANE 1  "deep lane"
  • Large 70B models (weights stay resident)
  • Speculative verification target
  • Inference port: http://localhost:8000
  • Env: ONEAPI_DEVICE_SELECTOR=level_zero:0

RTX 5060 Ti (16 GB GDDR7, 448 GB/s)  →  LANE 0  "fast lane"
  • Fast 7B–13B inference bursts
  • Speculative draft generation
  • Inference port: http://localhost:11434 (Ollama)
  • Env: CUDA_VISIBLE_DEVICES=0

Cross-GPU ring:  /dev/shm/loci_pipeline  (~7 GB/s sustained)
```

---

## 5. Post-Reboot Verification

```bash
# Quick check
bash scripts/05_verify_all.sh

# Manual spot checks
lsmod | grep xe                          # xe driver loaded
dmesg | grep -iE 'xe|GuC|battlemage'    # firmware status
LIBVA_DRIVER_NAME=iHD vainfo             # VA-API encode profiles
clinfo -l                                # OpenCL devices
intel_gpu_top                            # live GPU activity
nvtop                                    # Arc + RTX side by side
vulkaninfo --summary 2>/dev/null | grep GPU
```

---

## 6. Known Issues

| Issue | Fix |
|---|---|
| `xe` not loading | Secure Boot off; check `dmesg \| grep firmware` for missing `.bin` |
| VA-API says no driver | Reboot; `LIBVA_DRIVER_NAME=iHD vainfo` after reboot |
| Arc not in clinfo | Confirm Intel compute runtime installed: `dpkg -l intel-opencl-icd` |
| Black screen on boot | Add `nomodeset` to GRUB temporarily; re-run `02_grub_firmware.sh` |
| Ollama using CPU | CUDA_VISIBLE_DEVICES not set; check `journalctl -u ollama` |

---

## 7. Environment Quick Reference

```bash
# Activate LOCI Python venv
source ~/.bashrc && loci-activate

# Source Arc compute env (before inference)
arc-env

# Monitor GPUs
gpu-top          # intel_gpu_top (Arc)
nvtop            # both GPUs

# Service control
systemctl {start|stop|status} ollama
systemctl {start|stop|status} loci-arc-inference
```
