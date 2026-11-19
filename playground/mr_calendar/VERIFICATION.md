# Reproduce the Mr Calendar workstation checks

One run checks the focus rules, the text gateway, the RTX and Arc capture benchmarks, and the offline audio and video timeline. The optional live checks exercise the YouTube browser path and explicitly selected microphones. The report timestamps the run, checksums its files, and marks unverified claims separately.

## Run from the LOCI workspace root

Use the pinned Python environment from `playground/vision-hearing`. Run the Mr Calendar adaptive gateway on loopback port 11437. The offline tests use the existing MusicSympRev WAV fixtures. Device indices default to RTX EGL 0 and Intel Arc EGL 2. Override them with `--rtx-device` or `--arc-device` if necessary. The runner checks both renderer names.

```bash
python=playground/vision-hearing/.venv/bin/python
runner=arc-canonical/playground/mr_calendar/verify.py
run_dir="$(mktemp -d)"
"$python" "$runner" --workspace "$PWD" --output "$run_dir/results"
```

For live playback, start the dedicated PulseAudio sink and GeckoDriver as described in the [capture setup](README.md). Add `--live` to the command. Optionally add `--mic SOURCE` for each input shown by `live.py --list-mics`. The live microphone test runs against an original local browser fixture. It records feature values only and requires explicit device names. Keep the output folder local if it includes device IDs or room-audio features.

The resulting `report.json` contains measured values and per-file hashes. Live checks retry once and keep both attempt outcomes and stderr logs. The run exits nonzero when a requested check still fails. A skipped test was not requested. An unverified item still requires a separate experiment. Browser playback and hardware timings may vary between runs.

## What to say when someone asks how it works

- The shader draws a known scene. The benchmark checks what was preserved against that known scene. It times rendering, readback and packet creation. The GPU test does not time YouTube playback or a model reading the feed.
- The heat map compares recent video frames tile by tile. It raises detail where motion and current texture justify it. Every packet can keep a whole-scene overview. The live test also showed that motion can select the wrong subject.
- Audio and video keep different sampling rates. Timestamps put them in the same sequence. The offline timing test moves the score by a known amount and checks the result. The live microphone check confirms distinct input tracks. It does not measure the accuracy of speech understanding or sample-accurate alignment.
- Native full-duplex speech, semantic prediction on unseen scenes, and independent reproduction are separate claims. See the [voice test plan](VOICE-RESEARCH.md) and [publication checks](PUBLICATION-NOTES.md).
