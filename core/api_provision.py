"""
core/api_provision.py — API key provisioning for Arc: xAI (Grok) + nexos.ai gateway

nexos.ai: OpenAI-compatible gateway to 200+ models at https://api.nexos.ai
  - Single endpoint for Grok, Claude, GPT-4, Llama, Mistral, etc.
  - Smart routing + auto-failover built in
  - Headers: X-Api-Key: nexos-... OR Authorization: Bearer nexos-...

xAI: Grok API (direct)
  - Endpoint: https://api.x.ai/v1
  - OpenAI-compatible interface
  - Models: grok-2, grok-2-mini, grok-3 (when available)

Role in Arc — dual-track local inference:
  - LOCAL        RTX 5060 Ti (16GB GDDR7) — Ollama CUDA — localhost:11434/v1
                 Used for: nomic-embed, ≤8B models, single-user fast path
  - LOCAL_INTEL  Arc Pro B70 (32GB GDDR6) — Intel LLM-Scaler vLLM — localhost:8000/v1
                 Used for: 9B–70B models, mobile/web concurrent traffic
  - LOCAL_SGLANG Arc Pro B70 — SGLang XPU (experimental) — localhost:7000/v1
  - NEXOS        nexos.ai (burst / heavy tasks)
  - XAI          xAI Grok (failsafe)
  - API key storage: config/arc_config.yaml (encrypted field, loaded at startup)

Routing: embed/small → LOCAL → LOCAL_INTEL → NEXOS → XAI
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import AsyncGenerator, Optional

import aiohttp

from core.cache_layer import PromptCacheLayer, CacheTTL, LociItem, cache_layer  # noqa: F401


# ── Provider definitions ───────────────────────────────────────────────────────

class Provider(Enum):
    LOCAL        = auto()   # RTX 5060 Ti — Ollama CUDA — localhost:11434
    LOCAL_INTEL  = auto()   # Arc Pro B70 — Intel LLM-Scaler vLLM — localhost:8000
    LOCAL_SGLANG = auto()   # Arc Pro B70 — SGLang XPU (experimental) — localhost:7000
    NEXOS        = auto()   # nexos.ai gateway (burst / offset compute)
    XAI          = auto()   # xAI Grok (failsafe)


PROVIDER_ENDPOINTS = {
    Provider.LOCAL:        os.environ.get(
        "LOCI_LOCAL_BASE_URL",
        os.environ.get("ARC_LOCAL_ENDPOINT", "http://localhost:11434/v1"),
    ),
    Provider.LOCAL_INTEL:  os.environ.get(
        "LOCI_LOCAL_INTEL_BASE_URL", "http://localhost:8000/v1"
    ),
    Provider.LOCAL_SGLANG: os.environ.get(
        "LOCI_LOCAL_SGLANG_BASE_URL", "http://localhost:7000/v1"
    ),
    Provider.NEXOS:  "https://api.nexos.ai/v1",
    Provider.XAI:    "https://api.x.ai/v1",
}

PROVIDER_MODELS = {
    Provider.LOCAL:        os.environ.get("LOCI_LOCAL_MODEL", "arc-local"),
    Provider.LOCAL_INTEL:  os.environ.get("LOCI_LOCAL_INTEL_MODEL", "arc-b70"),
    Provider.LOCAL_SGLANG: os.environ.get("LOCI_LOCAL_SGLANG_MODEL", "arc-b70-sglang"),
    Provider.NEXOS:        "meta-llama/llama-3.3-70b-instruct",  # default; overridable
    Provider.XAI:          "grok-2",
}

# Routing table: model size → preferred provider
# Small (embed, ≤8B) → LOCAL; large (9B–70B) → LOCAL_INTEL; fallback chain: NEXOS → XAI
def provider_for_model(model_name: str) -> Provider:
    """
    Heuristic: pick LOCAL_INTEL for large models, LOCAL for small.
    arc-runtime callers can override by passing provider= explicitly.
    """
    lower = model_name.lower()
    large_tokens = ("70b", "72b", "65b", "34b", "32b", "30b", "13b", "14b")
    if any(t in lower for t in large_tokens):
        return Provider.LOCAL_INTEL
    return Provider.LOCAL

HEALTH_PATH = "/dev/shm/arc_api_health.json"


@dataclass
class APIKeys:
    nexos:         Optional[str] = None
    xai:           Optional[str] = None
    local:         Optional[str] = None   # optional; most local servers don't need a key
    local_intel:   Optional[str] = None   # Intel vLLM — usually "local" or empty
    local_sglang:  Optional[str] = None   # SGLang XPU — usually "local" or empty

    @classmethod
    def from_env(cls) -> "APIKeys":
        # Primary names match arc_config.yaml / .env.example (ARC_*_KEY).
        # Legacy aliases (without ARC_ prefix) are accepted as fallback.
        return cls(
            nexos=(os.environ.get("ARC_NEXOS_KEY") or os.environ.get("NEXOS_API_KEY")),
            xai=(os.environ.get("ARC_XAI_KEY") or os.environ.get("XAI_API_KEY")),
            local=os.environ.get("ARC_LOCAL_API_KEY", "ollama"),
            local_intel=os.environ.get("LOCI_LOCAL_INTEL_API_KEY", "local"),
            local_sglang=os.environ.get("LOCI_LOCAL_SGLANG_API_KEY", "local"),
        )

    @classmethod
    def from_config(cls, config_path: str = "config/arc_config.yaml") -> "APIKeys":
        """Load keys from YAML config (plain-text; use env vars in production)."""
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(Path(config_path).read_text())
            keys = cfg.get("api_keys", {})
            return cls(
                nexos=keys.get("nexos"),
                xai=keys.get("xai"),
                local=keys.get("local", "ollama"),
                local_intel=keys.get("local_intel", "local"),
                local_sglang=keys.get("local_sglang", "local"),
            )
        except Exception:
            return cls.from_env()

    def for_provider(self, p: Provider) -> Optional[str]:
        return {
            Provider.NEXOS:        self.nexos,
            Provider.XAI:          self.xai,
            Provider.LOCAL:        self.local,
            Provider.LOCAL_INTEL:  self.local_intel,
            Provider.LOCAL_SGLANG: self.local_sglang,
        }.get(p)


# ── Health check ───────────────────────────────────────────────────────────────

@dataclass
class ProviderHealth:
    provider:       Provider
    reachable:      bool  = False
    latency_ms:     float = 0.0
    last_checked:   float = field(default_factory=time.time)
    error:          str   = ""


async def _ping_provider(
    session: aiohttp.ClientSession,
    provider: Provider,
    keys: APIKeys,
    timeout: float = 4.0,
) -> ProviderHealth:
    endpoint = PROVIDER_ENDPOINTS[provider]
    key      = keys.for_provider(provider)
    headers  = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider == Provider.NEXOS and key:
        headers["X-Api-Key"] = key

    t0 = time.monotonic()
    try:
        async with session.get(
            f"{endpoint}/models",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            latency = (time.monotonic() - t0) * 1000
            ok = resp.status in (200, 401)   # 401 = key invalid but endpoint alive
            return ProviderHealth(
                provider=provider,
                reachable=ok,
                latency_ms=latency,
                last_checked=time.time(),
                error="" if ok else f"HTTP {resp.status}",
            )
    except Exception as exc:
        return ProviderHealth(
            provider=provider,
            reachable=False,
            latency_ms=(time.monotonic() - t0) * 1000,
            last_checked=time.time(),
            error=str(exc),
        )


async def health_check_all(keys: APIKeys) -> dict[Provider, ProviderHealth]:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            _ping_provider(session, Provider.LOCAL,        keys),
            _ping_provider(session, Provider.LOCAL_INTEL,  keys),
            _ping_provider(session, Provider.LOCAL_SGLANG, keys),
            _ping_provider(session, Provider.NEXOS,        keys),
            _ping_provider(session, Provider.XAI,          keys),
        )
    health = {r.provider: r for r in results}
    # Publish to shm
    try:
        Path(HEALTH_PATH).write_text(json.dumps({
            p.name: {
                "reachable":    h.reachable,
                "latency_ms":   round(h.latency_ms, 1),
                "last_checked": h.last_checked,
                "error":        h.error,
            }
            for p, h in health.items()
        }))
    except OSError:
        pass
    return health


# ── Chat completion client ─────────────────────────────────────────────────────

@dataclass
class ChatMessage:
    role:    str
    content: str


@dataclass
class CompletionRequest:
    messages:         list[ChatMessage]
    model:            Optional[str]        = None
    max_tokens:       int                  = 1024
    temperature:      float                = 0.7
    stream:           bool                 = False
    provider:         Provider             = Provider.LOCAL
    metadata:         dict                 = field(default_factory=dict)
    # Prompt caching support (Anthropic / nexos Claude routes only)
    tools:            list                 = field(default_factory=list)
    retrieved_items:  list                 = field(default_factory=list)
    enable_caching:   bool                 = True


@dataclass
class CompletionResponse:
    content:      str
    provider:     Provider
    model:        str
    tokens_used:  int
    latency_ms:   float
    error:        Optional[str]  = None


class APIProvisioner:
    """
    Arc API provisioner.

    Routing priority:
      1. Local (sovereignty / always first if model is loaded)
      2. nexos.ai (compute offset — heavier tasks or when local is saturated)
      3. xAI Grok (failsafe — when nexos unreachable)

    Usage:
        prov = APIProvisioner.from_config()
        await prov.start()
        response = await prov.complete(CompletionRequest(messages=[...]))
        await prov.stop()
    """

    def __init__(self, keys: APIKeys) -> None:
        self._keys      = keys
        self._health:   dict[Provider, ProviderHealth] = {}
        self._session:  Optional[aiohttp.ClientSession] = None
        self._running   = False

    @classmethod
    def from_env(cls) -> "APIProvisioner":
        return cls(APIKeys.from_env())

    @classmethod
    def from_config(cls, path: str = "config/arc_config.yaml") -> "APIProvisioner":
        return cls(APIKeys.from_config(path))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._running = True
        self._health  = await health_check_all(self._keys)

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    # ── Routing ───────────────────────────────────────────────────────────────

    def _pick_provider(self, requested: Provider) -> Provider:
        """
        Follow the priority chain, falling back on health.

        Chains:
          LOCAL        → LOCAL only (sovereignty; no API fallback for embed/small)
          LOCAL_INTEL  → LOCAL_INTEL → NEXOS → XAI
          LOCAL_SGLANG → LOCAL_SGLANG → LOCAL_INTEL → NEXOS → XAI
          NEXOS        → NEXOS → XAI
          XAI          → XAI only
        """
        chains: dict[Provider, list[Provider]] = {
            Provider.LOCAL:        [Provider.LOCAL],
            Provider.LOCAL_INTEL:  [Provider.LOCAL_INTEL, Provider.NEXOS, Provider.XAI],
            Provider.LOCAL_SGLANG: [Provider.LOCAL_SGLANG, Provider.LOCAL_INTEL, Provider.NEXOS, Provider.XAI],
            Provider.NEXOS:        [Provider.NEXOS, Provider.XAI],
            Provider.XAI:          [Provider.XAI],
        }
        for p in chains.get(requested, [requested]):
            h = self._health.get(p)
            if h and h.reachable:
                return p
        # Last resort: return requested even if unhealthy
        return requested

    # ── Completion ────────────────────────────────────────────────────────────

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        provider = self._pick_provider(req.provider)
        endpoint = PROVIDER_ENDPOINTS[provider]
        model    = req.model or PROVIDER_MODELS[provider]
        key      = self._keys.for_provider(provider)

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider == Provider.NEXOS and key:
            headers["X-Api-Key"] = key

        # Refresh local spatial/hardware orientation on every inference, including
        # requests that reach this provisioner outside the dashboard UI.
        from core.orientation import orient_messages
        oriented = orient_messages(req.messages, req.metadata.get("interaction_profile"))
        raw_msgs: list[dict] = [{"role": m.role, "content": m.content} for m in oriented]
        raw_tools = req.tools or []

        # Apply prompt caching breakpoints for Claude-family models
        cache = PromptCacheLayer(
            enabled     = req.enable_caching and provider != Provider.XAI,
            default_ttl = CacheTTL.EPHEMERAL,
        )
        cached_msgs, cached_tools = cache.annotate(
            messages        = raw_msgs,
            tools           = raw_tools,
            retrieved_items = req.retrieved_items,
            model           = model,
        )
        # Merge extended-TTL beta headers if needed
        headers.update(cache.all_extra_headers())

        payload: dict = {
            "model":       model,
            "messages":    cached_msgs,
            "max_tokens":  req.max_tokens,
            "temperature": req.temperature,
            "stream":      False,
        }
        if cached_tools:
            payload["tools"] = cached_tools

        if self._session is None:
            raise RuntimeError("APIProvisioner.start() must be called before complete()")
        t0 = time.monotonic()
        try:
            async with self._session.post(
                f"{endpoint}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                latency = (time.monotonic() - t0) * 1000
                data = await resp.json()
                if resp.status != 200:
                    return CompletionResponse(
                        content="",
                        provider=provider,
                        model=model,
                        tokens_used=0,
                        latency_ms=latency,
                        error=f"HTTP {resp.status}: {data}",
                    )
                content     = data["choices"][0]["message"]["content"]
                tokens_used = data.get("usage", {}).get("total_tokens", 0)
                return CompletionResponse(
                    content=content,
                    provider=provider,
                    model=model,
                    tokens_used=tokens_used,
                    latency_ms=latency,
                )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            return CompletionResponse(
                content="",
                provider=provider,
                model=model,
                tokens_used=0,
                latency_ms=latency,
                error=str(exc),
            )

    async def stream(
        self,
        req: CompletionRequest,
    ) -> AsyncGenerator[str, None]:
        """Streaming completion — yields content chunks."""
        provider = self._pick_provider(req.provider)
        endpoint = PROVIDER_ENDPOINTS[provider]
        model    = req.model or PROVIDER_MODELS[provider]
        key      = self._keys.for_provider(provider)

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider == Provider.NEXOS and key:
            headers["X-Api-Key"] = key

        from core.orientation import orient_messages
        oriented = orient_messages(req.messages, req.metadata.get("interaction_profile"))
        raw_msgs2: list[dict] = [{"role": m.role, "content": m.content} for m in oriented]
        raw_tools2 = req.tools or []

        cache2 = PromptCacheLayer(
            enabled     = req.enable_caching and provider != Provider.XAI,
            default_ttl = CacheTTL.EPHEMERAL,
        )
        cached_msgs2, cached_tools2 = cache2.annotate(
            messages        = raw_msgs2,
            tools           = raw_tools2,
            retrieved_items = req.retrieved_items,
            model           = model,
        )
        headers.update(cache2.all_extra_headers())

        payload: dict = {
            "model":       model,
            "messages":    cached_msgs2,
            "max_tokens":  req.max_tokens,
            "temperature": req.temperature,
            "stream":      True,
        }
        if cached_tools2:
            payload["tools"] = cached_tools2

        if self._session is None:
            raise RuntimeError("APIProvisioner.start() must be called before stream()")
        async with self._session.post(
            f"{endpoint}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    return
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError):
                    continue

    # ── Health refresh ────────────────────────────────────────────────────────

    async def refresh_health(self) -> dict[Provider, ProviderHealth]:
        self._health = await health_check_all(self._keys)
        return self._health

    @property
    def health(self) -> dict[Provider, ProviderHealth]:
        return self._health


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def main():
        prov = APIProvisioner.from_env()
        await prov.start()

        print("[api_provision] health check:")
        for p, h in prov.health.items():
            print(f"  {p.name:8s}  reachable={h.reachable}  latency={h.latency_ms:.0f}ms  {h.error}")

        await prov.stop()

    asyncio.run(main())
