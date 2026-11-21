# Publication check for adaptive audiovisual capture

Preliminary literature and evidence review, 2026-11-20. Arko Sanyal proposed using audiovisual cues to anticipate events and change the resolution allocated to individual packet families. Codex implemented the capture prototypes and drafted this comparison.

## Related work

- FOVEA allocates resolution within a small image canvas to preserve small-object detection. RegionFilter streams regions at different quality levels for analytics. Both establish strong precedents for a graded visual aperture. [FOVEA](https://openaccess.thecvf.com/content/ICCV2021/html/Thavamani_FOVEA_Foveated_Image_Magnification_for_Autonomous_Navigation_ICCV_2021_paper.html) · [RegionFilter](https://www.sciencedirect.com/science/article/pii/S1389128624004560)
- Audio-visual gaze anticipation already uses audio and video together to estimate where a person will look. A horror-film experiment also separated audio-only, video-only, and combined conditions around anticipated and unprompted stingers. [Audio-visual gaze anticipation](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/01396.pdf) · [Horror-film experiment abstract](https://static1.squarespace.com/static/65fa2ea634036b6933902afe/t/672927313c10c33c5fc05a39/1730750267192/SMPC24_ProgramDraft_v5.pdf)
- MoshiRAG is direct prior art for asynchronous retrieval alongside a native full-duplex voice stream. [MoshiRAG](https://kyutai.org/blog/2026-04-30-moshi-rag/)

## Candidate contribution and evidence needed

Test whether forecasting which information a downstream model will need lets a streaming system allocate fidelity across audio and video before the event. Compare at the same transmitted byte and inference-token budgets. The central question is whether causal, relevance-conditioned scheduling preserves more task evidence than purely reactive allocation. Prioritize brief off-center text, speech cues that precede a cut, quiet subjects, and false alarms.

- Compare a fixed whole-frame stream, motion-only graded capture, reactive model-confidence upgrades, and the proposed semantic forecast. Include native capture as a reference. Hold downstream models and budgets constant, and report encode and inference costs separately.
- Isolate the contribution of audio, visual features, and retrieved examples. Split by source video and exclude near-duplicates and future packets from both the retrieval corpus and the query.
- Record task accuracy, bytes, tokens, latency, event lead time, false alerts per minute, and misses. Include confidence intervals by scene and the whole-frame fallback rate. Independently reproduce any claimed operating point.
- Evaluate voice separately for interruption, pause handling and tool result delivery. A natural voice alone would not validate the capture hypothesis.

The current evidence supports a reproducible prototype and a failure report. Synthetic detail and GPU timing, a short live scene, and simultaneous microphone capture establish feasibility only. A paper claim needs held-out task results and a broader prior-art search after the measured effect is known.
