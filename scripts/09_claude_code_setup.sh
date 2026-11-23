#!/usr/bin/env bash
# 09_claude_code_setup.sh — Claude Code CLI + Arc Chat runtime setup
# Installs Claude Code on the LOCI workstation and wires Arc Chat as the
# central hub so you can "speak to all" agents from one terminal.
#
# Run as: sudo bash scripts/09_claude_code_setup.sh
# Or as the target user: bash scripts/09_claude_code_setup.sh --user arko-san

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_USER="${SUDO_USER:-${1#--user=}}"
TARGET_USER="${TARGET_USER:-arko-san}"
HOME_DIR="/home/${TARGET_USER}"
REPO_DIR="${HOME_DIR}/arkos-loci-repos/arc"
NODE_MAJOR=22        # LTS

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

need_root() {
    [[ $EUID -eq 0 ]] || fail "Re-run with sudo for step: $1"
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Node.js LTS (required for Claude Code npm package)
# ══════════════════════════════════════════════════════════════════════════════
info "Step 1 — Node.js ${NODE_MAJOR} LTS"
need_root "Node.js install"

if node --version 2>/dev/null | grep -q "^v${NODE_MAJOR}"; then
    ok "Node.js $(node --version) already installed"
else
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y nodejs
    ok "Node.js $(node --version) installed"
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Claude Code CLI  (@anthropic-ai/claude-code)
# ══════════════════════════════════════════════════════════════════════════════
info "Step 2 — Claude Code CLI"
if claude --version 2>/dev/null | grep -q "claude"; then
    ok "Claude Code $(claude --version) already installed"
else
    npm install -g @anthropic-ai/claude-code
    ok "Claude Code installed: $(claude --version)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — API key & environment wiring
# ══════════════════════════════════════════════════════════════════════════════
info "Step 3 — Environment config in ${HOME_DIR}/.bashrc"

BASHRC="${HOME_DIR}/.bashrc"

add_if_absent() {
    local marker="$1" line="$2"
    grep -qF "$marker" "$BASHRC" 2>/dev/null || echo "$line" >> "$BASHRC"
}

# ANTHROPIC_API_KEY placeholder (user fills this in)
add_if_absent "ANTHROPIC_API_KEY" \
    'export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"   # fill in your key'

# Claude Code working directory = the arc repo
add_if_absent "CLAUDE_CODE_DIR" \
    "export CLAUDE_CODE_DIR=\"${REPO_DIR}\""

# Arc Chat endpoints
add_if_absent "ARC_INFERENCE_URL" \
    'export ARC_INFERENCE_URL="http://localhost:8000"'
add_if_absent "RTX_INFERENCE_URL" \
    'export RTX_INFERENCE_URL="http://localhost:11434"'

# Arc Chat alias — 'chat' opens the terminal hub
add_if_absent "alias chat=" \
    "alias chat='python ${REPO_DIR}/app/arc_chat.py'"

# Arc Chat API server alias
add_if_absent "alias arc-serve=" \
    "alias arc-serve='python ${REPO_DIR}/app/arc_chat.py --serve'"

chown "${TARGET_USER}:${TARGET_USER}" "$BASHRC"
ok ".bashrc wired"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Python deps for Arc Chat + semantic_constant
# ══════════════════════════════════════════════════════════════════════════════
info "Step 4 — Python packages (httpx, fastapi, uvicorn, datasets, ipex)"

VENV="${HOME_DIR}/arkos-loci-repos/arc/.venv"
if [[ ! -d "$VENV" ]]; then
    sudo -u "${TARGET_USER}" python3 -m venv "$VENV"
fi

PIPX="${VENV}/bin/pip"
sudo -u "${TARGET_USER}" "$PIPX" install --quiet --upgrade pip

# Core Arc Chat deps
sudo -u "${TARGET_USER}" "$PIPX" install --quiet \
    httpx \
    fastapi \
    "uvicorn[standard]"

# Semantic constant deps (best-effort — may need internet)
sudo -u "${TARGET_USER}" "$PIPX" install --quiet \
    datasets \
    Pillow \
    numpy \
    scipy || warn "datasets/numpy/scipy install failed — will use synthetic fallback"

# IPEX for Arc B70 XPU (Intel Extension for PyTorch)
# Requires oneAPI base toolkit; skip gracefully if not available
if python3 -c "import torch" 2>/dev/null; then
    sudo -u "${TARGET_USER}" "$PIPX" install --quiet \
        intel-extension-for-pytorch || warn "IPEX not installed — Arc XPU compute unavailable"
else
    warn "PyTorch not found; skipping IPEX — run scripts/01_display_drivers.sh first"
fi

ok "Python deps done"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Claude Code project init in the arc repo
# ══════════════════════════════════════════════════════════════════════════════
info "Step 5 — Claude Code project init in ${REPO_DIR}"

CLAUDE_DIR="${REPO_DIR}/.claude"
mkdir -p "$CLAUDE_DIR"

# Write a minimal CLAUDE.md so Claude Code knows the project structure
cat > "${REPO_DIR}/CLAUDE.md" <<'CLAUDE_MD'
# LOCI Arc — Claude Code project context

## Architecture
- **Arc B70 (deep lane)** — level_zero:0, port 8000, COMPLEX/SOVEREIGN queries
- **RTX 5060 Ti (fast lane)** — CUDA:0, port 11434 (Ollama), TRIVIAL/MODERATE queries
- **Arc Chat hub** — `app/arc_chat.py`, port 9000, routes by semantic entropy

## Key dirs
| Path | Purpose |
|------|---------|
| `core/` | LOCI engine: speculative_pipeline, semantic_constant, lora_kd_trainer |
| `app/`  | Arc Chat hub, inference server wrappers |
| `scripts/` | Setup scripts 00–09 |
| `docs/` | SSH recovery, setup guide |
| `/data/cache/semantic_constant/` | Measured semantic constant (after first run) |

## Running things
```bash
# Terminal chat (routes to right GPU automatically)
chat

# API server (for other agents to call)
arc-serve --port 9000

# Run semantic constant measurement (first time, needs ~1h on full dataset)
python core/semantic_constant.py --mode both --samples 50000

# Speculative pipeline
python core/speculative_pipeline.py
```

## Commit attribution
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MV4Gz1zFvuTYkU3DJbhsAd
CLAUDE_MD

chown -R "${TARGET_USER}:${TARGET_USER}" "${REPO_DIR}/CLAUDE.md" "$CLAUDE_DIR"
ok "CLAUDE.md written — Claude Code will find it on first run"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Arc Chat systemd service (optional, starts on boot)
# ══════════════════════════════════════════════════════════════════════════════
info "Step 6 — Arc Chat systemd service"

cat > /etc/systemd/system/arc-chat.service <<SERVICE
[Unit]
Description=LOCI Arc Chat Hub (universal semantic router)
After=network.target

[Service]
User=${TARGET_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV}/bin/python app/arc_chat.py --serve --port 9000
Restart=on-failure
RestartSec=5
Environment=ARC_INFERENCE_URL=http://localhost:8000
Environment=RTX_INFERENCE_URL=http://localhost:11434
# Set your key: systemctl edit arc-chat  → [Service] Environment=ANTHROPIC_API_KEY=sk-...

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
# Don't auto-enable — user enables after setting API key
ok "arc-chat.service written (not yet enabled)"
info "To start: sudo systemctl enable --now arc-chat"
info "To set API key: sudo systemctl edit arc-chat"

# ══════════════════════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Claude Code + Arc Chat setup complete                  ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo "  1. Set your API key in ~/.bashrc:"
echo "     export ANTHROPIC_API_KEY=sk-ant-..."
echo "  2. Source it:  source ~/.bashrc"
echo "  3. Launch:     chat           (terminal hub)"
echo "                 arc-serve      (API server on :9000)"
echo "  4. Verify:     claude --version"
echo ""
echo "  Arc Chat routes by semantic entropy H:"
printf "    H < %.1f → RTX (fast lane)\n" "3.2"
printf "    H ∈ [%.1f, %.1f] → dual-lane speculative\n" "3.2" "5.8"
printf "    H > %.1f → Arc B70 (deep lane)\n" "5.8"
echo ""
