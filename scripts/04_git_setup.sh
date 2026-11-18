#!/usr/bin/env bash
# =============================================================================
# 04_git_setup.sh — Git + GitHub organization for LOCI repos on workstation
# =============================================================================
set -euo pipefail

GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
info() { echo -e "${GRN}[GIT]${NC} $*"; }
step() { echo -e "${BLU}[==]${NC} $*"; }
warn() { echo -e "${YLW}[WARN]${NC} $*"; }

LOCI_USER=${SUDO_USER:-arko-san}
LOCI_HOME=$(eval echo "~${LOCI_USER}")
REPOS_DIR="${LOCI_HOME}/arkos-loci-repos"

# ── 1. Global git config ──────────────────────────────────────────────────────
step "1/5  Git global config"
sudo -u "$LOCI_USER" git config --global user.name  "Arko Sanyal"
sudo -u "$LOCI_USER" git config --global user.email "arko.sanyal@gmail.com"
sudo -u "$LOCI_USER" git config --global core.editor "vim"
sudo -u "$LOCI_USER" git config --global pull.rebase false
sudo -u "$LOCI_USER" git config --global push.default current
sudo -u "$LOCI_USER" git config --global init.defaultBranch main
sudo -u "$LOCI_USER" git config --global fetch.prune true
sudo -u "$LOCI_USER" git config --global diff.colorMoved zebra
sudo -u "$LOCI_USER" git config --global core.autocrlf input
sudo -u "$LOCI_USER" git config --global credential.helper "store"

# Git LFS
git lfs install --system 2>/dev/null || sudo -u "$LOCI_USER" git lfs install
info "  Global git config set  ✓"

# ── 2. SSH key for GitHub ─────────────────────────────────────────────────────
step "2/5  SSH key"
SSH_KEY="${LOCI_HOME}/.ssh/id_ed25519"
SSH_CFG="${LOCI_HOME}/.ssh/config"
mkdir -p "${LOCI_HOME}/.ssh"
chmod 700 "${LOCI_HOME}/.ssh"
chown -R "${LOCI_USER}:${LOCI_USER}" "${LOCI_HOME}/.ssh"

if [[ ! -f "$SSH_KEY" ]]; then
    sudo -u "$LOCI_USER" ssh-keygen -t ed25519 \
        -C "arko.sanyal@gmail.com" \
        -f "$SSH_KEY" -N "" -q
    info "  SSH key generated: ${SSH_KEY}  ✓"
    echo ""
    warn "  Add this public key to https://github.com/settings/keys :"
    echo "  ──────────────────────────────────────────────────────────"
    cat "${SSH_KEY}.pub"
    echo "  ──────────────────────────────────────────────────────────"
    echo ""
else
    info "  SSH key exists: ${SSH_KEY}  ✓"
fi

# SSH config for GitHub
if ! grep -q 'Host github.com' "$SSH_CFG" 2>/dev/null; then
    cat >> "$SSH_CFG" <<'SSH'

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    AddKeysToAgent yes
    StrictHostKeyChecking accept-new
SSH
    chmod 600 "$SSH_CFG"
    chown "${LOCI_USER}:${LOCI_USER}" "$SSH_CFG"
    info "  SSH config written  ✓"
fi

# ── 3. Clone / update all LOCI repos ─────────────────────────────────────────
step "3/5  Cloning / updating LOCI repos"
mkdir -p "$REPOS_DIR"

declare -A REPOS=(
    ["arc"]="git@github.com:arko-sanyal-loci/arc.git"
    ["LOCI-MEMORY-ENGINE"]="git@github.com:arko-sanyal-loci/LOCI-MEMORY-ENGINE.git"
    ["loci-coordination-bus"]="git@github.com:arko-sanyal-loci/loci-coordination-bus.git"
)

for NAME in "${!REPOS[@]}"; do
    URL="${REPOS[$NAME]}"
    TARGET="${REPOS_DIR}/${NAME}"
    if [[ -d "${TARGET}/.git" ]]; then
        info "  Updating ${NAME} ..."
        sudo -u "$LOCI_USER" git -C "$TARGET" fetch --all -q
        sudo -u "$LOCI_USER" git -C "$TARGET" pull -q || warn "  ${NAME}: pull failed (local changes?)"
    else
        info "  Cloning ${NAME} ..."
        sudo -u "$LOCI_USER" git clone "$URL" "$TARGET" -q \
            && info "  ${NAME} cloned  ✓" \
            || warn "  ${NAME} clone failed — SSH key added to GitHub?"
    fi
done

# ── 4. Per-repo .git/config overrides ────────────────────────────────────────
step "4/5  Per-repo settings"
ARC_REPO="${REPOS_DIR}/arc"
if [[ -d "${ARC_REPO}/.git" ]]; then
    sudo -u "$LOCI_USER" git -C "$ARC_REPO" config core.hooksPath .githooks
    # Create commit-msg hook to enforce attribution
    mkdir -p "${ARC_REPO}/.githooks"
    cat > "${ARC_REPO}/.githooks/commit-msg" <<'HOOK'
#!/usr/bin/env bash
# Enforce Co-Authored-By attribution on AI-assisted commits
MSG=$(cat "$1")
if echo "$MSG" | grep -q "Claude\|claude"; then
    if ! echo "$MSG" | grep -q "Co-Authored-By: Claude"; then
        echo "WARN: AI-assisted commit missing Co-Authored-By line" >&2
    fi
fi
HOOK
    chmod +x "${ARC_REPO}/.githooks/commit-msg"
    info "  arc: .githooks/commit-msg installed  ✓"
fi

# ── 5. Shared .gitignore_global ───────────────────────────────────────────────
step "5/5  Global .gitignore"
cat > "${LOCI_HOME}/.gitignore_global" <<'GITIGNORE'
# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
.venv/
venv/
env/
*.egg-info/
dist/
build/
.eggs/
*.whl

# IDE
.vscode/settings.json
.idea/
*.swp
*.swo
*~
.DS_Store
.envrc

# LOCI secrets + keys
*.key
*.pem
secrets.yaml
.env
.env.*
!.env.example

# Model weights (too large for git; track via git-lfs or exclude)
*.safetensors
*.bin
*.gguf
*.pt
*.ckpt
!requirements*.txt

# Logs
logs/
*.log

# /dev/shm staging
loci_pipeline/
*.shm

# Cache
.cache/
__ai_cache__/
*.pyc
GITIGNORE

sudo -u "$LOCI_USER" git config --global core.excludesfile "${LOCI_HOME}/.gitignore_global"
info "  Global .gitignore installed  ✓"

echo ""
echo "════════════════════════════════════════"
echo "  Git Setup Complete"
echo "════════════════════════════════════════"
echo ""
echo "Repos at ${REPOS_DIR}:"
for NAME in arc LOCI-MEMORY-ENGINE loci-coordination-bus; do
    if [[ -d "${REPOS_DIR}/${NAME}/.git" ]]; then
        BRANCH=$(git -C "${REPOS_DIR}/${NAME}" branch --show-current 2>/dev/null || echo "?")
        HASH=$(git -C "${REPOS_DIR}/${NAME}" rev-parse --short HEAD 2>/dev/null || echo "?")
        echo "  ✓ ${NAME}  [${BRANCH} @ ${HASH}]"
    else
        echo "  ✗ ${NAME}  (not cloned)"
    fi
done
echo ""
echo "GitHub SSH test:"
echo "  ssh -T git@github.com"
