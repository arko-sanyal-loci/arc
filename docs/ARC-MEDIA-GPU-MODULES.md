# Arc media and GPU modules

Arc keeps the intelligence runtime, media references, and hardware evidence
separate so each can be tested without implying that a synthetic fixture is a
live perception system.

| Area | Location | Boundary |
| --- | --- | --- |
| Intelligence/runtime | `core/`, `loci/`, `app/` | Local inference, memory, routing, and chat surfaces |
| Vision/video | `playground/benchmarks/loci-dual-gpu-2026-11-20/reference/vision_hearing/` | Synthetic EGL capture and packet benchmark |
| Audio | `playground/benchmarks/loci-dual-gpu-2026-11-20/reference/audio/` | Offline PCM feature extraction and timeline multiplexing |
| Parallel GPU | `playground/benchmarks/loci-dual-gpu-2026-11-20/run_*.py` and `core/gpu_offload.py` | Reproducible device-lane comparison and runtime offload |
| Evidence | `playground/benchmarks/loci-dual-gpu-2026-11-20/raw/` | Synthetic fixtures and recorded benchmark outputs |

The `vision_hearing` and `audio` directories are reference snapshots. They do
not include camera recordings, microphone captures, credentials, personal
conversations, or model weights. Results must be interpreted with the limits
documented in the benchmark README and `RESULTS.md`.
