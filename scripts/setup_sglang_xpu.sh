#!/usr/bin/env bash
# setup_sglang_xpu.sh — Deploy SGLang XPU on Arc Pro B70 (Xe2/Battlemage)
# arc-runtime · Billo Tech
#
# STATUS: FRONTIER / EXPERIMENTAL as of 2026-11-18
#   - SGLang XPU is optimized for Xe2 (Arc B-Series, Arc Pro B-Series)
#   - Intel + RadixArk official collaboration announced Nov 12 2026
#   - Docker image "under active development" — source-build for now
#   - For production today: use setup_intel_vllm.sh (LLM-Scaler PV 1.4)
#   - Expected stable Docker ~Q1 2027
#
# Port: 7000 → SGLang OpenAI-compatible API (Provider.LOCAL_SGLANG)
#
# Requires:
#   - oneAPI + Level Zero (install_drivers.sh)
#   - conda or mamba for Python env isolation
#   - ReBAR enabled in BIOS

set -euo pipefail

SERVE_PORT="${SERVE_PORT:-7000}"
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
CONDA_ENV="sglang-xpu"
SGLANG_DIR="/opt/sglang-xpu"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR]${NC}   $*" >&2; }

banner() {
    echo ""
    echo "══ SGLang XPU Setup — Arc Pro B70 (Xe2) [EXPERIMENTAL] ══"
    echo ""
    warn "This is the FRONTIER path. For production serving use setup_intel_vllm.sh instead."
    warn "SGLang XPU Docker is under active development as of Nov 2026."
    echo ""
}

check_prerequisites() {
    info "Checking Level Zero / oneAPI..."
    if ! command -v ze_info &>/dev/null; then
        err "Level Zero tools not found. Run install_drivers.sh first."
        exit 1
    fi
    ze_info 2>/dev/null | grep "Device Name" | head -3

    info "Checking conda/mamba..."
    if command -v mamba &>/dev/null; then
        CONDA_CMD="mamba"
    elif command -v conda &>/dev/null; then
        CONDA_CMD="conda"
    else
        err "conda/mamba not found. Installing miniforge..."
        curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh" \
            -o /tmp/miniforge.sh
        bash /tmp/miniforge.sh -b -p /opt/miniforge3
        export PATH="/opt/miniforge3/bin:$PATH"
        CONDA_CMD="conda"
        ok "Miniforge installed at /opt/miniforge3"
    fi
    ok "Using ${CONDA_CMD}"
}

source_oneapi() {
    info "Sourcing oneAPI environment..."
    if [[ -f /opt/intel/oneapi/setvars.sh ]]; then
        # shellcheck disable=SC1091
        source /opt/intel/oneapi/setvars.sh --force 2>/dev/null || true
        ok "oneAPI environment sourced."
    elif [[ -f /etc/profile.d/oneapi.sh ]]; then
        # shellcheck disable=SC1091
        source /etc/profile.d/oneapi.sh 2>/dev/null || true
        ok "oneAPI env sourced from profile.d"
    else
        warn "oneAPI setvars.sh not found. Some XPU ops may fail."
    fi
}

create_conda_env() {
    info "Creating conda env: ${CONDA_ENV} (Python 3.12)..."
    ${CONDA_CMD} create -n "${CONDA_ENV}" python=3.12 -y 2>/dev/null || \
        warn "Env '${CONDA_ENV}' may already exist — continuing."

    # Activate
    # shellcheck disable=SC1091
    eval "$(conda shell.bash hook 2>/dev/null)" || true
    conda activate "${CONDA_ENV}" 2>/dev/null || {
        warn "Could not activate conda env interactively. Run manually:"
        warn "  conda activate ${CONDA_ENV}"
        warn "  pip install sglang[xpu]"
    }
    ok "Conda env ready: ${CONDA_ENV}"
}

install_xpu_torch() {
    info "Installing PyTorch XPU (Intel Extension)..."
    # Per SGLang XPU docs: "Getting Started on Intel GPU" prerequisite
    pip install --quiet \
        torch==2.6.0+xpu \
        intel_extension_for_pytorch==2.6.0+xpu \
        oneccl_bind_pt==2.6.0+xpu \
        --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/

    ok "PyTorch XPU installed."
}

install_sglang() {
    info "Installing SGLang from source (XPU path)..."
    mkdir -p "${SGLANG_DIR}"
    cd "${SGLANG_DIR}"

    if [[ ! -d "sglang/.git" ]]; then
        git clone --depth 1 https://github.com/sgl-project/sglang.git
    else
        info "SGLang source already cloned — pulling latest."
        git -C sglang pull --ff-only 2>/dev/null || true
    fi

    cd sglang
    pip install --quiet -e "python[xpu]"
    ok "SGLang XPU installed from source."
}

create_launch_script() {
    info "Writing SGLang launch script..."
    mkdir -p /usr/local/bin
    cat > /usr/local/bin/arc-sglang-start <<LAUNCH
#!/usr/bin/env bash
# Launch SGLang XPU server on Arc Pro B70
set -euo pipefail

source /etc/profile.d/oneapi.sh 2>/dev/null || true
eval "\$(conda shell.bash hook 2>/dev/null)" || true
conda activate ${CONDA_ENV} 2>/dev/null || true

export ZE_AFFINITY_MASK=0          # GPU 0 = Arc B70
export ONEAPI_DEVICE_SELECTOR=level_zero:0
export SYCL_CACHE_PERSISTENT=1
export ZES_ENABLE_SYSMAN=1

MODEL="\${1:-${MODEL}}"
PORT="\${2:-${SERVE_PORT}}"

echo "[SGLang XPU] Starting server: model=\${MODEL} port=\${PORT}"

python -m sglang.launch_server \\
    --model-path "\${MODEL}" \\
    --device xpu \\
    --host 0.0.0.0 \\
    --port "\${PORT}" \\
    --attention-backend intel_xpu \\
    --cuda-graph-backend-prefill tc_piecewise \\
    --trust-remote-code \\
    --max-total-tokens 16384
LAUNCH
    chmod +x /usr/local/bin/arc-sglang-start
    ok "Launch script: /usr/local/bin/arc-sglang-start"
}

create_service() {
    info "Creating systemd service: arc-sglang-xpu..."
    cat > /etc/systemd/system/arc-sglang-xpu.service <<SERVICE
[Unit]
Description=SGLang XPU server (Arc Pro B70 — experimental)
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=15
User=root
Environment="ONEAPI_DEVICE_SELECTOR=level_zero:0"
Environment="SYCL_CACHE_PERSISTENT=1"
Environment="ZES_ENABLE_SYSMAN=1"
Environment="ZE_AFFINITY_MASK=0"
ExecStart=/usr/local/bin/arc-sglang-start ${MODEL} ${SERVE_PORT}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

    systemctl daemon-reload
    ok "Service arc-sglang-xpu created (NOT auto-started — experimental)."
}

write_env() {
    ARC_ENV="/home/$(logname 2>/dev/null || echo root)/arc-runtime/.env.local"
    if [[ -f "${ARC_ENV}" ]]; then
        sed -i '/LOCI_LOCAL_SGLANG/d' "${ARC_ENV}"
        cat >> "${ARC_ENV}" <<ENV

# SGLang XPU (Arc B70 — experimental) — Provider.LOCAL_SGLANG
LOCI_LOCAL_SGLANG_BASE_URL=http://localhost:${SERVE_PORT}/v1
LOCI_LOCAL_SGLANG_API_KEY=local
LOCI_LOCAL_SGLANG_MODEL=arc-b70-sglang
ENV
        ok "SGLang env vars written to ${ARC_ENV}"
    fi
}

print_summary() {
    echo ""
    echo "┌──────────────────────────────────────────────────────────────────────┐"
    echo "│  SGLang XPU — Arc Pro B70                                            │"
    echo "├──────────────────────────────────────────────────────────────────────┤"
    echo "│  API endpoint:  http://localhost:${SERVE_PORT}/v1                        │"
    echo "│  Start:  systemctl start arc-sglang-xpu  (or arc-sglang-start)       │"
    echo "│  Logs:   journalctl -u arc-sglang-xpu -f                             │"
    echo "├──────────────────────────────────────────────────────────────────────┤"
    echo "│  P/D disaggregation (two Arc GPUs):                                  │"
    echo "│   ZE_AFFINITY_MASK=0 python -m sglang.launch_server --device xpu \\  │"
    echo "│     --disaggregation-mode prefill --port 30000                        │"
    echo "│   ZE_AFFINITY_MASK=1 python -m sglang.launch_server --device xpu \\  │"
    echo "│     --disaggregation-mode decode  --port 30001                        │"
    echo "├──────────────────────────────────────────────────────────────────────┤"
    echo "│  PRODUCTION: use arc-vllm-intel (LLM-Scaler PV 1.4) instead          │"
    echo "└──────────────────────────────────────────────────────────────────────┘"
    echo ""
}

main() {
    banner
    check_prerequisites
    source_oneapi
    create_conda_env
    install_xpu_torch
    install_sglang
    create_launch_script
    create_service
    write_env
    print_summary

    ok "SGLang XPU setup complete."
    warn "Service NOT started automatically — this is experimental."
    echo "  To start: systemctl start arc-sglang-xpu"
    echo "  Or manually: arc-sglang-start [model] [port]"
}

main "$@"
