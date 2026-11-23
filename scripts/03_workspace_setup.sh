#!/usr/bin/env bash
# =============================================================================
# 03_workspace_setup.sh — Workspace + Python environment setup
#                          for LOCI / Arc runtime on Ubuntu 26.04
# =============================================================================
set -euo pipefail

GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info() { echo -e "${GRN}[WS]${NC} $*"; }
step() { echo -e "${BLU}[==]${NC} $*"; }
warn() { echo -e "${YLW}[WARN]${NC} $*"; }

LOCI_USER=${SUDO_USER:-arko-san}
LOCI_HOME=$(eval echo "~${LOCI_USER}")
REPOS_DIR="${LOCI_HOME}/arkos-loci-repos"
DATA_DIR="/data"

[[ $EUID -eq 0 ]] || { warn "Some steps need sudo. Run with: sudo bash $0"; }

# ── 1. Directory layout ────────────────────────────────────────────────────────
step "1/6  Creating workspace directory tree ..."
sudo -u "$LOCI_USER" mkdir -p \
    "${REPOS_DIR}" \
    "${DATA_DIR}/models/arc"         \
    "${DATA_DIR}/models/rtx"         \
    "${DATA_DIR}/models/shared"      \
    "${DATA_DIR}/datasets"           \
    "${DATA_DIR}/checkpoints"        \
    "${DATA_DIR}/cache/huggingface"  \
    "${DATA_DIR}/cache/ollama"       \
    "${DATA_DIR}/cache/vllm"         \
    "${DATA_DIR}/logs"               \
    "${LOCI_HOME}/.config/loci"      \
    "${LOCI_HOME}/.local/share/loci"
info "  Directory tree created  ✓"

# Symlink cache dirs to data partition (keeps / smaller)
sudo -u "$LOCI_USER" bash -c "
    [[ -L ~/.cache/huggingface ]] || { rm -rf ~/.cache/huggingface; ln -s /data/cache/huggingface ~/.cache/huggingface; }
" 2>/dev/null || true
info "  HuggingFace cache → /data/cache/huggingface  ✓"

# ── 2. Python + venv ──────────────────────────────────────────────────────────
step "2/6  Python environment"
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    python3-wheel python3-setuptools build-essential \
    git-lfs libffi-dev libssl-dev libxml2-dev libxslt1-dev \
    libjpeg-dev zlib1g-dev 2>/dev/null

VENV_DIR="${REPOS_DIR}/arc/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$LOCI_USER" python3 -m venv "$VENV_DIR"
    info "  Created venv at ${VENV_DIR}  ✓"
else
    info "  venv exists at ${VENV_DIR}  ✓"
fi

PYTHON="${VENV_DIR}/bin/python3"
PIP="${VENV_DIR}/bin/pip"

# Upgrade pip + wheel
sudo -u "$LOCI_USER" "$PIP" install --upgrade pip setuptools wheel -q

# Core runtime deps
sudo -u "$LOCI_USER" "$PIP" install \
    fastapi uvicorn httpx aiohttp \
    numpy pydantic \
    transformers accelerate \
    safetensors sentencepiece tokenizers \
    huggingface_hub \
    tqdm rich \
    cryptography \
    -q
info "  Core Python deps installed  ✓"

# Intel XPU/IPEX (best-effort — may not have a 26.04 wheel yet)
sudo -u "$LOCI_USER" "$PIP" install \
    intel-extension-for-pytorch \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/ \
    -q 2>/dev/null || warn "  IPEX XPU wheel not available — PyTorch will fall back to CPU on Arc"

info "  Python environment ready  ✓"

# ── 3. Ollama (RTX fast lane) ─────────────────────────────────────────────────
step "3/6  Ollama (RTX 5060 Ti fast lane)"
if ! command -v ollama &>/dev/null; then
    curl -fsSL https://ollama.com/install.sh | bash 2>/dev/null \
        && info "  Ollama installed  ✓" \
        || warn "  Ollama install failed — try manually: curl -fsSL https://ollama.com/install.sh | bash"
else
    info "  Ollama already installed  ✓"
fi

# Configure Ollama to use RTX (CUDA device 0)
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/arc-loci.conf <<'SYSTEMD'
[Service]
# LOCI fast lane: RTX 5060 Ti (CUDA device 0)
Environment="CUDA_VISIBLE_DEVICES=0"
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MODELS=/data/models/rtx"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
SYSTEMD
systemctl daemon-reload
systemctl enable ollama 2>/dev/null || true
info "  Ollama systemd unit configured for RTX  ✓"

# ── 4. vLLM / SGLang for Arc (deep lane) ─────────────────────────────────────
step "4/6  vLLM XPU (Arc B70 deep lane)"
# vLLM with XPU backend
sudo -u "$LOCI_USER" "$PIP" install \
    vllm \
    --extra-index-url https://download.pytorch.org/whl/xpu \
    -q 2>/dev/null || warn "  vLLM XPU not available yet — will use custom IPEX inference server"

# Arc inference systemd service (uses arc_loci_env.sh)
cat > /etc/systemd/system/loci-arc-inference.service <<SYSTEMD
[Unit]
Description=LOCI Arc B70 Inference Server (deep lane)
After=network.target
Wants=network.target

[Service]
Type=simple
User=${LOCI_USER}
WorkingDirectory=${REPOS_DIR}/arc
ExecStartPre=/bin/bash -c 'source ${REPOS_DIR}/arc/scripts/arc_loci_env.sh'
ExecStart=${VENV_DIR}/bin/python3 -m uvicorn app.server:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure
RestartSec=5
Environment="ONEAPI_DEVICE_SELECTOR=level_zero:0"
Environment="LIBVA_DRIVER_NAME=iHD"
Environment="LOCI_GPU_DEEP_LANE_DEVICE=0"
Environment="LOCI_GPU_FAST_LANE_DEVICE=1"
Environment="HF_HOME=/data/cache/huggingface"

[Install]
WantedBy=multi-user.target
SYSTEMD
systemctl daemon-reload
systemctl enable loci-arc-inference 2>/dev/null || true
info "  Arc inference systemd service created  ✓"

# ── 5. Environment variables in .bashrc / .profile ───────────────────────────
step "5/6  Shell environment"
ENV_BLOCK='
# ── LOCI / Arc Runtime ────────────────────────────────────────
export LOCI_REPOS="${HOME}/arkos-loci-repos"
export HF_HOME="/data/cache/huggingface"
export OLLAMA_MODELS="/data/models/rtx"
export LOCI_LOCAL_BASE_URL="http://localhost:11434"
export LOCI_LOCAL_INTEL_BASE_URL="http://localhost:8000"
export LOCI_SHM_DIR="/dev/shm/loci_pipeline"
export LIBVA_DRIVER_NAME="iHD"
# Arc venv
export LOCI_VENV="${HOME}/arkos-loci-repos/arc/.venv"
alias loci-activate="source ${LOCI_VENV}/bin/activate"
alias arc-env="source ${HOME}/arkos-loci-repos/arc/scripts/arc_loci_env.sh"
# Intel GPU tools
alias gpu-top="intel_gpu_top"
alias gpu-info="clinfo -l && vainfo && vulkaninfo --summary 2>/dev/null | head -20"
# ─────────────────────────────────────────────────────────────
'

BASHRC="${LOCI_HOME}/.bashrc"
if ! grep -q 'LOCI / Arc Runtime' "$BASHRC" 2>/dev/null; then
    echo "$ENV_BLOCK" >> "$BASHRC"
    info "  Shell aliases + env vars added to .bashrc  ✓"
else
    info "  .bashrc already has LOCI env block  ✓"
fi

# ── 6. /dev/shm ring buffer dir ───────────────────────────────────────────────
step "6/6  /dev/shm staging dir"
mkdir -p /dev/shm/loci_pipeline
chown "${LOCI_USER}:${LOCI_USER}" /dev/shm/loci_pipeline
chmod 700 /dev/shm/loci_pipeline
# Make it survive reboot via tmpfiles
echo "d /dev/shm/loci_pipeline 0700 ${LOCI_USER} ${LOCI_USER} -" \
    > /etc/tmpfiles.d/loci-pipeline.conf
info "  /dev/shm/loci_pipeline configured  ✓"

echo ""
echo "════════════════════════════════════════"
echo "  Workspace Setup Complete"
echo "════════════════════════════════════════"
echo ""
echo "Directory layout:"
echo "  ~/arkos-loci-repos/     — git repos"
echo "  /data/models/arc/       — large models for Arc B70 (32 GB deep lane)"
echo "  /data/models/rtx/       — fast models for RTX 5060 Ti (ollama)"
echo "  /data/cache/huggingface — HF model cache"
echo "  /dev/shm/loci_pipeline  — cross-GPU ring buffer"
echo ""
echo "Services:"
echo "  systemctl status ollama"
echo "  systemctl status loci-arc-inference"
echo ""
echo "Quick start:"
echo "  source ~/.bashrc && loci-activate"
echo "  arc-env"
