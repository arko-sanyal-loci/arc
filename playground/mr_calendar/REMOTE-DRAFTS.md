# Remote drafting for an approved VPS identity

The existing workstation GPUs continue to run the model. An SSH reverse forward gives one dedicated VPS Unix identity a local socket for text requests. There is no public inference port, additional hosted model, or OAuth service. The VPS connection does not get access to the workspace recall server, its files, or its scratchpad.

Configure a verified SSH alias for the selected VPS Unix identity. Use a dedicated account and key for each independently authorized agent. The VPS must allow remote Unix socket forwarding. Keep the socket under the account's owner-only state directory. If a permanent forward is desired, run the command below as the workstation user through its userspace service manager with restart on failure.

```bash
bash playground/mr_calendar/vps-draft-tunnel.sh vps-billo
```

From the selected VPS account, verify that the socket exists, is owned by the account, and sits inside the private directory. Then make a local request there.

```bash
socket="$HOME/.local/state/mr-calendar-remote/router.sock"
test -S "$socket" && test -O "$socket"
curl -fsS --unix-socket "$socket" http://localhost/health
curl -fsS --unix-socket "$socket" http://localhost/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"stream":false,"messages":[{"role":"user","content":"Reply with READY."}]}'
```

Only processes running as the authorized VPS account should have access to the directory. Keep any SSH restrictions and file permissions in place when adding agents. A stale socket may need removal by the owner after checking that another forwarding session is not using it. When the workstation loses power, this remote path becomes unavailable.

The deployed Hostinger path was not verified. The SSH alias timed out before authentication, so the wrapper could not create the remote socket.

## Shared scheduling

Every approved agent can poll `/load` through its own VPS socket. The scheduler reports each lane's active flag, waiting count, and time in service. The documented limit is one active request and three waiting requests per lane. A saturated request can use another available lane or receive HTTP 429. Respect the server's retry delay. The figures cover requests through the gateway. Direct Ollama requests do not count.

For deferrable work, send `X-LOCI-Priority: idle` to `/api/chat`. Idle traffic is admitted only when both gateway lanes have no active or waiting requests. Idle output has a 256-token generation budget. The scheduler does not preempt an admitted job. Clients retain and retry their own deferred work. Keep direct GPU endpoints for operator diagnostics so the shared queue can account for production work.

A workstation-only SSH smoke test created a mode-0600 forwarded socket and successfully returned `/load` through it. A separate live scheduler test received HTTP 429 for idle work while a foreground request was active, then accepted idle work after the lane released. No VPS user or forwarded socket was created because the VPS connection timed out.
