#!/usr/bin/env bash
# ============================================================================
# Arc Runtime — startup script
# Usage: bash start.sh [--port 7860] [--no-heartbeat] [--no-gpu]
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Defaults ─────────────────────────────────────────────────────────────
PORT="${ARC_PORT:-7860}"
SKIP_HEARTBEAT=false
SKIP_GPU=false
DB_PATH="data/arc.db"
LOG_DIR="logs"
SHM_RING_PATH="/dev/shm/arc_offload_ring"
SHM_HB_PATH="/dev/shm/arc_heartbeat"

# ── Args ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)       PORT="$2"; shift 2 ;;
    --no-heartbeat) SKIP_HEARTBEAT=true; shift ;;
    --no-gpu)     SKIP_GPU=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Colors ───────────────────────────────────────────────────────────────
GRN="\033[0;32m"; YLW="\033[0;33m"; RED="\033[0;31m"; RST="\033[0m"
BLD="\033[1m"
ok()   { echo -e "${GRN}✓${RST} $*"; }
warn() { echo -e "${YLW}⚠${RST}  $*"; }
err()  { echo -e "${RED}✗${RST} $*"; }
hdr()  { echo -e "\n${BLD}$*${RST}"; }

echo -e "${BLD}"
echo "   ╔══════════════════════════════╗"
echo "   ║        Arc Runtime           ║"
echo "   ║  Loci Engine  ·  7.9 GB/s   ║"
echo "   ╚══════════════════════════════╝"
echo -e "${RST}"

# ── 1. Environment ────────────────────────────────────────────────────────
hdr "[ 1/6 ] Environment"

if [[ -f ".env" ]]; then
  set -o allexport; source .env; set +o allexport
  ok ".env loaded"
else
  warn ".env not found — using shell environment"
fi

for key_var in ARC_XAI_KEY ARC_NEXOS_KEY; do
  if [[ -n "${!key_var:-}" ]]; then
    ok "$key_var set"
  else
    warn "$key_var not set — provider will be skipped"
  fi
done

# ── 2. Directories ────────────────────────────────────────────────────────
hdr "[ 2/6 ] Directories"

mkdir -p data logs
ok "data/, logs/"

# /dev/shm ring buffers
if [[ -d /dev/shm ]]; then
  # 4 MiB ring (from bench_bus_fidelity benchmark)
  RING_SIZE=$((4 * 1024 * 1024))
  if [[ ! -f "${SHM_RING_PATH}" ]]; then
    dd if=/dev/zero of="${SHM_RING_PATH}" bs=1 count="${RING_SIZE}" 2>/dev/null
    ok "/dev/shm ring buffer allocated (4 MiB)"
  else
    ok "/dev/shm ring buffer already exists"
  fi
  if [[ ! -f "${SHM_HB_PATH}" ]]; then
    dd if=/dev/zero of="${SHM_HB_PATH}" bs=1 count=4096 2>/dev/null
    ok "/dev/shm heartbeat buffer allocated"
  fi
else
  warn "/dev/shm not available — ring buffers disabled (CPU-only mode)"
fi

# ── 3. Python / deps ──────────────────────────────────────────────────────
hdr "[ 3/6 ] Python"

if ! python3 -c "import fastapi, uvicorn, aiosqlite, pydantic" 2>/dev/null; then
  warn "Some dependencies missing — installing from requirements.txt"
  pip install -q --break-system-packages -r requirements.txt
  ok "Dependencies installed"
else
  ok "All core dependencies present"
fi

# ── 4. Database ───────────────────────────────────────────────────────────
hdr "[ 4/6 ] Database"

python3 - <<'PYEOF'
import asyncio, sys
sys.path.insert(0, ".")
from core.engine_sync import EngineSync
sync = EngineSync()
asyncio.run(asyncio.to_thread(sync.ensure_db))
print("  DB schema ready →", sync.db_path)
PYEOF
ok "SQLite WAL enabled at ${DB_PATH}"

# ── 5. GPU check ──────────────────────────────────────────────────────────
hdr "[ 5/6 ] GPU"

if [[ "$SKIP_GPU" == "true" ]]; then
  warn "GPU offload skipped (--no-gpu)"
else
  # RTX (nvidia-smi)
  if command -v nvidia-smi &>/dev/null; then
    RTX_INFO=$(nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "")
    if [[ -n "$RTX_INFO" ]]; then
      ok "RTX detected: ${RTX_INFO}"
    else
      warn "nvidia-smi present but returned no data"
    fi
  else
    warn "nvidia-smi not found — RTX offload will use mock stats"
  fi

  # Arc B70 (Intel Xe sysfs)
  if ls /sys/class/drm/card*/device/vendor 2>/dev/null | xargs grep -l "0x8086" &>/dev/null; then
    ok "Intel Arc GPU detected"
  else
    warn "Intel Arc sysfs not found — Arc offload will use mock stats"
  fi
fi

# ── 6. Launch ─────────────────────────────────────────────────────────────
hdr "[ 6/6 ] Launch"

# Optional: background heartbeat daemon
if [[ "$SKIP_HEARTBEAT" == "false" ]]; then
  python3 - <<'PYEOF' &
import asyncio, sys
sys.path.insert(0, ".")
from core.heartbeat import HeartbeatEngine
engine = HeartbeatEngine()
engine.register_worker("server", lane=0x04)   # CPU lane
asyncio.run(engine.run())
PYEOF
  HB_PID=$!
  ok "Heartbeat engine started (PID ${HB_PID}, tick=140ms)"
else
  warn "Heartbeat skipped (--no-heartbeat)"
fi

echo ""
echo -e "  ${BLD}Arc is starting on http://0.0.0.0:${PORT}${RST}"
echo -e "  Chat UI  →  ${GRN}http://localhost:${PORT}/${RST}"
echo -e "  API      →  ${GRN}http://localhost:${PORT}/api/status${RST}"
echo -e "  MCP      →  ${GRN}http://localhost:${PORT}/mcp${RST}"
echo -e "  Logs     →  ${LOG_DIR}/arc.log"
echo ""

# Launch FastAPI server (foreground)
exec python3 -m uvicorn app.server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --log-level info \
  --log-config /dev/null \
  2>&1 | tee -a "${LOG_DIR}/arc.log"
