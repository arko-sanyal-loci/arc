# Inference lane measurements

`results/2026-11-22-inference-lanes-smoke.json` is a short workstation baseline
from `scripts/benchmark_inference_lanes.py`. It measures Ollama-compatible API
latency and generated-token throughput for the configured Arc and RTX endpoints.
The run uses a warmed `qwen3.5:4b-q4_K_M` model, three sequential trials per
lane, and one simultaneous request to both lanes.

This is an API smoke benchmark, not a peak-performance result, pooled-VRAM test,
or proof of cross-vendor tensor parallelism. The script records no prompt or
response content beyond its fixed synthetic prompt and output length. Intel GPU
utilization was not captured because `intel_gpu_top` requires `CAP_PERFMON` on
the measured workstation.

Run a new baseline from the repository root:

```sh
python3 scripts/benchmark_inference_lanes.py --trials 5 --max-tokens 48
```

Save results to a new timestamped file; do not overwrite prior measurements.
