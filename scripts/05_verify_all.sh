#!/usr/bin/env bash
# =============================================================================
# 05_verify_all.sh — Full system verification after setup + reboot
#                    Run this AFTER rebooting from 02_grub_firmware.sh
# =============================================================================
set -uo pipefail

GRN='\033[0;32m'; RED='\033[0;31m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
PASS=0; FAIL=0; WARN_COUNT=0

pass() { echo -e "  ${GRN}✓${NC} $*"; ((PASS++)); }
fail() { echo -e "  ${RED}✗${NC} $*"; ((FAIL++)); }
warn() { echo -e "  ${YLW}~${NC} $*"; ((WARN_COUNT++)); }
hdr()  { echo -e "\n${BLU}── $* ──${NC}"; }

echo "════════════════════════════════════════════"
echo "  LOCI Workstation — Full Verification"
echo "  $(date)"
echo "════════════════════════════════════════════"

# ── GPU Hardware ──────────────────────────────────────────────────────────────
hdr "GPU Hardware"
ARC=$(lspci | grep -iE "intel.*arc|battlemage|b580|b770" | head -1)
if [[ -n "$ARC" ]]; then pass "Arc B70 detected: ${ARC}"; else fail "Arc B70 not detected in lspci"; fi

RTX=$(lspci | grep -iE "nvidia.*5060|blackwell|GB206" | head -1)
if [[ -n "$RTX" ]]; then pass "RTX 5060 Ti detected: ${RTX}"; else warn "RTX 5060 Ti not detected (or different PCI ID)"; fi

# ── Kernel / xe driver ────────────────────────────────────────────────────────
hdr "Kernel Driver"
KERNEL=$(uname -r)
KERNEL_MAJOR=$(echo "$KERNEL" | cut -d. -f1)
KERNEL_MINOR=$(echo "$KERNEL" | cut -d. -f2)
if (( KERNEL_MAJOR > 6 || (KERNEL_MAJOR == 6 && KERNEL_MINOR >= 14) )); then
    pass "Kernel ${KERNEL} (≥ 6.14, xe supported)"
else
    warn "Kernel ${KERNEL} (xe/Xe2 support requires ≥ 6.14)"
fi

if lsmod | grep -q '^xe '; then
    pass "xe driver loaded"
elif lsmod | grep -q '^i915 '; then
    warn "i915 loaded (xe preferred for B70)"
else
    fail "Neither xe nor i915 loaded"
fi

XE_DEVS=$(ls /dev/dri/renderD* 2>/dev/null | wc -l || echo 0)
if (( XE_DEVS > 0 )); then pass "/dev/dri: ${XE_DEVS} render node(s) found"
else fail "/dev/dri/renderD* not found"; fi

# ── GRUB params ───────────────────────────────────────────────────────────────
hdr "GRUB Kernel Parameters"
CMDLINE=$(cat /proc/cmdline)
for PARAM in "xe.force_probe" "iommu=pt" "transparent_hugepage" "pcie_aspm=off"; do
    if echo "$CMDLINE" | grep -q "$PARAM"; then pass "${PARAM} in cmdline"
    else fail "${PARAM} NOT in cmdline"; fi
done

# ── Firmware / GuC ────────────────────────────────────────────────────────────
hdr "Firmware (GuC / HuC)"
if dmesg | grep -qi "guc.*initialized\|guc.*firmware.*version"; then
    GUC_VER=$(dmesg | grep -i 'guc.*firmware\|guc.*version' | head -1 | sed 's/.*xe/xe/')
    pass "GuC firmware loaded: ${GUC_VER}"
else
    warn "GuC firmware status unclear — check: dmesg | grep -i guc"
fi

# ── VA-API ────────────────────────────────────────────────────────────────────
hdr "VA-API (Hardware Video)"
export LIBVA_DRIVER_NAME=iHD
VA_OUT=$(vainfo 2>&1)
if echo "$VA_OUT" | grep -q 'VAProfile'; then
    VA_PROFILES=$(echo "$VA_OUT" | grep -c 'VAProfile' || echo 0)
    pass "VA-API iHD: ${VA_PROFILES} profiles"
else
    fail "VA-API iHD not working: $(echo "$VA_OUT" | head -2)"
fi

# ── OpenCL ────────────────────────────────────────────────────────────────────
hdr "OpenCL (Compute)"
CL_OUT=$(clinfo -l 2>&1)
if echo "$CL_OUT" | grep -qi 'intel.*arc\|intel.*xe\|intel.*gpu'; then
    pass "Intel Arc visible in clinfo"
else
    warn "Intel Arc not explicitly named in clinfo — check: clinfo"
fi
CL_DEVS=$(clinfo --raw 2>/dev/null | grep -c 'CL_DEVICE_NAME' || echo 0)
if (( CL_DEVS > 0 )); then pass "OpenCL devices: ${CL_DEVS}"; else fail "No OpenCL devices"; fi

# ── Vulkan ────────────────────────────────────────────────────────────────────
hdr "Vulkan"
VK_OUT=$(vulkaninfo --summary 2>&1 || true)
if echo "$VK_OUT" | grep -qi 'intel.*arc\|intel.*xe'; then
    pass "Intel Arc visible in Vulkan"
elif echo "$VK_OUT" | grep -qi 'GPU'; then
    warn "Vulkan has GPUs but Arc name not matched — check: vulkaninfo --summary"
else
    fail "Vulkan: no GPU detected"
fi

# ── Huge pages ────────────────────────────────────────────────────────────────
hdr "Huge Pages"
HP_TOTAL=$(grep 'HugePages_Total' /proc/meminfo | awk '{print $2}' || echo 0)
HP_FREE=$(grep 'HugePages_Free' /proc/meminfo | awk '{print $2}' || echo 0)
if (( HP_TOTAL >= 2048 )); then
    pass "Huge pages: ${HP_TOTAL} total, ${HP_FREE} free ($(( HP_TOTAL * 2 )) MB reserved)"
else
    warn "Huge pages: ${HP_TOTAL} (expected ≥ 4096 for 8 GB reservation)"
fi

# ── /dev/shm ring ─────────────────────────────────────────────────────────────
hdr "LOCI SHM Ring Buffer"
SHM_SIZE=$(df -h /dev/shm 2>/dev/null | awk 'NR==2{print $2}' || echo "?")
if [[ -d /dev/shm/loci_pipeline ]]; then
    pass "/dev/shm/loci_pipeline exists (total shm: ${SHM_SIZE})"
else
    warn "/dev/shm/loci_pipeline missing — run: mkdir -p /dev/shm/loci_pipeline"
fi

# ── Services ──────────────────────────────────────────────────────────────────
hdr "Services"
for SVC in ollama loci-arc-inference; do
    STATUS=$(systemctl is-active "$SVC" 2>/dev/null || echo "not-found")
    case "$STATUS" in
        active)   pass "${SVC}: active" ;;
        inactive) warn "${SVC}: inactive (start with: systemctl start ${SVC})" ;;
        *)        warn "${SVC}: ${STATUS}" ;;
    esac
done

# ── Network / inference endpoints ─────────────────────────────────────────────
hdr "Inference Endpoints"
if curl -sf http://localhost:11434/api/tags &>/dev/null; then
    pass "Ollama API: http://localhost:11434 ✓"
else
    warn "Ollama API not responding (start: systemctl start ollama)"
fi
if curl -sf http://localhost:8000/health &>/dev/null; then
    pass "Arc inference API: http://localhost:8000 ✓"
else
    warn "Arc inference API not responding (start: systemctl start loci-arc-inference)"
fi

# ── Python + LOCI venv ────────────────────────────────────────────────────────
hdr "Python / LOCI venv"
VENV="${HOME}/arkos-loci-repos/arc/.venv/bin/python3"
if [[ -f "$VENV" ]]; then
    PY_VER=$("$VENV" --version 2>&1)
    pass "venv python: ${PY_VER}"
    if "$VENV" -c "import torch; print(torch.__version__)" &>/dev/null; then
        TORCH_VER=$("$VENV" -c "import torch; print(torch.__version__)")
        pass "PyTorch: ${TORCH_VER}"
        if "$VENV" -c "import torch; assert torch.xpu.is_available(), 'no xpu'" &>/dev/null; then
            pass "PyTorch XPU (Arc) available  ✓"
        else
            warn "PyTorch XPU not available — IPEX may need manual install"
        fi
    else
        warn "PyTorch not installed in venv"
    fi
else
    warn "LOCI venv not found at ${VENV}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Results: ${PASS} passed  |  ${WARN_COUNT} warnings  |  ${FAIL} failed"
echo "════════════════════════════════════════════"

if (( FAIL > 0 )); then
    echo -e "\n${RED}Action needed for FAILED checks:${NC}"
    echo "  1. Reboot if you just ran 02_grub_firmware.sh"
    echo "  2. Check BIOS: ReBAR + VT-d + XMP I enabled"
    echo "  3. Re-run 01_display_drivers.sh if xe missing"
fi

echo ""
echo "Quick monitoring:"
echo "  intel_gpu_top          — live Arc GPU usage"
echo "  nvtop                  — combined Arc + RTX"
echo "  journalctl -u ollama   — Ollama logs"
echo "  dmesg | grep xe        — kernel driver messages"
