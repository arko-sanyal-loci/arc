#!/usr/bin/env python3
"""Measure inference latency and throughput for configured Ollama-compatible lanes.

This is a repeatable API-level benchmark, not a claim about aggregate VRAM or
cross-vendor tensor parallelism. It performs one warm-up per lane, sequential
trials, then one simultaneous request to each lane. Responses are not saved.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


PROMPT = (
    "Write a concise 30-word explanation of why stable backups matter "
    "for software projects."
)


def request(base_url: str, model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_predict": max_tokens, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"request to {base_url} failed: {exc}") from exc
    wall_ms = (time.perf_counter() - started) * 1000
    eval_count = int(result.get("eval_count") or 0)
    eval_ns = int(result.get("eval_duration") or 0)
    return {
        "ok": bool(result.get("done")) and bool(result.get("response", "").strip()),
        "wall_ms": round(wall_ms, 1),
        "prompt_tokens": result.get("prompt_eval_count"),
        "prompt_ms": round((result.get("prompt_eval_duration") or 0) / 1e6, 1),
        "output_tokens": eval_count,
        "output_chars": len(result.get("response", "")),
        "generation_ms": round(eval_ns / 1e6, 1),
        "generation_tokens_per_second": round(eval_count / (eval_ns / 1e9), 2)
        if eval_ns else None,
        "load_ms": round((result.get("load_duration") or 0) / 1e6, 1),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["ok"]]
    return {
        "passed": len(valid),
        "failed": len(rows) - len(valid),
        "median_wall_ms": round(statistics.median(r["wall_ms"] for r in valid), 1)
        if valid else None,
        "median_generation_tokens_per_second": round(
            statistics.median(r["generation_tokens_per_second"] for r in valid), 2
        ) if valid else None,
        "trials": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:4b-q4_K_M")
    parser.add_argument("--arc-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rtx-url", default="http://127.0.0.1:11435")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=48)
    args = parser.parse_args()
    if args.trials < 1 or args.max_tokens < 1:
        parser.error("--trials and --max-tokens must be positive")

    lanes = {"arc": args.arc_url, "rtx": args.rtx_url}
    output: dict[str, Any] = {
        "schema": "loci.inference-lane-benchmark.v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "prompt": PROMPT,
        "max_tokens": args.max_tokens,
        "trial_count": args.trials,
        "telemetry_note": (
            "API timing is measured. GPU utilization is not collected by this script; "
            "intel_gpu_top requires CAP_PERFMON on this workstation."
        ),
        "lanes": {},
    }

    try:
        for lane, url in lanes.items():
            request(url, args.model, PROMPT, args.max_tokens)  # warm-up, excluded
            rows = [request(url, args.model, PROMPT, args.max_tokens)
                    for _ in range(args.trials)]
            output["lanes"][lane] = {"url": url, **summarize(rows)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                lane: pool.submit(request, url, args.model, PROMPT, args.max_tokens)
                for lane, url in lanes.items()
            }
            output["simultaneous"] = {
                lane: future.result() for lane, future in futures.items()
            }
    except RuntimeError as exc:
        output["error"] = str(exc)
        print(json.dumps(output, indent=2))
        return 1

    output["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(output, indent=2))
    return 0 if all(
        lane["failed"] == 0 for lane in output["lanes"].values()
    ) and all(row["ok"] for row in output["simultaneous"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
