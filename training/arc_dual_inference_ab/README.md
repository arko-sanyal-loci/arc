# Arc dual-inference synthetic evaluation pack

This pack compares two **orchestration protocols**, without touching model weights:

- **A — category-adaptive speculative path:** route by category/complexity, then draft and verify in sequence; optional TRS tie-breaker is recorded as an experimental factor.
- **B — deductive + connection streams:** two independent mock lanes produce evidence/constraints and alternatives/human-impact checks in parallel; a director arbitrates disagreements and synthesizes one result.

The runner is a scaffold benchmark. It measures the included mock scheduler and deterministic delay model only. It cannot establish reasoning quality, emotional intelligence, actual GPU/network throughput, learned weights, or production speedup. To compare real Arc behavior, wire both protocols to the same local model endpoint and preserve prompts, context, model revision, sampling parameters, seed, and resource limits. Have reviewers score anonymized outputs against the rubric before looking at arm labels.

## Run

```sh
python3 run_synthetic.py --runs 20 --json results.json
```

No model endpoint is contacted. The runner writes only the chosen output path. It does not import Arc runtime clients or touch `/dev/shm`.

## Factors and measurements

Both arms receive the same cases, same seed per paired run, and same synthetic latency schedule. The runner reports scheduler latency, pairwise protocol delta, path count, and static quality rubric dimensions as `not_measured`. TRS is explicitly isolated as a tie-breaker condition; do not enable the nudge based on reported tuning alone.

## Current repository evidence

The checked-in `core/speculative_pipeline.py` clients return placeholder draft token IDs and accept every draft token. The moderate path awaits draft then verify; its prefill is explicitly simulated. Its computed speedup is therefore not a GPU measurement. This pack uses independent mocks to make that limitation visible.

## Learning-loop staging

Follow `GOAL_WORKFLOW.md`: define a goal and observable outcomes, hold conditions fixed, compare category-adaptive and deductive/connection streams, require evidence and human review, then decide whether a bounded change is justified. No weight updates are part of this experiment.
