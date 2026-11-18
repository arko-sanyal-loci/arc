#!/usr/bin/env bash
# setup_intel_vllm.sh — Deploy Intel LLM-Scaler vLLM PV 1.4 on Arc Pro B70
# arc-runtime · Billo Tech
#
# Prerequisites:
#   1. BIOS: ReBAR enabled, Above 4G Decoding enabled, XMP I enabled
#   2. install_drivers.sh already run (Level Zero, oneAPI, intel-opencl-icd)
#   3. Docker installed with 'docker' in PATH
#
# Ports:
#   8000 → vLLM OpenAI-compatible API (Provider.LOCAL_INTEL in arc-runtime)
#   8001 → vLLM metrics endpoint
#
# Usage:
#   chmod +x scripts/setup_intel_vllm.sh
#   sudo ./scripts/setup_intel_vllm.sh [--model llama3.1:8b] [--port 8000]

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
MODEL="${MODEL:-meta-llama/Llama-3.1-8B-Instruct}"
SERVE_PORT="${SERVE_PORT:-8000}"
METRICS_PORT="${METRICS_PORT:-8001}"
VLLM_IMAGE="intel/intel-extension-for-pytorch:2.1.40-xpu-pip-base"
SCALER_IMAGE="intel/llm-scaler-vllm:latest"
MODEL_DIR="/data/vllm/models"
CONTAINER_NAME="arc-vllm-intel"
ENV_FILE="/home/$(logname)/.arc-runtime.env" 2>/dev/null || ENV_FILE="/root/.arc-runtime.env"

# ── Colour output ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR]${NC}   $*" >&2; }

# ── BIOS / hardware pre-flight ────────────────────────────────────────────────
check_rebar() {
    info "Checking Resizable BAR (ReBAR) status..."
    if lspci -vvv 2>/dev/null | grep -q "Resizable BAR"; then
        ok "ReBAR capable slot detected."
    else
        warn "ReBAR check inconclusive. Verify in BIOS: Advanced → PCIe → Resizable BAR → Enabled"
        warn "Also enable: Above 4G Decoding = Enabled"
        warn "Without ReBAR, Arc GPU performance is ~50% of rated spec."
    fi
}

check_arc_gpu() {
    info "Checking for Intel Arc GPU via Level Zero..."
    if command -v ze_info &>/dev/null; then
        if ze_info 2>/dev/null | grep -qi "Intel.*Arc\|Battlemage\|BMG\|ARC_PRO"; then
            ok "Intel Arc GPU found:"
            ze_info 2>/dev/null | grep "Device Name" | head -5
        else
            warn "ze_info ran but no Arc GPU found. Check BIOS and driver."
        fi
    else
        warn "ze_info not found — run install_drivers.sh first."
    fi
}

check_docker() {
    info "Checking Docker..."
    if ! command -v docker &>/dev/null; then
        err "Docker not installed. Installing..."
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
        usermod -aG docker "$(logname)" 2>/dev/null || true
        ok "Docker installed."
    else
        ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
    fi
}

check_hf_token() {
    if [[ -z "${HF_TOKEN:-}" ]]; then
        warn "HF_TOKEN not set. Gated models (Llama-3.1) need a Hugging Face token."
        warn "Export HF_TOKEN before running, or use an ungated model."
        warn "  export HF_TOKEN=hf_..."
        warn "  Ungated alternatives: Qwen/Qwen2.5-7B-Instruct, mistralai/Mistral-7B-Instruct-v0.3"
    fi
}

# ── Model directory ───────────────────────────────────────────────────────────
setup_model_dir() {
    info "Creating model directory: ${MODEL_DIR}"
    mkdir -p "${MODEL_DIR}"
    chmod 777 "${MODEL_DIR}"
    ok "Model dir ready."
}

# ── Pull LLM-Scaler image ─────────────────────────────────────────────────────
pull_image() {
    info "Pulling Intel LLM-Scaler vLLM image..."
    # Try the official Intel registry first (requires login), fall back to Docker Hub
    if docker pull "intelanalytics/ipex-llm-serving-xpu:latest" 2>/dev/null; then
        SCALER_IMAGE="intelanalytics/ipex-llm-serving-xpu:latest"
        ok "Pulled IPEX-LLM serving XPU image."
    elif docker pull "intel/intel-extension-for-pytorch:2.6.0-xpu" 2>/dev/null; then
        SCALER_IMAGE="intel/intel-extension-for-pytorch:2.6.0-xpu"
        ok "Pulled Intel Extension for PyTorch XPU image."
    else
        warn "Could not pull pre-built image. Building from Intel GitHub..."
        warn "See: https://github.com/intel/llm-scaler-vllm"
        warn "Manual: docker pull registry.hub.docker.com/intelanalytics/ipex-llm-serving-xpu"
    fi
}

# ── Create systemd service for vLLM Intel ─────────────────────────────────────
create_service() {
    info "Creating systemd service: arc-vllm-intel..."
    cat > /etc/systemd/system/arc-vllm-intel.service <<SERVICE
[Unit]
Description=Intel LLM-Scaler vLLM server (Arc Pro B70)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=on-failure
RestartSec=10
ExecStartPre=-/usr/bin/docker stop ${CONTAINER_NAME}
ExecStartPre=-/usr/bin/docker rm ${CONTAINER_NAME}
ExecStart=/usr/bin/docker run --rm \\
    --name ${CONTAINER_NAME} \\
    --device /dev/dri \\
    --ipc=host \\
    -e ZE_AFFINITY_MASK=0 \\
    -e ONEAPI_DEVICE_SELECTOR=level_zero:0 \\
    -e SYCL_CACHE_PERSISTENT=1 \\
    -e ZES_ENABLE_SYSMAN=1 \\
    -e VLLM_WORKER_MULTIPROC_METHOD=fork \\
    -e HF_TOKEN=\${HF_TOKEN:-} \\
    -v /dev/dri:/dev/dri \\
    -v /dev/shm:/dev/shm \\
    -v ${MODEL_DIR}:/models \\
    -p ${SERVE_PORT}:8000 \\
    -p ${METRICS_PORT}:8001 \\
    ${SCALER_IMAGE} \\
    python -m vllm.entrypoints.openai.api_server \\
        --model ${MODEL} \\
        --device xpu \\
        --dtype bfloat16 \\
        --max-model-len 8192 \\
        --gpu-memory-utilization 0.90 \\
        --port 8000 \\
        --host 0.0.0.0 \\
        --served-model-name arc-b70
ExecStop=/usr/bin/docker stop ${CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
SERVICE

    systemctl daemon-reload
    ok "Service arc-vllm-intel created."
}

# ── Write env vars ────────────────────────────────────────────────────────────
write_env() {
    info "Writing LOCAL_INTEL env to arc-runtime..."
    # Append to existing .env.local in arc-runtime
    ARC_ENV="/home/$(logname)/arc-runtime/.env.local" 2>/dev/null || ARC_ENV="/root/arc-runtime/.env.local"
    if [[ -f "${ARC_ENV}" ]]; then
        # Remove any existing LOCAL_INTEL lines
        sed -i '/LOCI_LOCAL_INTEL/d' "${ARC_ENV}"
    fi
    cat >> "${ARC_ENV}" <<ENV

# Intel LLM-Scaler vLLM (Arc B70) — Provider.LOCAL_INTEL
LOCI_LOCAL_INTEL_BASE_URL=http://localhost:${SERVE_PORT}/v1
LOCI_LOCAL_INTEL_API_KEY=local
LOCI_LOCAL_INTEL_MODEL=arc-b70
ENV
    ok "Written to ${ARC_ENV}"
}

# ── Smoke test ────────────────────────────────────────────────────────────────
smoke_test() {
    info "Waiting 15s for vLLM to load model..."
    sleep 15
    info "Smoke test: GET /v1/models"
    if curl -sf "http://localhost:${SERVE_PORT}/v1/models" | python3 -m json.tool | head -20; then
        ok "vLLM Intel responding on port ${SERVE_PORT}"
    else
        warn "vLLM not responding yet. Check: journalctl -u arc-vllm-intel -f"
        warn "Model loading may take 2-5 min on first run."
    fi
}

# ── Print model routing cheatsheet ───────────────────────────────────────────
print_routing() {
    echo ""
    echo "┌─────────────────────────────────────────────────────────────────────┐"
    echo "│  arc-runtime dual-track routing                                     │"
    echo "├─────────────────────────────────────────────────────────────────────┤"
    echo "│  RTX 5060 Ti  │ Ollama (CUDA)        │ localhost:11434 │ ≤8B models │"
    echo "│  Arc B70      │ Intel vLLM (SYCL)   │ localhost:${SERVE_PORT}  │ 9B–70B    │"
    echo "│  Fallback     │ NEXOS → XAI          │ remote API      │ any        │"
    echo "├─────────────────────────────────────────────────────────────────────┤"
    echo "│  BIOS REQUIRED: ReBAR=ON, Above4G=ON, XMP_I=ON                     │"
    echo "└─────────────────────────────────────────────────────────────────────┘"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo "══ Intel LLM-Scaler vLLM Setup — Arc Pro B70 ══"
    echo ""

    check_rebar
    check_arc_gpu
    check_docker
    check_hf_token
    setup_model_dir
    pull_image
    create_service
    write_env
    print_routing

    echo ""
    info "Starting arc-vllm-intel service..."
    systemctl enable arc-vllm-intel
    systemctl start arc-vllm-intel

    smoke_test

    echo ""
    ok "Intel LLM-Scaler vLLM deployed."
    echo ""
    echo "  Logs:    journalctl -u arc-vllm-intel -f"
    echo "  Status:  systemctl status arc-vllm-intel"
    echo "  API:     http://localhost:${SERVE_PORT}/v1"
    echo "  Stop:    systemctl stop arc-vllm-intel"
    echo ""
    echo "  Next: wire Provider.LOCAL_INTEL in arc-runtime/core/api_provision.py"
    echo "        env key: LOCI_LOCAL_INTEL_BASE_URL=http://localhost:${SERVE_PORT}/v1"
}

main "$@"
