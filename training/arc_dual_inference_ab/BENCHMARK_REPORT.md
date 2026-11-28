# Arc synthetic A/B report

**Run date:** 2026-11-27 UTC
**Model/GPU/network:** none
**Weights changed:** no
**Scoring:** semantic quality not measured; benchmark emits no model answer

The runner executed 20 paired seeds across five synthetic task categories. A models category-adaptive serial draft/verify; B models two parallel lanes and director arbitration. Only component delays are simulated.

| A condition | A modeled mean | B modeled mean | Paired B − A |
|---|---:|---:|---:|
| TRS off | 63.80 ms | 67.90 ms | +4.10 ms |
| TRS on | 69.84 ms | 67.90 ms | −1.94 ms |

The modeled TRS nudge adds its mock cost to A. These values describe these hand-set synthetic schedules only. They do not predict Arc latency, validate TRS, or establish a reasoning winner. To test the actual design, use fixed local model revisions and blinded rubric scores, measure each stage and cross-device bytes, and keep TRS on/off as a separate factor.

## Current scaffold root cause

The inspected `core/speculative_pipeline.py` clients fabricate draft token IDs and accept all of them. Its moderate path awaits the placeholder Arc “prefill,” then draft, then verify. Therefore a result from that path cannot establish real acceptance rates or hardware speedup. The primary `/api/chat` path calls one `APIProvisioner` provider and does not invoke two hemispheres.

## Related reported TRS data

A Nov 25 Gmail message reports EMA+hysteresis/no post-hoc blending at mean error 0.0088 with 23/100 block regressions; multi-hop tie-breaker at mean error 0.0107 with 26/100. Those are reported prior measurements, not reproduced by this synthetic run. The final requested ZIP could not be read from Gmail and is not included here.
