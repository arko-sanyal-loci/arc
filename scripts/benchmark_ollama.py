#!/usr/bin/env python3
"""
scripts/benchmark_ollama.py — Standalone tok/s benchmark for Arc's Ollama backend

Compares:
  - LOCAL / native:          /api/chat  with eval_count / eval_duration (ns) — most accurate
  - LOCAL / openai-compat:   /v1/chat/completions with wall-clock timing

Hardware targets (design):
  RTX 5060 Ti 16 GB GDDR7 (448 GB/s)  → ~90-100 tok/s for 7B Q4
  Arc Pro B70  32 GB GDDR6 (608 GB/s)  → ~25-30  tok/s for 32B Q4
  Remote (nexos / xAI)                 → ~20-50  tok/s + network overhead

Usage:
  python scripts/benchmark_ollama.py
  python scripts/benchmark_ollama.py --model llama3.2:3b --tokens 300 --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.ollama_client import OllamaClient


PROMPTS = [
    "Explain what a Loci memory engine does in three sentences.",
    "What is the difference between VRAM and DDR5 for LLM inference?",
    "Describe the arc-rise animation keyframe and what it communicates to the user.",
]


async def run(args: argparse.Namespace) -> None:
    client = OllamaClient()

    # List models
    try:
        model_list = await client.list_models()
        print("Available Ollama models:")
        for m in model_list:
            print(f"  {m.name:<40} {m.size_gb:.1f} GB  {m.quant}")
        print()
    except Exception as exc:
        print(f"[warn] Could not list models: {exc}")
        model_list = []

    model = args.model
    if model is None and model_list:
        model = model_list[0].name
        print(f"Using first available model: {model}\n")
    elif model is None:
        model = "llama3.2:latest"
        print(f"No models found — defaulting to {model}\n")

    prompt = PROMPTS[0] if not args.prompt else args.prompt

    all_native: list[float] = []
    all_compat: list[float] = []

    print(f"Prompt: {prompt!r}")
    print(f"Model:  {model}")
    print(f"Runs:   {args.runs}")
    print(f"Tokens: {args.tokens}")
    print()

    for run_i in range(1, args.runs + 1):
        print(f"── Run {run_i}/{args.runs} ──────────────────────────────")
        results = await client.benchmark(
            prompt=prompt,
            model=model,
            max_tokens=args.tokens,
        )
        for r in results:
            if r.error:
                print(f"  {r.provider:<30} ERROR: {r.error}")
            else:
                print(
                    f"  {r.provider:<30} "
                    f"{r.tok_per_sec:6.1f} tok/s  "
                    f"{r.total_tokens:4d} tokens  "
                    f"{r.latency_ms:.0f} ms"
                )
                if "native" in r.provider:
                    all_native.append(r.tok_per_sec)
                else:
                    all_compat.append(r.tok_per_sec)
        print()

    if args.runs > 1:
        print("── Summary ──────────────────────────────────────────")
        if all_native:
            avg_n = sum(all_native) / len(all_native)
            print(f"  native avg:        {avg_n:.1f} tok/s")
        if all_compat:
            avg_c = sum(all_compat) / len(all_compat)
            print(f"  compat avg:        {avg_c:.1f} tok/s")
        print()

    print("── Hardware reference ───────────────────────────────")
    print("  RTX 5060 Ti 16GB GDDR7  (448 GB/s):  ~90-100 tok/s  [7B Q4]")
    print("  Arc Pro B70  32GB GDDR6 (608 GB/s):  ~25-30  tok/s  [32B Q4]")
    print("  Remote (nexos / xAI):                ~20-50  tok/s  [+ network]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arc Ollama backend benchmark")
    parser.add_argument("--model",  default=None,  help="Ollama model tag (default: first available)")
    parser.add_argument("--prompt", default=None,  help="Prompt to use")
    parser.add_argument("--tokens", default=200,   type=int, help="Max tokens to generate")
    parser.add_argument("--runs",   default=1,     type=int, help="Number of benchmark runs")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
