#!/usr/bin/env bash
# =============================================================================
# scripts/setup_terminal.sh  ·  arc-runtime · Billo Tech
# =============================================================================
# A friendly terminal for the Arc workstation. Keeps bash as your shell and
# adds, with no changes to anything system-wide except apt packages:
#
#   • Starship prompt with a LOCI theme (git branch, venv, exit status, time)
#   • fzf (Ctrl-R fuzzy history, Ctrl-T file picker), zoxide ("z proj" jumps)
#   • eza (ls with icons/colours), bat (cat with syntax colour), btop, nvtop
#   • `arc` welcome card on new terminals: both GPUs, VRAM, temps, services
#   • Optional: WezTerm terminal app with a matching colour scheme (--wezterm)
#
#   bash scripts/setup_terminal.sh            # install for the current user
#   bash scripts/setup_terminal.sh --wezterm  # also install WezTerm
#   bash scripts/setup_terminal.sh --undo     # remove the hook from ~/.bashrc
#
# Run it as yourself, not with sudo; it asks for sudo only for apt.
# Packages that aren't in your repos are skipped, never forced, so this
# cannot create a new dpkg error. Run arc_repair_and_update.sh --repair first
# if apt is currently broken.
# =============================================================================
set -uo pipefail

WEZ=0; UNDO=0
for a in "$@"; do case "$a" in --wezterm) WEZ=1 ;; --undo) UNDO=1 ;; -h|--help) sed -n 2,22p "$0"; exit 0 ;; esac; done

CFG="$HOME/.config/arc"; MARK="# >>> arc terminal >>>"; END="# <<< arc terminal <<<"
G='\033[1;32m'; Y='\033[1;33m'; B='\033[1;34m'; N='\033[0m'
say() { echo -e "${B}▸${N} $*"; }; ok() { echo -e "  ${G}✔${N} $*"; }; skip() { echo -e "  ${Y}•${N} $*"; }

if [[ $UNDO -eq 1 ]]; then
  sed -i "/$MARK/,/$END/d" "$HOME/.bashrc" && ok "hook removed from ~/.bashrc (config kept in $CFG)"; exit 0
fi
[[ $EUID -eq 0 ]] && { echo "Run as your normal user, not root."; exit 1; }

if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || [[ -n "$(dpkg -l | awk 'NR>5 && $1 !~ /^(ii|rc|un)$/')" ]]; then
  echo -e "${Y}apt is busy or has broken packages. Run: sudo bash scripts/arc_repair_and_update.sh --repair${N}"; exit 1
fi

say "Installing tools (skipping any your Ubuntu release doesn't carry)"
sudo apt-get update -qq
PKGS=(fzf zoxide eza bat btop nvtop fastfetch git curl fonts-firacode starship)
OKP=(); for p in "${PKGS[@]}"; do apt-cache show "$p" >/dev/null 2>&1 && OKP+=("$p") || skip "not available: $p"; done
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${OKP[@]}" && ok "${OKP[*]}"

if ! command -v starship >/dev/null; then
  say "Starship isn't in apt here; installing the official release to ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://starship.rs/install.sh | sh -s -- -y -b "$HOME/.local/bin" >/dev/null && ok "starship installed"
fi

say "Writing config to $CFG"
mkdir -p "$CFG" "$HOME/.config"

cat > "$HOME/.config/starship.toml" <<'EOF'
# Arc · LOCI prompt
add_newline = true
format = "$username$hostname$directory$git_branch$git_status$python$cmd_duration$line_break$status$character"
[character]
success_symbol = "[❯](bold #7c7cff)"
error_symbol   = "[❯](bold #ff6b6b)"
[directory]
style = "bold #8be9fd"
truncation_length = 4
[git_branch]
symbol = " "
style = "#c3a6ff"
[git_status]
style = "#ffb86c"
[python]
symbol = "🐍 "
format = "[$symbol($virtualenv )]($style)"
[cmd_duration]
min_time = 2000
format = "[ $duration](#6c7086) "
[status]
disabled = false
format = "[✘ $status](#ff6b6b) "
[hostname]
ssh_only = true
format = "[@$hostname](#7c7cff) "
[username]
show_always = false
EOF

cat > "$CFG/arc-status.sh" <<'EOF'
#!/usr/bin/env bash
# Arc status card. Unknown values are shown as "unknown", never guessed.
P='\033[38;5;105m'; C='\033[38;5;117m'; D='\033[2m'; G='\033[32m'; R='\033[31m'; N='\033[0m'
echo -e "${P}   ▄▄▄    ██████  ██████${N}   ${D}Arc · LOCI runtime · Billo Tech${N}"
echo -e "${P}  ██▄██   ██▄▄▀   ██    ${N}   ${D}$(date '+%a %d %b %H:%M')  ·  up $(uptime -p | sed 's/up //')${N}"
echo -e "${P}  ██  ██  ██  ██  ██████${N}"
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  IFS=, read -r n u t tp <<<"$(nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits | head -1)"
  echo -e "  ${C}Y0${N} ${n# }  ${u# }/${t# } MiB  ${tp# }°C"
else echo -e "  ${C}Y0${N} RTX 5060 Ti  ${R}driver not loaded${N}"; fi
if command -v xpu-smi >/dev/null; then
  m=$(xpu-smi stats -d 0 2>/dev/null | awk -F'|' '/GPU Memory Used/{gsub(/ /,"",$3); print $3}')
  tp=$(xpu-smi stats -d 0 2>/dev/null | awk -F'|' '/GPU Core Temperature/{gsub(/ /,"",$3); print $3}')
  echo -e "  ${C}Y1${N} Arc Pro B70  ${m:-unknown}/32768 MiB  ${tp:-unknown}°C"
elif [[ -d /sys/module/xe ]]; then echo -e "  ${C}Y1${N} Arc Pro B70  xe loaded  ${D}VRAM unknown (install xpu-smi)${N}"
else echo -e "  ${C}Y1${N} Arc Pro B70  ${R}xe driver not loaded${N}"; fi
svc() { if (exec 3<>/dev/tcp/127.0.0.1/$2) 2>/dev/null; then echo -ne "${G}●${N} $1  "; else echo -ne "${D}○ $1${N}  "; fi; }
echo -ne "  "; svc arc 7860; svc ollama 11434; svc vllm 8000; svc sglang 7000; echo
EOF
chmod +x "$CFG/arc-status.sh"

cat > "$CFG/bashrc.sh" <<'EOF'
# Arc friendly terminal: sourced from ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
HISTSIZE=50000; HISTFILESIZE=100000; HISTCONTROL=ignoreboth:erasedups; shopt -s histappend
command -v starship >/dev/null && eval "$(starship init bash)"
command -v zoxide   >/dev/null && eval "$(zoxide init bash)"
[[ -f /usr/share/doc/fzf/examples/key-bindings.bash ]] && . /usr/share/doc/fzf/examples/key-bindings.bash
command -v fzf >/dev/null && fzf --bash >/dev/null 2>&1 && eval "$(fzf --bash)"
if command -v eza >/dev/null; then alias ls='eza --group-directories-first --icons=auto'; alias ll='ls -lah --git'; alias tree='eza --tree --level=2'; else alias ll='ls -lah'; fi
command -v batcat >/dev/null && alias bat='batcat'
alias gpus='watch -n1 "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv; xpu-smi stats -d 0 2>/dev/null"'
alias arc='~/.config/arc/arc-status.sh'
alias fixapt='sudo bash ~/arc/scripts/arc_repair_and_update.sh --repair'
alias gs='git status -sb'; alias gl='git log --oneline --graph -15'
# Welcome card once per interactive terminal (not in scripts, not in tmux splits)
[[ $- == *i* && -z "${ARC_WELCOMED:-}" ]] && export ARC_WELCOMED=1 && ~/.config/arc/arc-status.sh
EOF

if ! grep -q "$MARK" "$HOME/.bashrc" 2>/dev/null; then
  printf '\n%s\n[ -f "$HOME/.config/arc/bashrc.sh" ] && . "$HOME/.config/arc/bashrc.sh"\n%s\n' "$MARK" "$END" >> "$HOME/.bashrc"
  ok "hooked into ~/.bashrc (undo with --undo)"
else ok "~/.bashrc already hooked"; fi

if [[ $WEZ -eq 1 ]]; then
  say "Installing WezTerm from its official apt repo"
  curl -fsSL https://apt.fury.io/wez/gpg.key | sudo gpg --yes --dearmor -o /usr/share/keyrings/wezterm-fury.gpg
  echo 'deb [signed-by=/usr/share/keyrings/wezterm-fury.gpg] https://apt.fury.io/wez/ * *' | sudo tee /etc/apt/sources.list.d/wezterm.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq wezterm && ok "wezterm installed"
  cat > "$HOME/.wezterm.lua" <<'EOF'
local wezterm = require 'wezterm'
local c = wezterm.config_builder()
c.color_scheme = 'Catppuccin Mocha'
c.font = wezterm.font_with_fallback { 'Fira Code', 'JetBrains Mono' }
c.font_size = 12.5
c.window_background_opacity = 0.96
c.hide_tab_bar_if_only_one_tab = true
c.window_padding = { left = 10, right = 10, top = 8, bottom = 6 }
c.default_cursor_style = 'BlinkingBar'
return c
EOF
  ok "~/.wezterm.lua written"
fi

echo; ok "Done. Open a new terminal (or run: source ~/.bashrc). Type 'arc' any time for the status card."
