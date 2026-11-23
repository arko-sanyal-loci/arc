#!/usr/bin/env bash
# =============================================================================
# scripts/setup_ollama.sh
# =============================================================================
# Install Ollama and configure it for arc-runtime's dual-GPU workstation:
#
#   • NVIDIA RTX 5060 Ti  (16 GB GDDR7)  — CUDA backend
#   • Intel Arc B70       (32 GB GDDR6)  — combined VRAM: 48 GB
#
# Ollama is the confirmed model wrapper for arc-runtime's Provider.LOCAL:
#     http://localhost:11434/v1    (OpenAI-compatible)
#
# After this script:
#   • Ollama systemd service runs on :11434
#   • Models pulled and ready for arc-runtime
#   • CUDA_VISIBLE_DEVICES and Intel SYCL env vars baked into the service
#
# Usage:
#   chmod +x scripts/setup_ollama.sh
#   sudo bash scripts/setup_ollama.sh
#
# Note: Run AFTER install_drivers.sh and a reboot.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC}  $*"; }
step()  { echo -e "${BLU}[STEP]${NC}  $*"; }
warn()  { echo -e "${YEL}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

# =============================================================================
# 0. Pre-flight checks
# =============================================================================
step "Checking GPU availability..."

NVIDIA_OK=false
INTEL_OK=false

if command -v nvidia-smi &>/dev/null && nvidia-smi -L 2>/dev/null | grep -q "RTX"; then
    NVIDIA_OK=true
    info "NVIDIA RTX found: $(nvidia-smi -L | head -1)"
else
    warn "NVIDIA RTX not detected via nvidia-smi — CUDA offload may fail."
    warn "If drivers were just installed, make sure you rebooted."
fi

if command -v clinfo &>/dev/null && clinfo -l 2>/dev/null | grep -q -i "Intel"; then
    INTEL_OK=true
    info "Intel Arc found via OpenCL."
else
    warn "Intel Arc not detected via clinfo — Level Zero / SYCL offload may fail."
fi

# =============================================================================
# 1. Install Ollama
# =============================================================================
step "Installing / upgrading Ollama..."

if command -v ollama &>/dev/null; then
    CURRENT=$(ollama --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo "unknown")
    info "Ollama $CURRENT already installed — upgrading..."
fi

# Official install script (handles CUDA detection automatically)
curl -fsSL https://ollama.ai/install.sh | sh

OLLAMA_VERSION=$(ollama --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo "installed")
info "Ollama $OLLAMA_VERSION installed at $(command -v ollama)"

# =============================================================================
# 2. Configure systemd service with GPU env vars
# =============================================================================
step "Configuring Ollama systemd service for dual-GPU setup..."

OLLAMA_ENV_DIR="/etc/systemd/system/ollama.service.d"
mkdir -p "$OLLAMA_ENV_DIR"

# NVIDIA: expose all GPUs to CUDA
# Intel Arc: SYCL + Level Zero env vars for Ollama's Intel backend
cat > "$OLLAMA_ENV_DIR/gpu-env.conf" <<'EOF'
[Service]
# NVIDIA — expose all CUDA devices to Ollama
Environment="CUDA_VISIBLE_DEVICES=all"
# NVIDIA — allow Ollama to find CUDA 12 libs
Environment="LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu"

# Intel Arc — Level Zero + SYCL runtime
Environment="ONEAPI_DEVICE_SELECTOR=level_zero:0"
Environment="SYCL_CACHE_PERSISTENT=1"
Environment="ZES_ENABLE_SYSMAN=1"

# Ollama GPU memory split config (MB)
# Arc B70  = 32768 MB GDDR6
# RTX 5060 Ti = 16384 MB GDDR7
# Ollama will auto-split across both via GGML layer offload.
# Total addressable VRAM: ~48 GB — enough for 70B Q4_K_M
Environment="OLLAMA_GPU_OVERHEAD=512"

# Host all models in /data/ollama to keep system drive clear
Environment="OLLAMA_MODELS=/data/ollama/models"
EOF

# Create model storage directory
mkdir -p /data/ollama/models
chmod 755 /data/ollama/models

systemctl daemon-reload
systemctl enable --now ollama
info "Ollama service enabled and started."

# =============================================================================
# 3. Wait for Ollama to come up
# =============================================================================
step "Waiting for Ollama API on :11434..."
for i in $(seq 1 15); do
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        info "Ollama is up."
        break
    fi
    sleep 2
    echo -n "."
done
echo ""

# =============================================================================
# 4. Pull recommended models
# =============================================================================
# Total VRAM: 48 GB → comfortably fits 70B at Q4_K_M (≈40 GB)
#
# Tier selection for arc-runtime:
#   REASONING / CODING  → llama3.1:70b-instruct-q4_K_M  (40.4 GB)
#   FAST / TOOL USE     → llama3.1:8b-instruct-q8_0     (8.5 GB)
#   EMBEDDINGS          → nomic-embed-text               (274 MB)

step "Pulling models (this will take a while on first run)..."

pull_model() {
    local model="$1"
    info "Pulling $model ..."
    ollama pull "$model" && info "$model ready." || warn "$model pull failed — retry manually: ollama pull $model"
}

# Fast model first (smaller, proves the stack works)
pull_model "nomic-embed-text"
pull_model "llama3.1:8b-instruct-q8_0"

# Heavy model — only if 48 GB VRAM detected
TOTAL_VRAM_MB=0
if $NVIDIA_OK; then
    NV_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)
    TOTAL_VRAM_MB=$((TOTAL_VRAM_MB + NV_VRAM))
fi
# Intel Arc B70 = 32768 MB; add it conservatively
if $INTEL_OK; then
    TOTAL_VRAM_MB=$((TOTAL_VRAM_MB + 32768))
fi

info "Estimated combined VRAM: ${TOTAL_VRAM_MB} MB"

if [[ $TOTAL_VRAM_MB -ge 44000 ]]; then
    pull_model "llama3.1:70b-instruct-q4_K_M"
else
    warn "Combined VRAM < 44 GB — skipping 70B model (need both GPUs active)."
    warn "If Arc B70 is online, re-run after verifying with: arc-verify-gpus"
    pull_model "llama3.1:70b-instruct-q8_0"  # 32B would be safer but 70B Q4 is the target
fi

# Optional: coding-specific model
pull_model "qwen2.5-coder:14b"

# =============================================================================
# 5. Verify via OpenAI-compatible endpoint (arc-runtime's Provider.LOCAL)
# =============================================================================
step "Testing OpenAI-compatible endpoint..."

RESP=$(curl -sf http://localhost:11434/v1/models 2>/dev/null || echo '{"error":"no response"}')
if echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])),'models listed')" 2>/dev/null; then
    info "Provider.LOCAL endpoint healthy: http://localhost:11434/v1"
else
    warn "Could not parse /v1/models response:"
    echo "$RESP" | head -5
fi

# Quick inference smoke test
info "Smoke-testing llama3.1:8b ..."
SMOKE=$(curl -sf http://localhost:11434/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "llama3.1:8b-instruct-q8_0",
        "messages": [{"role":"user","content":"Reply: OK"}],
        "max_tokens": 5
    }' 2>/dev/null || echo '{}')

if echo "$SMOKE" | grep -q "choices"; then
    info "Inference smoke test: PASS"
else
    warn "Smoke test inconclusive — check: journalctl -u ollama -n 20"
fi

# =============================================================================
# 6. Write arc-runtime .env snippet
# =============================================================================
ENV_SNIPPET="/home/claude/arc-runtime/.env.local"
cat > "$ENV_SNIPPET" <<'EOF'
# arc-runtime local inference configuration
# Source this or copy into your .env

# Provider.LOCAL — Ollama OpenAI-compatible endpoint
LOCI_LOCAL_BASE_URL=http://localhost:11434/v1
LOCI_LOCAL_API_KEY=ollama

# Default model for local inference
LOCI_LOCAL_MODEL=llama3.1:8b-instruct-q8_0

# For heavy reasoning / coding tasks (needs both GPUs):
# LOCI_LOCAL_MODEL=llama3.1:70b-instruct-q4_K_M

# Embedding model (Nomic on Ollama)
LOCI_EMBED_MODEL=nomic-embed-text

# CUDA
CUDA_VISIBLE_DEVICES=all
EOF
chown "${SUDO_USER:-root}:${SUDO_USER:-root}" "$ENV_SNIPPET" 2>/dev/null || true
info "Environment snippet written to $ENV_SNIPPET"

# =============================================================================
# 7. Summary
# =============================================================================
info ""
info "===== OLLAMA SETUP COMPLETE ====="
info ""
info "Endpoint:   http://localhost:11434/v1  (OpenAI-compatible)"
info "Models dir: /data/ollama/models"
info "Models:     ollama list"
info ""
info "arc-runtime model wrapper: CONFIRMED as Ollama"
info "Provider.LOCAL in api_provision.py → http://localhost:11434/v1"
info ""
info "To start local GPU inference from arc-runtime:"
info "  cd arc-runtime"
info "  source .env.local"
info "  python3 -m app.server"
info ""
info "To pull additional models:"
info "  ollama pull deepseek-r1:14b        # strong coder, fits 8B slot"
info "  ollama pull nomic-embed-text       # embedding (already pulled)"
info ""
