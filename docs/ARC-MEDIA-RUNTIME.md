# Arc local media and music runtime

Arc's interface (`127.0.0.1:7860`) embeds the separate opt-in monitor
(`127.0.0.1:11500/capture`). The monitor owns browser permission, transient
source media, acoustic/vision inference, MusicSympRev packet creation, and the
local music database. Arc owns the durable consent-scoped semantic journal and
exposes music retrieval to clients.

## YouTube path

1. Open Arc, choose the Audio + Video panel, and select **Browser tab / window +
   shared audio**.
2. Start capture. In the browser chooser select the YouTube tab and enable
   **Share tab audio**. This explicit browser gesture cannot be automated or
   resumed after login/reboot.
3. A four-second audio loop reports RMS, tempo/onsets, timbre, interval and
   arrangement cues. A three-second video loop records Garena-selected motion
   regions. Each loop waits for its preceding request to prevent backlog.
4. Music passages become `musicsymprev.gist.v1` packets. A background worker
   stores compact 256-dimensional Nomic vectors in
   `~/.local/state/arc/music-library.sqlite3`; raw source audio is not stored.
5. Stop capture. The derived, timestamped session is appended to Arc's semantic
   stream journal. A session is not proven to have reached Arc until this Stop
   response says `indexed` and returns a journal version.

Arc provides `POST /api/music/search`, `GET /api/music/recent`, and the
`arc_music_search` MCP tool. They proxy to the loopback monitor; the database is
not made directly readable by the Arc service. Search supports descriptions
such as tempo, rhythm, texture, broad instrument cues, arrangement changes, and
known source labels.

## Startup and recovery

Both `arc-interface.service` and `arc-monitor.service` are user services with
`Restart=on-failure` and `WantedBy=default.target`. Arc wants and starts after
the monitor. User lingering keeps the service manager available before an
interactive login, but browser media permission and the selected tab remain an
explicit per-session action.

```bash
systemctl --user daemon-reload
systemctl --user enable --now arc-monitor.service arc-interface.service
systemctl --user status arc-monitor.service arc-interface.service
journalctl --user -u arc-monitor.service -u arc-interface.service -n 100
```

## Latency and fidelity

The monitor exposes `/latency` and `/api/latency`. Per-stage summaries include
mean, p50, p95, max and population-standard-deviation jitter. The Arc interface
polls status every three seconds while visible, fifteen seconds while hidden,
and backs off to thirty seconds after failures. Music embeddings run outside
the audio request and remain retryable.

The fast acoustic path is intentionally a gist, not source separation or score
transcription. Song identity remains unknown unless the user provides visible
metadata or a future reviewed matcher provides provenance. See the Mr Calendar
`docs/MUSIC_MEMORY.md` for the packet schema, optional Essentia/CLAP enrichment,
and known half/double-tempo and short-window limits.

