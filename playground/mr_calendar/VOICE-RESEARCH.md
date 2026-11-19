# Full-duplex voice for Mr Calendar

Research recorded on 2026-11-20 Asia/Dhaka. Arko Sanyal defined the goal of a local conversation that can hear interruptions, speak naturally, and reach shared LOCI tools. Codex compared the runtimes and checked local prerequisites.

## Shortlist

| Runtime | Why it fits | Evidence and limitation |
| --- | --- | --- |
| Kyutai Moshi Q8 on Rust/Candle | Local, audio-native full duplex and an official CUDA path. Best first workstation baseline. | The public English checkpoint and codec total 8.55 GB on disk. A request from the workstation reached the weights without authentication. Rust Cargo and CUDA nvcc were absent from the shell PATH. Actual GPU residency and timing are unmeasured. [Runtime](https://github.com/kyutai-labs/moshi) · [Checkpoint](https://huggingface.co/kyutai/moshiko-candle-q8/tree/main) |
| NVIDIA PersonaPlex | Local full duplex with stronger role and voice conditioning. Useful comparison once access is granted. | The weight endpoint returned HTTP 401. Complete the publisher's model terms on Hugging Face. NVIDIA documents CPU offload, which still needs a local jitter and interruption test. [Runtime](https://github.com/NVIDIA/personaplex) · [Checkpoint access](https://huggingface.co/nvidia/personaplex-7b-v1) |
| OpenAI Realtime | Hosted speech-to-speech reference with documented interruption handling, WebRTC and function calls. | A session sends microphone audio to a hosted API. No cloud session was started. [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) |

MoshiRAG is particularly relevant to LOCI. Kyutai studies retrieving background information asynchronously while an audio-native conversation continues. This is a design reference for an Arc-side tool and retrieval worker alongside the voice loop. Standard Moshi's CUDA release and PersonaPlex's role prompts do not establish production-ready LOCI tool execution. [MoshiRAG](https://kyutai.org/blog/2026-04-30-moshi-rag/)

## Local first test

The RTX 5060 Ti reported 16,311 MiB total and 12,019 MiB free during one check and only 4,980 MiB free earlier with more models resident. Disk weight size is not a complete VRAM budget. First measure one voice session on the RTX with competing RTX models idle, then repeat under the expected Arc-side tool and research workload. No voice runtime was installed during this research pass.

Use the USB microphone and headphones first. Test interruption mid-sentence, a short acknowledgment during playback, a long pause that should not be treated as end of turn, and a slow LOCI lookup. Capture median and p95 time to first audible response, time from interruption to stopped playback, audio dropouts, false interruptions and peak VRAM. Include the percentage of interruptions that actually change the answer. Record English and Bangla results separately if testing both. The first candidate is an English checkpoint.

For speaker output, repeat with a measured echo path and check whether the model interrupts itself on delayed playback. The connected Echo Dot exposes playback only on this workstation. The paired Bose headphones did not expose an input during this check.
