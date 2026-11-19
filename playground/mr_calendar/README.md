# Mr Calendar live scene playground

Arko Sanyal proposed motion-driven focus and varying packet fidelity by feature family. Codex implemented the capture experiment and the local tool interface.

## Capture

Use Firefox with geckodriver, a Python environment containing NumPy and Pillow, and the existing Qwen3-VL 8B on local Ollama port 11435. In a terminal, run `geckodriver --host 127.0.0.1 --port 4447`. From the Arc checkout, use the video ID after `--` because it begins with a hyphen.

```bash
python playground/mr_calendar/live.py --start 90 --frames 32 --ask -- -nxb3VCH7Vw
python playground/mr_calendar/live.py --frames 35 --ask fixture
```

The capture asks an isolated Firefox session for screenshots of the video element. Timestamps are bracketed by two browser player-clock reads. Packets contain a 4 × 3 motion heat map, focus region and region color. The current frame must have texture in a tile to earn a high-resolution crop. A large global frame change returns focus to the whole scene. Setting `--ask` compares the same instant as a whole low-resolution view, a native crop and a full-resolution reference. A crop carries roughly one twelfth of the full frame's pixels. Depth, physical lighting and semantic object tracking are outside this fixture.

Only derived measurements and the model review go to stdout. Raster captures stay in memory. This is sampled playback and not a browser decoder performance benchmark.

## Antigravity and individual loci

Use the example MCP configuration with absolute paths to the two local Python environments. The tool process exposes a bounded local scratchpad, routed Mr Calendar text chat, and the fixed video review. The chat tool connects to the local gateway on port 11437. Set one `MR_CALENDAR_LOCUS` name per tool process. The working notes are under `${XDG_STATE_HOME:-$HOME/.local/state}/arc-mr-calendar/<locus>/`. Separation between different human operators requires different Unix accounts. A LOCI name is a namespace and does not authenticate an operator.

The sample uses MCP over stdio, so the owner can keep the tools local. To add a VPS Glassbox link, first verify its reachable endpoint, server identity, and how it authenticates each locus. Publish through an explicit reviewed submission from the operator. Scratchpads, prompts and captured frames stay local by default.

## Parallel audio capture

Create a dedicated PulseAudio sink and launch the isolated browser driver using that sink. For this test, playback audio is routed to the capture sink.

```bash
sink_module=$(pactl load-module module-null-sink sink_name=loci_media_test)
PULSE_SINK=loci_media_test geckodriver --host 127.0.0.1 --port 4447
# In a second terminal
python playground/mr_calendar/live.py --audio --start 90 --frames 32 --ask -- -nxb3VCH7Vw
# After stopping the test driver, clean up the sink
pactl unload-module "$sink_module"
```

The audio track reports 50 ms PCM RMS windows. It maps audio read times onto the browser playback clock using nearby video samples. This alignment is approximate and excludes device-latency calibration. Audio is optional. The earlier offline MusicSympRev path provides additional spectral features.

## Workstation test on 2026-11-20

At about 03:49 Asia/Dhaka, Firefox captured 31 advancing YouTube video samples and 98 nonzero audio windows over roughly 4.9 seconds. The video time bracket had a 40 ms median. The heat map selected native crops in 20 samples across seven regions. At the reviewed instant, the full and coarse frames showed a character, while the selected crop showed a bright structure. The motion-driven crop missed the character.

The original animated fixture selected 31 crops across the two occupied regions. At the reviewed instant, the focused and full images returned the correct label, `ORBIT-742`. The low-resolution whole frame returned `OBJECT-342`. The first run caught a stale crop after the subject moved. Requiring texture in the current frame corrected the fixture case. Neither check establishes semantic tracking across arbitrary footage.

The MCP client discovered the four tools and verified isolated scratchpad writes, reads, and a routed local chat reply. The Antigravity example configuration has not been loaded into an IDE.

## Writing and voice

The `calendar_chat` tool accepts recent turns from its client. The `calendar_write` tool accepts an audience and returns Markdown for review in the client, with lists and language-tagged code blocks when requested. It does not run commands contained in a draft.

For local full-duplex speech, NVIDIA PersonaPlex is a candidate. It receives and generates audio concurrently. The checkpoint requires Hugging Face access and acceptance of the model terms. The workstation request was denied without authentication. The RTX 5060 Ti had 16,311 MiB total and 4,980 MiB free during this check. NVIDIA documents CPU offload, but this workstation's overlap and interruption latency have yet to be measured. The voice engine and the LOCI tools would need a separate bridge before a live voice session could call LOCI. [PersonaPlex runtime](https://github.com/NVIDIA/personaplex) and [model access](https://huggingface.co/nvidia/personaplex-7b-v1).

## Cue forecasting

A causal forecast can estimate whether a scene transition is likely in the next two seconds. Give it only packets available at the forecast time. Audio dips, sustained quiet, spatial framing and changes in movement can be predictive together. A semantic match to earlier scenes can help interpret an ambiguous cue, provided the reference corpus excludes the clip and near duplicates.

An initial experiment should use held-out scenes with annotated events and fake-outs. Compare audio-only, video-only, and combined inputs, with and without semantic retrieval. Report false alerts per minute, precision and recall, and warning time for correct alerts. Use only preceding context for each forecast and compare against an event-rate baseline.
