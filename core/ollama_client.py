"""
core/ollama_client.py — Native Ollama API client for Arc

Provides native Ollama API access on top of the existing OpenAI-compat path:
  - /api/tags   → model list with size + quantization info
  - /api/chat   → native streaming with eval_count/eval_duration for accurate tok/s
  - /api/show   → model details
  - benchmark() → compare tok/s: native path vs OpenAI-compat path
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import aiohttp

OLLAMA_BASE = "http://localhost:11434"


@dataclass
class OllamaModel:
    name: str
    size_gb: float
    quant: str
    modified: str
    details: dict = field(default_factory=dict)


@dataclass
class OllamaChatResult:
    content: str
    prompt_tokens: int
    eval_tokens: int
    eval_duration_ns: int
    tok_per_sec: float
    total_duration_ns: int
    model: str


@dataclass
class BenchmarkResult:
    provider: str
    model: str
    prompt: str
    tok_per_sec: float
    total_tokens: int
    latency_ms: float
    error: str | None = None


class OllamaClient:
    """
    Native Ollama API client. Complements api_provision.py's OpenAI-compat path
    (Provider.LOCAL → localhost:11434/v1) with accurate tok/s via /api/chat's
    eval_count / eval_duration fields (returned only on the native endpoint).
    """

    def __init__(self, base_url: str = OLLAMA_BASE, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.last_stats: OllamaChatResult | None = None

    async def list_models(self) -> list[OllamaModel]:
        """List all locally available Ollama models with metadata."""
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(f"{self.base_url}/api/tags") as resp:
                resp.raise_for_status()
                data = await resp.json()

        models = []
        for m in data.get("models", []):
            details = m.get("details", {})
            size_bytes = m.get("size", 0)
            models.append(OllamaModel(
                name=m.get("name", ""),
                size_gb=round(size_bytes / 1e9, 2),
                quant=details.get("quantization_level", "?"),
                modified=m.get("modified_at", ""),
                details=details,
            ))
        return models

    async def show_model(self, name: str) -> dict:
        """Get detailed model info (architecture, context length, etc.)."""
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(
                f"{self.base_url}/api/show",
                json={"name": name},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        options: dict | None = None,
    ) -> AsyncIterator[str]:
        """
        Native Ollama streaming chat. Yields text chunks.
        The final object (done=true) with eval_count/eval_duration is stored in
        self.last_stats after the generator is exhausted.
        """
        payload: dict = {"model": model, "messages": messages, "stream": True}
        if options:
            payload["options"] = options

        self.last_stats = None

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(
                f"{self.base_url}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                content_parts: list[str] = []
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("done"):
                        eval_count = obj.get("eval_count", 0)
                        eval_duration = obj.get("eval_duration", 1)  # ns
                        tok_ps = eval_count / (eval_duration / 1e9) if eval_duration else 0.0
                        self.last_stats = OllamaChatResult(
                            content="".join(content_parts),
                            prompt_tokens=obj.get("prompt_eval_count", 0),
                            eval_tokens=eval_count,
                            eval_duration_ns=eval_duration,
                            tok_per_sec=round(tok_ps, 2),
                            total_duration_ns=obj.get("total_duration", 0),
                            model=obj.get("model", model),
                        )
                        break
                    chunk = obj.get("message", {}).get("content", "")
                    if chunk:
                        content_parts.append(chunk)
                        yield chunk

    async def chat(
        self,
        model: str,
        messages: list[dict],
        options: dict | None = None,
    ) -> OllamaChatResult:
        """Non-streaming native Ollama chat. Returns full result with accurate tok/s."""
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if options:
            payload["options"] = options

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()

        eval_count = data.get("eval_count", 0)
        eval_duration = data.get("eval_duration", 1)
        tok_ps = eval_count / (eval_duration / 1e9) if eval_duration else 0.0

        return OllamaChatResult(
            content=data.get("message", {}).get("content", ""),
            prompt_tokens=data.get("prompt_eval_count", 0),
            eval_tokens=eval_count,
            eval_duration_ns=eval_duration,
            tok_per_sec=round(tok_ps, 2),
            total_duration_ns=data.get("total_duration", 0),
            model=data.get("model", model),
        )

    async def benchmark(
        self,
        prompt: str = "Explain what a Loci memory engine does in three sentences.",
        model: str | None = None,
        max_tokens: int = 200,
    ) -> list[BenchmarkResult]:
        """
        Benchmark tok/s for LOCAL (native Ollama /api/chat with accurate ns timing)
        vs LOCAL (OpenAI-compat /v1 with wall-clock timing).

        Returns list sorted by tok/s descending.
        Hardware reference:
          RTX 5060 Ti 16GB GDDR7 (448 GB/s): ~90-100 tok/s for 7B Q4
          Arc B70 32GB GDDR6 (608 GB/s):     ~25-30 tok/s for 32B Q4
          Remote (nexos/xAI):                 ~20-50 tok/s with network overhead
        """
        results: list[BenchmarkResult] = []

        if model is None:
            try:
                models = await self.list_models()
                model = models[0].name if models else "llama3.2:latest"
            except Exception:
                model = "llama3.2:latest"

        # --- native Ollama path (accurate tok/s via eval_duration) ---
        t0 = time.perf_counter()
        try:
            result = await self.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"num_predict": max_tokens},
            )
            results.append(BenchmarkResult(
                provider="LOCAL / native",
                model=result.model,
                prompt=prompt,
                tok_per_sec=result.tok_per_sec,
                total_tokens=result.eval_tokens,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            ))
        except Exception as exc:
            results.append(BenchmarkResult(
                provider="LOCAL / native",
                model=model,
                prompt=prompt,
                tok_per_sec=0.0,
                total_tokens=0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error=str(exc),
            ))

        # --- OpenAI-compat path (wall-clock tok/s) ---
        t0 = time.perf_counter()
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "stream": False,
                    },
                    headers={"Authorization": "Bearer ollama"},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            elapsed = (time.perf_counter() - t0) * 1000
            usage = data.get("usage", {})
            comp_tokens = usage.get("completion_tokens", 0)
            tok_ps = round(comp_tokens / (elapsed / 1000), 2) if elapsed > 0 else 0.0
            results.append(BenchmarkResult(
                provider="LOCAL / openai-compat",
                model=model,
                prompt=prompt,
                tok_per_sec=tok_ps,
                total_tokens=comp_tokens,
                latency_ms=round(elapsed, 1),
            ))
        except Exception as exc:
            results.append(BenchmarkResult(
                provider="LOCAL / openai-compat",
                model=model,
                prompt=prompt,
                tok_per_sec=0.0,
                total_tokens=0,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error=str(exc),
            ))

        results.sort(key=lambda r: r.tok_per_sec, reverse=True)
        return results


# Module-level singleton — shared by server.py and other modules
_client: OllamaClient | None = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
