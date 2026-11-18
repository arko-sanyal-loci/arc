#!/usr/bin/env bash
# =============================================================================
# scripts/arc_repair_and_update.sh  ·  arc-runtime · Billo Tech
# =============================================================================
# Fixes "dpkg returned an error code (1)" and brings drivers, firmware and
# runtimes current on the Arc workstation:
#   • NVIDIA RTX 5060 Ti  (Blackwell, sm_120)  → open kernel driver + CUDA 12.8+
#   • Intel Arc Pro B70   (BMG-G31, xe driver) → Level Zero, OpenCL, Mesa, xpu-smi
#
# Safe by default: with no flags it only DIAGNOSES and changes nothing.
#
#   bash scripts/arc_repair_and_update.sh              # diagnose only (no sudo needed for most checks)
#   sudo bash scripts/arc_repair_and_update.sh --repair  # fix the broken dpkg state
#   sudo bash scripts/arc_repair_and_update.sh --update  # upgrade OS, drivers, firmware, runtimes
#   sudo bash scripts/arc_repair_and_update.sh --all     # repair, then update
#   add --yes to accept every prompt (not recommended the first time)
#
# Every risky step asks first. A full log is written to ~/arc-logs/.
# Supersedes the package section of scripts/install_drivers.sh, which installs
# CUDA 12.4 (no Blackwell support) and conflicting Intel Level Zero packages.
# =============================================================================

set -uo pipefail

MODE_REPAIR=0; MODE_UPDATE=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --repair) MODE_REPAIR=1 ;;
    --update) MODE_UPDATE=1 ;;
    --all)    MODE_REPAIR=1; MODE_UPDATE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n 2,22p "$0"; exit 0 ;;
    *) echo "unknown option: $a (try --help)"; exit 2 ;;
  esac
done

REAL_USER="${SUDO_USER:-${USER:-$(id -un)}}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6); REAL_HOME="${REAL_HOME:-$HOME}"
TS=$(date +%Y%m%d-%H%M%S)
LOG_DIR="$REAL_HOME/arc-logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/repair-$TS.log"
exec > >(tee -a "$LOG") 2>&1

C_B='\033[1;34m'; C_G='\033[1;32m'; C_Y='\033[1;33m'; C_R='\033[1;31m'; C_D='\033[2m'; C_0='\033[0m'
step() { echo -e "\n${C_B}━━ $* ━━${C_0}"; }
ok()   { echo -e "  ${C_G}✔${C_0} $*"; }
note() { echo -e "  ${C_Y}•${C_0} $*"; }
bad()  { echo -e "  ${C_R}✘${C_0} $*"; FINDINGS+=("$*"); }
dim()  { echo -e "  ${C_D}$*${C_0}"; }
FINDINGS=()

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local ans; read -r -p "  → $1 [y/N] " ans </dev/tty || return 1
  [[ "$ans" =~ ^[Yy]$ ]]
}
need_root() {
  if [[ $EUID -ne 0 ]]; then echo -e "${C_R}This mode needs root: sudo bash $0 $*${C_0}"; exit 1; fi
}
have() { command -v "$1" >/dev/null 2>&1; }
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
pkg_available() { apt-cache show "$1" >/dev/null 2>&1; }
apt_q() { DEBIAN_FRONTEND=noninteractive apt-get -o Dpkg::Options::=--force-confold "$@"; }

# WSL2: GPU drivers come from Windows. Linux kernel GPU drivers, DKMS,
# headers and firmware must NOT be installed inside WSL (a DKMS build against
# the WSL kernel fails and leaves dpkg with "error code (1)").
IS_WSL=0
if [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null; then IS_WSL=1; fi

echo -e "${C_B}Arc workstation repair & update${C_0}  ·  $(date)  ·  log: $LOG"

# ─────────────────────────────────────────────────────────────────────────────
# 1. DIAGNOSE (always runs, read-only)
# ─────────────────────────────────────────────────────────────────────────────
diagnose() {
  step "System"
  . /etc/os-release 2>/dev/null || true
  ok "${PRETTY_NAME:-unknown OS}  ·  kernel $(uname -r)  ·  $(uname -m)"
  UBU_VER="${VERSION_ID:-0}"; UBU_CODENAME="${VERSION_CODENAME:-unknown}"
  if [[ $IS_WSL -eq 1 ]]; then
    note "Running inside WSL2: GPU drivers are the Windows ones; this script only manages user-space runtimes here"
    local wsl_bad; wsl_bad=$(dpkg -l 2>/dev/null | awk '$1 ~ /^(ii|iU|iF|iH)$/ && $2 ~ /^(nvidia-driver-|nvidia-dkms-|nvidia-kernel-|cuda-drivers|linux-modules-nvidia)/ {print $2}')
    [[ -n "$wsl_bad" ]] && bad "Linux NVIDIA driver packages are installed inside WSL (they can't build here and break dpkg): $(echo $wsl_bad)"
  fi

  step "Disk space (a full /boot is the #1 cause of dpkg error 1 during kernel upgrades)"
  for m in / /boot /var; do
    [[ -d $m ]] || continue
    local pct; pct=$(df -P "$m" | awk 'NR==2{gsub("%","",$5); print $5}')
    local av;  av=$(df -hP "$m" | awk 'NR==2{print $4}')
    if (( pct >= 90 )); then bad "$m is ${pct}% full (${av} free)"; else ok "$m ${pct}% used (${av} free)"; fi
  done

  step "Package manager locks"
  if have fuser && fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
    bad "another apt/dpkg process holds the lock (unattended-upgrades or Software Updater?)"
  else ok "no lock held"; fi

  step "Broken or half-installed packages"
  local audit; audit=$(dpkg --audit 2>/dev/null)
  local broken; broken=$(dpkg -l 2>/dev/null | awk 'NR>5 && $1 !~ /^(ii|rc|un)$/ {print $1, $2, $3}')
  if [[ -z "$audit" && -z "$broken" ]]; then ok "dpkg database is consistent"
  else
    bad "dpkg reports packages in a broken state:"
    [[ -n "$broken" ]] && echo "$broken" | sed 's/^/      /'
  fi

  step "Last dpkg/apt errors"
  if [[ -r /var/log/apt/term.log ]]; then
    local errs; errs=$(grep -E "^(dpkg: error|Errors were encountered|E: |  [a-z0-9].*: (trying to overwrite|dependency problems))|trying to overwrite|returned error exit status|Bad return status|dkms.*(failed|Error)" /var/log/apt/term.log | tail -n 25)
    if [[ -n "$errs" ]]; then echo "$errs" | sed 's/^/      /'; else ok "no errors in the current term.log"; fi
    OVERWRITE_LINES=$(grep -E "trying to overwrite" /var/log/apt/term.log | tail -n 5)
  else note "cannot read /var/log/apt/term.log (run with sudo for full detail)"; OVERWRITE_LINES=""; fi

  step "DKMS (kernel module builds: NVIDIA and others)"
  if have dkms; then
    dkms status 2>/dev/null | sed 's/^/      /'
    local mk; mk=$(ls -t /var/lib/dkms/*/*/build/make.log 2>/dev/null | head -1)
    if [[ -n "$mk" ]] && grep -qiE "error" "$mk"; then
      bad "latest DKMS build log has errors: $mk"; grep -iE "error" "$mk" | tail -n 5 | sed 's/^/      /'
    fi
    if [[ $IS_WSL -eq 0 ]] && ! dpkg-query -W "linux-headers-$(uname -r)" >/dev/null 2>&1; then
      bad "headers for the running kernel are missing (linux-headers-$(uname -r)); DKMS cannot build"
    fi
  else dim "dkms not installed"; fi

  step "Package sources that commonly conflict"
  INTEL_LEGACY=0; CUDA_LOCAL=0
  if grep -rqs "repositories.intel.com/gpu" /etc/apt/sources.list /etc/apt/sources.list.d/; then
    INTEL_LEGACY=1; note "legacy Intel GPU repo (repositories.intel.com/gpu) is enabled"
  fi
  if grep -rqs "kobuk-team" /etc/apt/sources.list.d/; then ok "Intel graphics PPA (kobuk-team) is enabled"; fi
  if pkg_installed intel-level-zero-gpu && { pkg_available libze-intel-gpu1 || pkg_installed libze-intel-gpu1; }; then
    bad "intel-level-zero-gpu (old name) is installed while libze-intel-gpu1 (new name) is available: they ship the same files"
  fi
  if ls /var/cuda-repo-* >/dev/null 2>&1 || grep -rqs "file:/var/cuda-repo" /etc/apt/sources.list.d/; then
    CUDA_LOCAL=1; note "a CUDA *local* installer repo is present (it pins an old bundled driver)"
  fi
  if pkg_installed cuda-toolkit-12-4; then
    bad "CUDA 12.4 is installed: it does not support Blackwell (RTX 5060 Ti needs CUDA 12.8 or newer)"
  fi
  local held; held=$(apt-mark showhold 2>/dev/null)
  [[ -n "$held" ]] && note "held packages: $(echo "$held" | tr '\n' ' ')"

  step "GPUs"
  if have lspci; then lspci -nn | grep -Ei "vga|3d|display" | sed 's/^/      /'; fi
  if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
    ok "NVIDIA: $(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | head -1)"
    if [[ $IS_WSL -eq 0 ]] && ! modinfo nvidia 2>/dev/null | grep -qi "license:.*Dual MIT/GPL"; then
      note "NVIDIA kernel module may not be the open variant (Blackwell requires nvidia-driver-XXX-open)"
    fi
  else bad "NVIDIA driver not loaded (nvidia-smi fails)"; fi
  if [[ $IS_WSL -eq 1 ]]; then
    [[ -e /dev/dxg ]] && ok "WSL GPU bridge /dev/dxg present (B70 and RTX are exposed through the Windows drivers)" \
                      || bad "/dev/dxg missing: update the Windows GPU drivers and run 'wsl --update' in Windows"
  elif [[ -d /sys/module/xe ]]; then ok "Intel xe driver is loaded"
  else bad "Intel xe driver is not loaded (the B70 needs xe; kernel 6.17+ recommended, 7.0 is known good)"; fi
  if have xpu-smi; then ok "xpu-smi present"; else note "xpu-smi not installed (needed for live B70 VRAM telemetry)"; fi
  if have clinfo; then clinfo -l 2>/dev/null | sed 's/^/      /'; fi
  local bar; bar=$(lspci -vv -d 8086:e223 2>/dev/null | grep -E "Region 2|BAR 2|Resizable" | head -3)
  [[ -n "$bar" ]] && dim "B70 BAR: $bar"
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. REPAIR the dpkg state
# ─────────────────────────────────────────────────────────────────────────────
repair() {
  need_root --repair
  step "Backing up apt sources to $REAL_HOME/arc-backups/apt-$TS"
  mkdir -p "$REAL_HOME/arc-backups/apt-$TS"
  cp -a /etc/apt/sources.list* /etc/apt/preferences.d "$REAL_HOME/arc-backups/apt-$TS/" 2>/dev/null
  dpkg --get-selections > "$REAL_HOME/arc-backups/apt-$TS/selections.txt"
  chown -R "$REAL_USER": "$REAL_HOME/arc-backups" 2>/dev/null
  ok "backup written (restore with: sudo cp -a <backup>/sources.list.d /etc/apt/)"

  # /boot full → remove old kernels
  local bootpct; bootpct=$(df -P /boot | awk 'NR==2{gsub("%","",$5); print $5}')
  if (( bootpct >= 90 )); then
    step "/boot is ${bootpct}% full"
    dpkg -l 'linux-image-*' | awk '/^ii/{print "      "$2}'
    note "running kernel: $(uname -r) (never removed)"
    confirm "Remove old kernels with 'apt autoremove --purge'?" && apt_q autoremove --purge -y
  fi

  # Linux NVIDIA driver inside WSL → purge (Windows driver provides the GPU)
  if [[ $IS_WSL -eq 1 ]]; then
    local wsl_bad; wsl_bad=$(dpkg -l 2>/dev/null | awk '$1 !~ /^(rc|un)$/ && $2 ~ /^(nvidia-driver-|nvidia-dkms-|nvidia-kernel-|cuda-drivers|linux-modules-nvidia)/ {print $2}')
    if [[ -n "$wsl_bad" ]]; then
      step "Linux NVIDIA driver packages inside WSL"
      echo "$wsl_bad" | sed 's/^/      /'
      confirm "Purge these (the Windows NVIDIA driver keeps working; CUDA in WSL uses it)?" && \
        { dpkg --purge --force-depends $wsl_bad; apt_q -f install -y; }
    fi
  fi

  # Missing headers → DKMS can't build
  if [[ $IS_WSL -eq 0 ]] && ! dpkg-query -W "linux-headers-$(uname -r)" >/dev/null 2>&1; then
    step "Installing headers for the running kernel"
    apt_q install -y "linux-headers-$(uname -r)" || bad "could not install headers"
  fi

  # Old/new Intel Level Zero package clash
  if pkg_installed intel-level-zero-gpu && pkg_available libze-intel-gpu1; then
    step "Intel Level Zero package rename clash"
    note "intel-level-zero-gpu was renamed libze-intel-gpu1; having both causes 'trying to overwrite' errors"
    confirm "Remove intel-level-zero-gpu (libze-intel-gpu1 replaces it)?" && \
      dpkg --remove --force-depends intel-level-zero-gpu
  fi

  # Legacy Intel repo mixed with Ubuntu/PPA packages
  if [[ ${INTEL_LEGACY:-0} -eq 1 ]]; then
    step "Legacy Intel GPU repository"
    note "Mixing repositories.intel.com/gpu with Ubuntu or PPA Intel packages causes file conflicts."
    if confirm "Disable the legacy Intel repo (moved to the backup, not deleted)?"; then
      for f in $(grep -rls "repositories.intel.com/gpu" /etc/apt/sources.list.d/); do mv "$f" "$REAL_HOME/arc-backups/apt-$TS/disabled-$(basename "$f")"; done
      ok "legacy Intel repo disabled"
    fi
  fi

  # CUDA local repo
  if [[ ${CUDA_LOCAL:-0} -eq 1 ]]; then
    step "CUDA local installer repo"
    if confirm "Disable the CUDA local repo (it pins an old driver; we use NVIDIA's network repo instead)?"; then
      for f in $(grep -rls "file:/var/cuda-repo" /etc/apt/sources.list.d/); do mv "$f" "$REAL_HOME/arc-backups/apt-$TS/disabled-$(basename "$f")"; done
      ok "CUDA local repo disabled"
    fi
  fi

  step "Finishing interrupted package configuration"
  dpkg --configure -a && ok "dpkg --configure -a succeeded" || note "dpkg --configure -a still failing, continuing"

  # File-overwrite conflicts: show and offer the targeted override
  if [[ -n "${OVERWRITE_LINES:-}" ]]; then
    step "File overwrite conflicts seen in the last run"
    echo "$OVERWRITE_LINES" | sed 's/^/      /'
    local debs; debs=$(grep -B2 "trying to overwrite" /var/log/apt/term.log | grep -oE "/var/cache/apt/archives/[^ ]+\.deb" | sort -u)
    for d in $debs; do
      [[ -f $d ]] || continue
      confirm "Force-install $(basename "$d") over the conflicting file?" && dpkg -i --force-overwrite "$d"
    done
  fi

  step "Fixing dependencies"
  apt_q update
  apt_q -f install -y && ok "apt -f install succeeded" || bad "apt -f install failed: see $LOG"
  dpkg --configure -a

  if [[ -z "$(dpkg -l | awk 'NR>5 && $1 !~ /^(ii|rc|un)$/')" ]]; then ok "dpkg is healthy again"
  else bad "some packages are still broken:"; dpkg -l | awk 'NR>5 && $1 !~ /^(ii|rc|un)$/ {print "      "$1, $2}'; fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. UPDATE OS, drivers, firmware, runtimes
# ─────────────────────────────────────────────────────────────────────────────
update() {
  need_root --update
  . /etc/os-release
  local ver_major="${VERSION_ID%%.*}"

  step "OS packages"
  apt_q update && apt_q full-upgrade -y || { bad "full-upgrade failed. Run --repair first."; return; }
  if [[ $IS_WSL -eq 1 ]]; then
    apt_q install -y --no-install-recommends build-essential curl wget gpg ca-certificates \
      software-properties-common pciutils clinfo
  else
    apt_q install -y --no-install-recommends build-essential dkms "linux-headers-$(uname -r)" \
      curl wget gpg ca-certificates software-properties-common pciutils clinfo ubuntu-drivers-common
  fi

  step "NVIDIA RTX 5060 Ti: open kernel driver"
  if [[ $IS_WSL -eq 1 ]]; then
    note "WSL: skipped. Update the NVIDIA driver in Windows (NVIDIA app or nvidia.com) instead."
  else
  local rec; rec=$(ubuntu-drivers list 2>/dev/null | grep -oE "nvidia-driver-[0-9]+-open" | sort -t- -k3 -n | tail -1)
  if [[ -n "$rec" ]]; then
    local cur; cur=$(dpkg -l 'nvidia-driver-*' 2>/dev/null | awk '/^ii/{print $2}' | head -1)
    note "installed: ${cur:-none}   newest available open driver: $rec"
    if [[ "$cur" != "$rec" ]] && confirm "Install $rec?"; then apt_q install -y "$rec" && ok "$rec installed"; fi
  else note "ubuntu-drivers found no open driver; check 'ubuntu-drivers list'"; fi
  fi

  step "CUDA toolkit (Blackwell needs 12.8+)"
  if ! pkg_installed cuda-keyring; then
    local repo="ubuntu${VERSION_ID//./}"
    [[ $IS_WSL -eq 1 ]] && repo="wsl-ubuntu"     # NVIDIA's WSL repo ships no driver
    local url="https://developer.download.nvidia.com/compute/cuda/repos/${repo}/x86_64/cuda-keyring_1.1-1_all.deb"
    if confirm "Add NVIDIA's CUDA network repo for $repo?"; then
      local tmp; tmp=$(mktemp -d)
      if wget -q -O "$tmp/k.deb" "$url"; then dpkg -i "$tmp/k.deb" && apt_q update
      else note "no CUDA repo for $repo yet; the Ubuntu archive 'nvidia-cuda-toolkit' is used instead"; fi
      rm -rf "$tmp"
    fi
  fi
  if apt-cache show cuda-toolkit >/dev/null 2>&1; then
    confirm "Install/upgrade cuda-toolkit (latest, keeps the driver from step above)?" && apt_q install -y cuda-toolkit
  elif pkg_available nvidia-cuda-toolkit; then
    confirm "Install nvidia-cuda-toolkit from the Ubuntu archive?" && apt_q install -y nvidia-cuda-toolkit
  fi
  if pkg_installed cuda-toolkit-12-4 && confirm "Remove the old CUDA 12.4 toolkit?"; then apt_q purge -y cuda-toolkit-12-4; fi

  step "Intel Arc Pro B70: compute + graphics runtime"
  if (( ver_major < 26 )) && ! grep -rqs kobuk-team /etc/apt/sources.list.d/; then
    note "Ubuntu $VERSION_ID: newer Intel runtimes come from ppa:kobuk-team/intel-graphics"
    confirm "Add ppa:kobuk-team/intel-graphics?" && add-apt-repository -y ppa:kobuk-team/intel-graphics && apt_q update
  fi
  local want=(libze-intel-gpu1 libze1 libze-dev intel-opencl-icd ocl-icd-libopencl1 clinfo
              intel-media-va-driver-non-free vainfo mesa-vulkan-drivers vulkan-tools
              intel-gpu-tools nvtop xpu-smi)
  [[ $IS_WSL -eq 0 ]] && want+=(linux-firmware)
  local list=(); for p in "${want[@]}"; do pkg_available "$p" && list+=("$p") || dim "not in your repos: $p"; done
  apt_q install -y "${list[@]}" && ok "Intel runtime packages current"
  have xpu-smi || note "xpu-smi not in apt: get the .deb from github.com/intel/xpumanager releases"
  getent group render >/dev/null && usermod -aG render,video "$REAL_USER" && ok "$REAL_USER is in render,video groups"

  step "Firmware"
  if [[ $IS_WSL -eq 1 ]]; then
    note "WSL: firmware is managed by Windows (Windows Update, Sparkle/Intel tools, motherboard vendor)."
  elif have fwupdmgr; then
    fwupdmgr refresh --force >/dev/null 2>&1
    fwupdmgr get-updates 2>/dev/null | sed 's/^/      /'
    confirm "Apply the firmware updates listed above (fwupd)?" && fwupdmgr update -y
  else apt_q install -y fwupd && note "fwupd installed; rerun --update to check firmware"; fi
  note "B70 card firmware (GSC) is updated with Intel's igsc tool, not fwupd. It is not flashed automatically."
  note "Check the version with: sudo igsc fw version --device /dev/mei*  (only flash a Sparkle/Intel-supplied image)"

  step "Inference runtimes"
  if have ollama; then
    note "Ollama $(ollama --version 2>/dev/null | awk '{print $NF}') installed"
    confirm "Update Ollama with its official installer (downloads and runs ollama.com/install.sh)?" && \
      curl -fsSL https://ollama.com/install.sh | sh
  fi
  if have docker; then
    confirm "Pull the latest intel/llm-scaler-vllm image for the B70?" && docker pull intel/llm-scaler-vllm:latest
  fi

  step "Clean up"
  apt_q autoremove -y
  grep -c "setvars.sh" /etc/profile.d/oneapi.sh 2>/dev/null | awk '$1>1{print "  • /etc/profile.d/oneapi.sh has duplicate lines (from install_drivers.sh)"}'
}

diagnose
[[ $MODE_REPAIR -eq 1 ]] && repair
[[ $MODE_UPDATE -eq 1 ]] && update

step "Summary"
if [[ $MODE_REPAIR -eq 0 && $MODE_UPDATE -eq 0 ]]; then
  note "Diagnose only. Nothing was changed."
  note "Next: sudo bash $0 --repair   then   sudo bash $0 --update"
fi
if (( ${#FINDINGS[@]} )); then
  echo -e "  ${C_Y}Findings:${C_0}"; for f in "${FINDINGS[@]}"; do echo "    - $f"; done
else ok "no problems found"; fi
[[ $MODE_UPDATE -eq 1 ]] && echo -e "\n  ${C_Y}Reboot to load the new kernel modules and firmware, then run: arc-verify-gpus${C_0}"
echo -e "  ${C_D}full log: $LOG${C_0}"
