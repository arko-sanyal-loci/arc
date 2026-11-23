#!/usr/bin/env bash
# =============================================================================
# arc_loci_env.sh — Source this before running any LOCI inference on Arc B70
# Usage: source scripts/arc_loci_env.sh
# =============================================================================

# oneAPI environment (OpenCL + Level Zero + SYCL)
ONEAPI_SETVARS=/opt/intel/oneapi/setvars.sh
if [[ -f "$ONEAPI_SETVARS" ]]; then
    # shellcheck disable=SC1090
    source "$ONEAPI_SETVARS" --force 2>/dev/null
    echo "[arc_env] oneAPI activated"
else
    echo "[arc_env] WARN: oneAPI not found at $ONEAPI_SETVARS — using system OpenCL"
fi

# Force iHD VA driver (Xe2 media)
export LIBVA_DRIVER_NAME=iHD

# GPU device selection for Level Zero (device 0 = Arc B70)
export ONEAPI_DEVICE_SELECTOR="level_zero:0"
export ZES_ENABLE_SYSMAN=1          # Level Zero System Management (temp/power monitoring)
export ZE_ENABLE_ALT_DRIVERS=1      # pick up non-default L0 ICD
export OCL_ICD_VENDORS=/etc/OpenCL/vendors  # explicit ICD path

# IPEX-LM / bigDL (if installed)
export BIGDL_USE_XE2=1
export IPEX_TILE_ASSIGN=0           # use all Xe-cores, don't restrict to one tile

# PyTorch XPU backend
export PYTORCH_XPU_DEVICE_INDEX=0

# LOCI-specific: deep lane = Arc (device 0), fast lane = RTX (device 1)
export LOCI_GPU_DEEP_LANE_DEVICE=0   # Arc B70  — 32 GB weights lane
export LOCI_GPU_FAST_LANE_DEVICE=1   # RTX 5060 Ti — inference burst lane

# Shared memory staging dir (cross-GPU ring buffer)
export LOCI_SHM_DIR=${LOCI_SHM_DIR:-/dev/shm/loci_pipeline}
mkdir -p "$LOCI_SHM_DIR"

# Inference server base URLs (must match speculative_pipeline.py defaults)
export LOCI_LOCAL_INTEL_BASE_URL=${LOCI_LOCAL_INTEL_BASE_URL:-http://localhost:8000}
export LOCI_LOCAL_BASE_URL=${LOCI_LOCAL_BASE_URL:-http://localhost:11434}

echo "[arc_env] Environment set:"
echo "  LIBVA_DRIVER_NAME     = $LIBVA_DRIVER_NAME"
echo "  ONEAPI_DEVICE_SELECTOR= $ONEAPI_DEVICE_SELECTOR"
echo "  LOCI_GPU_DEEP_LANE    = $LOCI_GPU_DEEP_LANE_DEVICE (Arc B70)"
echo "  LOCI_GPU_FAST_LANE    = $LOCI_GPU_FAST_LANE_DEVICE (RTX 5060 Ti)"
echo "  LOCI_SHM_DIR          = $LOCI_SHM_DIR"
echo ""
echo "Quick sanity checks:"
echo "  OpenCL:   clinfo -l"
echo "  VA-API:   vainfo"
echo "  Level0:   ze_info (from level-zero-dev)"
echo "  GPU temp: intel_gpu_top"
