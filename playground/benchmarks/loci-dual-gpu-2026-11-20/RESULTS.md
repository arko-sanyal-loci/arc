# Results — 2026-11-20

## Scope and setup

The run tested every local chat model found in both Ollama endpoints: Qwen 3.5 4.2B Q4_K_M, Qwen3-VL 8.2B Q4_K_M, Gemma 4 25.8B A4B Q4_K_M, and Qwen 3.6 27.8B Q4_K_M. Nomic Embed Text 137M F16 supplied embeddings for retrieval. There was **no 70B model installed**. The 8B model is therefore not heavier than a 70B model by parameter count; its local file is 6.19 GB. Gemma's `A4B` tag is a MoE variant with 25.8B reported total parameters; its file is 17.99 GB. The exact Ollama tags, digests, and file sizes are in [environment.json](raw/environment.json).

The two inference lanes were Arc Pro B70 using Vulkan at `127.0.0.1:8000` and RTX 5060 Ti using CUDA at `127.0.0.1:11435`; both ran Ollama 0.40.16. Main chat comparisons used `think=false`, temperature 0, `num_ctx=4096`, fixed per-trial seeds, and 160 maximum output tokens. The same six fictional tasks and static generated image were sent to every model/device pair, twice, with reversed order on the second round. There were **96/96 completed calls, no blank answers, and no HTTP errors** in this standard mode run.

## Model and GPU comparison

Values are medians across 12 calls per model/device combination. The completion rate is the deterministic required-fact screening rubric across six tasks × two repetitions; it is not a general intelligence or quality score.

| Model | GPU lane | Median total time | p90 total time | Median output rate | Rubric passes |
| --- | --- | ---: | ---: | ---: | ---: |
| Qwen 3.5 4.2B | Arc B70 Vulkan | 1.53 s | 2.05 s | 98.2 tok/s | 12/12 |
| Qwen 3.5 4.2B | RTX 5060 Ti CUDA | 1.34 s | 4.48 s | 106.8 tok/s | 12/12 |
| Qwen3-VL 8.2B | Arc B70 Vulkan | 1.51 s | 3.29 s | 82.0 tok/s | 12/12 |
| Qwen3-VL 8.2B | RTX 5060 Ti CUDA | 1.41 s | 3.01 s | 81.0 tok/s | 12/12 |
| Gemma 4 25.8B A4B | Arc B70 Vulkan | 1.59 s | 3.19 s | 90.8 tok/s | 12/12 |
| Gemma 4 25.8B A4B | RTX 5060 Ti CUDA | 1.83 s | 2.52 s | 66.6 tok/s | 12/12 |
| Qwen 3.6 27.8B | Arc B70 Vulkan | 6.54 s | 11.81 s | 26.8 tok/s | 12/12 |
| Qwen 3.6 27.8B | RTX 5060 Ti CUDA | 11.60 s | 13.46 s | 12.5 tok/s | 12/12 |

The test workload favors short, structured extraction and includes a small image. It does not establish that a smaller model is better at open-ended reasoning. The repeated 27.8B result favored this Arc Vulkan lane over this RTX CUDA lane for output rate; confirm on more prompts and repetitions before making a general hardware claim. The RTX reached a sampled 15,212 MiB used out of 16,311 MiB during the broader run, without an observed model-call failure or OOM.

## LOCI context and exact-source retrieval

The context experiment used the pinned LOCI compressor on a synthetic 22,231-character, 22-message history. It emitted 6,396 characters / 7 messages. In the equivalent Qwen long-context calls, prompt-evaluated tokens fell from 3,744 to 1,186 (about 68%). The compressor retained all three target facts (`18.4 C`, `F-9`, `B3`) in this fixture. All 12 Qwen 8B/27B × GPU × context-mode answers retained those facts across full context, compacted context, and compacted context plus Nomic's top-one result.

The retrieval stress test had five fictional records, including close distractors. Nomic ranked distractor AT-10 first (cosine 0.8693), while the target AT-7 ranked fourth (0.8440). With top one only, all eight model/device combinations safely answered “unavailable” because AT-7 was absent. With top four, all eight returned the exact target facts without substituting the distractor's F-8. This is a successful tiny controlled example of abstention and candidate expansion; it does not validate arbitrary semantic recall, conflict resolution, or a production tie-breaker.

## Reasoning-mode failure reproduction

For the three models whose local tags expose `thinking`, six additional calls used `think=true` and `num_predict=1024` on both GPU lanes. All six ended with `done_reason=length`, `eval_count=1024`, and an empty final `message.content`. The API did not return an OOM or transport error. The runtime reported 3,189–3,768 characters in the separate internal thinking field; the benchmark records only that field's character count and deliberately does not save its content. This reproduces a generation-budget exhaustion failure: reasoning consumed the available token budget before the final answer. The non-reasoning matrix returned complete answers. Dynamic context compaction cannot replenish a generation-token budget; a production wrapper should reserve final-answer tokens, detect empty/length-truncated responses, and retry with a larger budget or `think=false`.

## Shader and image/audio results

The EGL shader fixture ran 45 cases on each GPU: five modes × three resolutions × three repetitions, with 240 measured frames per case. At 1080p the median p95 frame-processing time and cumulative frames over 10 ms across the three repetitions were:

| Capture mode | RTX p95 | Arc p95 | RTX frames >10 ms / 720 | Arc frames >10 ms / 720 |
| --- | ---: | ---: | ---: | ---: |
| Full native readback | 8.393 ms | 10.115 ms | 82 | 238 |
| Coarse preview | 0.357 ms | 0.573 ms | 0 | 0 |
| Hybrid preview + text crop | 0.395 ms | 0.670 ms | 0 | 0 |
| Adaptive, authored motion/text change | 0.388 ms | 0.615 ms | 0 | 0 |
| Observed adaptive | 0.550 ms | 0.851 ms | 0 | 0 |

Observed mode still reads its preview every frame. Adaptive mode uses the fixture's authored motion and text-change reference, so its result is an optimistic upper-bound path, not a live detector result. These frame timings have no real-time pacing and exclude browser/camera capture, transport, model inference, and display.

The chat vision task used a static synthetic Pillow image, not a live website screenshot or output frame from the EGL renderer. Each model read the label/color and reported limits; all 96 fixed-task calls passed the screening rubric. Audio was represented as known synthetic RMS onset and spectral-centroid values in text. The test did **not** feed raw audio into an audio model, estimate emotion, test a microphone, or measure synchronized audio/video playback. The existing waveform/timeline code remains separate from raw-audio chat perception.

## 15-minute resource sampling

The sampler ran for 900 seconds (15 minutes); its 180 records span 895.0 seconds from the first to last five-second sample. It captured NVIDIA GPU memory/utilization/power/temperature snapshots, system available RAM, loaded Ollama model state, and the Arc energy counter. NVIDIA samples ranged 24–15,212 MiB, 0–100% utilization, 40–66 °C, and up to 130.54 W. Lowest sampled available host memory was 17.16 GiB. The Arc energy counter advanced by 67,868 J over the interval (75.8 W average over the entire card/system interval; not attributable to this benchmark alone).

`intel_gpu_top` could not read Arc utilization because this session lacks `CAP_PERFMON`; Arc utilization is therefore not claimed. Its energy counter and the model endpoint's configured lane are available, but those do not substitute for utilization telemetry. Polling is sampled, so short peaks may be missed.

## Data files and provenance

The [raw directory](raw/) contains the full JSONL response records, exact prompt suites and context fixture, model digests, shader measurements, 15-minute telemetry, reasoning-mode completion records, and synthetic fixture image. The [reference folder](reference/) contains the shader source snapshot and the LOCI context adapter used for the run. Matrix raw responses do not include credentials or personal conversation data. The retrieval and context corpora are fictional. The connected Drive evidence search surfaced a warning that earlier LOCI benchmark figures need verification; none of those historical scores were reused here.

Architecture ideas for LOCI and the XYZ+A/graded-capture experiment are credited to Arko Sanyal. Codex prepared the controlled harness, ran the measurements, and wrote the reproducibility package. The raw records and checksums provide the evidence trail. See [README.md](README.md) for commands.
