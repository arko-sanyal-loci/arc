#!/usr/bin/env bash
# Publish the existing loopback text router to one SSH identity's private VPS socket.
set -euo pipefail

target=${1:-vps-billo}
[[ $target =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { printf 'Use a configured SSH alias.\n' >&2; exit 2; }
options=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8)
socket=$(ssh "${options[@]}" "$target" '
    set -eu
    umask 077
    dir="$HOME/.local/state/mr-calendar-remote"
    mkdir -p "$dir"
    test ! -L "$dir" && test -O "$dir" && test "$(stat -c %a "$dir")" = 700
    printf "%s/router.sock\n" "$dir"
')
[[ $socket =~ ^/[a-zA-Z0-9_./-]+/router\.sock$ && ${#socket} -lt 108 ]] ||
    { printf 'Invalid or overlong private socket path.\n' >&2; exit 2; }
printf 'Forwarding to an owner-only directory on %s.\n' "$target" >&2
exec ssh "${options[@]}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=25 \
    -o ServerAliveCountMax=3 -N -T -R "$socket:127.0.0.1:11437" "$target"
