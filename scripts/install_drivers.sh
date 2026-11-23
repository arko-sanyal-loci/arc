#!/usr/bin/env bash
# =============================================================================
# scripts/install_drivers.sh
# =============================================================================
# GPU driver + compute runtime installer for Arko's workstation:
#
#   • NVIDIA RTX 5060 Ti  (16 GB GDDR7, 759 AI TOPS, Ada Lovelace)
#     → NVIDIA driver 570+, CUDA 12.4+, cuDNN 9
#
#   • Intel Arc B70  (32 GB GDDR6, 367 AI TOPS, Battlemage)
#     → Intel GPU userspace (NEO compute runtime), Level-Zero, oneAPI Base
#
# Tested on: Ubuntu 22.04 / 24.04 LTS (amd64)
# Run as root or with sudo.
#
# Usage:
#   chmod +x scripts/install_drivers.sh
#   sudo bash scripts/install_drivers.sh
#   # Reboot, then verify with: nvidia-smi && clinfo && ze_info
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YEL}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

UBUNTU_VER=$(lsb_release -rs 2>/dev/null || echo "unknown")
info "Ubuntu $UBUNTU_VER  |  $(uname -m)"

# =============================================================================
# 0. Prerequisites
# =============================================================================
info "Installing build prerequisites..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential dkms linux-headers-"$(uname -r)" \
    curl wget gpg ca-certificates software-properties-common \
    pciutils lshw clinfo vainfo intel-gpu-tools \
    python3-pip python3-venv

# =============================================================================
# 1. NVIDIA — RTX 5060 Ti (Ada Lovelace, sm_89+)
# =============================================================================
info "=== NVIDIA RTX 5060 Ti setup ==="

# 1a. Add graphics-drivers PPA for latest stable (570+)
if ! grep -q "graphics-drivers" /etc/apt/sources.list.d/* 2>/dev/null; then
    add-apt-repository -y ppa:graphics-drivers/ppa
fi
apt-get update -qq

# Check what ubuntu-drivers recommends (usually nvidia-driver-570 for 5060 Ti)
NVIDIA_REC=$(ubuntu-drivers devices 2>/dev/null \
    | awk '/recommended/ && /nvidia-driver/ {print $3; exit}')

if [[ -z "$NVIDIA_REC" ]]; then
    warn "ubuntu-drivers found no recommendation; defaulting to nvidia-driver-570"
    NVIDIA_REC="nvidia-driver-570"
fi
info "Installing $NVIDIA_REC ..."
apt-get install -y --no-install-recommends \
    "$NVIDIA_REC" \
    nvidia-settings \
    nvidia-prime

# 1b. CUDA 12.4 toolkit (needed for Ollama GPU offload)
CUDA_PIN="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-ubuntu2404.pin"
CUDA_DEB="https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda-repo-ubuntu2404-12-4-local_12.4.1-550.54.15-1_amd64.deb"

if ! dpkg -l cuda-toolkit-12-4 &>/dev/null; then
    info "Installing CUDA 12.4 toolkit..."
    TMP=$(mktemp -d)
    wget -qO "$TMP/cuda.deb" "$CUDA_DEB"
    dpkg -i "$TMP/cuda.deb"
    cp /var/cuda-repo-ubuntu2404-12-4-local/cuda-*-keyring.gpg /usr/share/keyrings/
    apt-get update -qq
    apt-get install -y cuda-toolkit-12-4
    rm -rf "$TMP"
else
    info "CUDA 12.4 toolkit already installed."
fi

# 1c. cuDNN 9
if ! dpkg -l libcudnn9-cuda-12 &>/dev/null; then
    info "Installing cuDNN 9..."
    apt-get install -y --no-install-recommends \
        libcudnn9-cuda-12 \
        libcudnn9-dev-cuda-12
fi

# Add CUDA to PATH for this session and permanently
export PATH="/usr/local/cuda/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"

CUDA_ENV='/etc/profile.d/cuda.sh'
cat > "$CUDA_ENV" <<'EOF'
export PATH="/usr/local/cuda/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
EOF
chmod +x "$CUDA_ENV"
info "CUDA env written to $CUDA_ENV"

# =============================================================================
# 2. Intel Arc B70 — compute runtime (Intel NEO + Level Zero + oneAPI)
# =============================================================================
info "=== Intel Arc B70 (Battlemage) setup ==="

# 2a. Intel GPU compute runtime (OpenCL + Level Zero)
#     Source: https://github.com/intel/compute-runtime/releases
INTEL_GPU_REPO="https://repositories.intel.com/gpu/ubuntu"
INTEL_KEYRING="/usr/share/keyrings/intel-graphics.gpg"

if [[ ! -f "$INTEL_KEYRING" ]]; then
    info "Adding Intel GPU repository key..."
    wget -qO- https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o "$INTEL_KEYRING"
fi

# Select the right Ubuntu codename
CODENAME=$(lsb_release -cs)
INTEL_LIST="/etc/apt/sources.list.d/intel-gpu.list"
if [[ ! -f "$INTEL_LIST" ]]; then
    echo "deb [signed-by=$INTEL_KEYRING arch=amd64] $INTEL_GPU_REPO $CODENAME client" \
        > "$INTEL_LIST"
fi

apt-get update -qq
apt-get install -y --no-install-recommends \
    intel-opencl-icd \
    intel-level-zero-gpu \
    level-zero \
    level-zero-dev \
    libze-dev \
    libze-intel-gpu1 \
    intel-ocloc \
    clinfo

# 2b. Intel oneAPI Base Toolkit (for SYCL, MKL, and llama.cpp SYCL backend)
#     This is large (~4 GB) but needed for local Arc inference with llama.cpp
ONEAPI_KEY="/usr/share/keyrings/oneapi-archive-keyring.gpg"
ONEAPI_LIST="/etc/apt/sources.list.d/oneAPI.list"

if [[ ! -f "$ONEAPI_KEY" ]]; then
    info "Adding Intel oneAPI repository key..."
    wget -qO- https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
        | gpg --dearmor -o "$ONEAPI_KEY"
fi

if [[ ! -f "$ONEAPI_LIST" ]]; then
    echo "deb [signed-by=$ONEAPI_KEY] https://apt.repos.intel.com/oneapi all main" \
        > "$ONEAPI_LIST"
fi

apt-get update -qq

# Install components needed for llama.cpp SYCL:
#   intel-oneapi-compiler-dpcpp-cpp  — SYCL compiler
#   intel-oneapi-mkl-devel           — MKL (BLAS for Arc)
#   intel-oneapi-dnnl-devel          — oneDNN for neural ops
info "Installing Intel oneAPI base components..."
apt-get install -y --no-install-recommends \
    intel-oneapi-compiler-dpcpp-cpp \
    intel-oneapi-mkl-devel \
    intel-oneapi-dnnl-devel \
    intel-oneapi-tbb-devel

# Source oneAPI environment
ONEAPI_ENV="/opt/intel/oneapi/setvars.sh"
if [[ -f "$ONEAPI_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$ONEAPI_ENV" --force 2>/dev/null || true
    info "oneAPI env sourced."
    # Persist it
    echo "source $ONEAPI_ENV --force 2>/dev/null || true" \
        >> /etc/profile.d/oneapi.sh
    chmod +x /etc/profile.d/oneapi.sh
else
    warn "oneAPI setvars.sh not found — manually source it after install."
fi

# 2c. i915 firmware (critical for Battlemage / Arc B-series)
info "Ensuring i915 firmware is current..."
apt-get install -y --no-install-recommends \
    linux-firmware \
    firmware-linux-nonfree 2>/dev/null || \
apt-get install -y linux-firmware  # Ubuntu usually ships this

# =============================================================================
# 3. Verification helpers (run after reboot)
# =============================================================================
cat > /usr/local/bin/arc-verify-gpus <<'VERSCRIPT'
#!/usr/bin/env bash
# Run after reboot to verify both GPUs are operational.
echo "===== NVIDIA RTX 5060 Ti ====="
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap \
               --format=csv,noheader
else
    echo "nvidia-smi not found — NVIDIA driver not loaded"
fi

echo ""
echo "===== Intel Arc B70 (OpenCL) ====="
if command -v clinfo &>/dev/null; then
    clinfo -l 2>/dev/null | grep -A2 "Intel" || echo "(no Intel OpenCL platform)"
else
    echo "clinfo not found"
fi

echo ""
echo "===== Intel Arc B70 (Level Zero) ====="
if command -v ze_info &>/dev/null; then
    ze_info 2>/dev/null | grep -E "Driver|Device|Memory" || true
else
    echo "ze_info not installed — try: apt install intel-level-zero-gpu-tools"
fi

echo ""
echo "===== Ollama GPU check ====="
if command -v ollama &>/dev/null; then
    ollama list 2>/dev/null || echo "Ollama installed but no models yet — run setup_ollama.sh"
else
    echo "Ollama not installed yet — run scripts/setup_ollama.sh"
fi
VERSCRIPT
chmod +x /usr/local/bin/arc-verify-gpus

# =============================================================================
# 4. XMP1 reminder
# =============================================================================
info ""
info "===== IMPORTANT: DDR5 XMP1 ====="
info "Your 32GB DDR5-5200 sticks default to JEDEC 4800 MT/s until XMP is enabled."
info "Current speed check: sudo dmidecode -t memory | grep Speed"
info "To enable 5200 MT/s:"
info "  BIOS → AI Tweaker → Ai Overclock Tuner → XMP I"
info "  Save & Exit, verify on next boot with the command above."
info ""

# =============================================================================
# Done
# =============================================================================
info "Driver installation complete."
info ""
info "NEXT STEPS:"
info "  1. Reboot the workstation."
info "  2. After reboot, run:  arc-verify-gpus"
info "  3. Then run:           bash scripts/setup_ollama.sh"
info ""
warn "A REBOOT IS REQUIRED before GPU drivers take effect."
