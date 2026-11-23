# LOCI dual GPU model and media benchmark

This is a reproducible local comparison, not a general model leaderboard. It uses fictional data only and records raw Ollama responses. No user conversations, account documents, credentials, camera recordings, or copyrighted video frames are in the fixture.

## Test design

- Models are the local 4.2B Q4_K_M, 8.2B Q4_K_M vision, 25.8B Gemma 4 A4B Q4_K_M, and 27.8B Q4_K_M chat weights where available. The installed Ollama model tag and digest are recorded in the raw manifests. The B70 Vulkan service is `127.0.0.1:8000`; the RTX 5060 Ti CUDA service is `127.0.0.1:11435`. One inference request runs at a time, `num_ctx=4096`, fixed seed, temperature zero, no reasoning mode. Repetitions reverse the order to reduce order and heat bias.
- Six fixed tasks exercise exact recall, versioned conflict resolution, measured versus authored shader packet fields, audio-derived RMS/centroid facts, unsupported-emotion abstention, and a synthetic image question. Each model/device cell has two recorded repetitions. Ollama's prompt/eval counts and durations, total/load durations, wall time, full response, errors and adjacent resource snapshots are preserved as JSON Lines.
- The context suite runs the pinned LOCI `TfidfCompressor` on the same synthetic long conversation, then compares full history, compacted history, and compacted history with a top-one Nomic retrieval from an isolated five-record synthetic corpus. It records compaction sizes and retrieval rank. This tests one specific retrieval/compaction path, not all persistent-store behavior or unlimited context.
- The shader suite replays the existing EGL fixture on each GPU: three repetitions, 240 measured frames per case, 24 warm-up frames, five capture modes, three resolutions. Raw per-case metrics and renderer strings are kept. This is not camera capture and has no real-time pacing.
- A 15-minute low-rate telemetry file records timestamps, available host RAM, NVIDIA memory/utilization/power/temperature where exposed, Arc energy counter where exposed, and loaded-model server state. Intel GPU utilization telemetry requires `CAP_PERFMON`; this session cannot read it, so that field is explicitly unavailable.

## Run

From this directory, with the local services and shader virtual environment already present:

```bash
python3 -m venv .venv
.venv/bin/pip install -r reference/vision_hearing/requirements.txt
python3 run_matrix.py
python3 run_context.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python \
  reference/vision_hearing/benchmark.py --device 0 --expect NVIDIA \
  --frames 240 --repeat 3 --output raw/shader-rtx.json
OPENBLAS_NUM_THREADS=1 .venv/bin/python \
  reference/vision_hearing/benchmark.py --device 2 --expect BMG \
  --frames 240 --repeat 3 --output raw/shader-arc.json
python3 sample_telemetry.py --duration-s 900 --interval-s 5 --output raw/telemetry-15min.jsonl
```

The LOCI adapter loads the compressor from commit `bbd50360a868ea4e97e8888d4cf46786b6652620` in a sibling `loci-canonical` checkout. Set `LOCI_ENGINE_CHECKOUT=/path/to/loci-canonical` when it is elsewhere. Use fresh output paths because the shader runner refuses to overwrite evidence. Results are tied to the exact workstation, drivers, models and Ollama build in the environment record.

## Interpretation limits

Exact-term coverage is a screening rubric and does not establish general reasoning quality. Two repetitions per cell are insufficient for broad performance claims. The vision fixture is generated and static. The audio test presents measured synthetic features as text; the chat models do not receive raw audio in this test. The shader's authored depth, incidence and blur values are labels from the renderer, not recovered physical quantities. No listener ratings, emotion labels, real-world camera stream, live web browser capture, distributed KV offload, or Google Drive write path is being tested. Historical Drive benchmark claims are not copied into this result; a source warning found in the connected Drive evidence says some older LOCI scores conflict with original emails.

See [RESULTS.md](RESULTS.md) for the measured run, the complete records in `raw/`, and the deterministic scoring summary.
