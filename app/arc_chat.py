"""
arc_chat.py — Arc Chat: Universal Hub Interface

The central router between all LOCI agents and models.
Positioned "in between" — receives any input, routes to the right lane,
returns a unified response.

                    ┌─────────────────┐
                    │   Arc Chat Hub   │  ← You talk here
                    └────────┬────────┘
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Arc B70         RTX 5060Ti      Semantic
      (deep lane)      (fast lane)     Constant
      level_zero:0      CUDA:0         Filter
      port 8000         port 11434
      COMPLEX/          TRIVIAL/        H ∈ [3.2, 5.8]
      SOVEREIGN         MODERATE

The semantic constant filter sits at the entry point:
  - If token entropy of the input is in the semantic range → route to intelligence
  - If below range (mundane) → fast lane only
  - If above range (noise/incoherent) → clarify before routing

Usage:
    # Terminal chat
    python app/arc_chat.py

    # API server (FastAPI)
    uvicorn app.arc_chat:app --host 0.0.0.0 --port 9000

    # Single message
    python app/arc_chat.py --message "explain the semantic constant"
"""

import os
import sys
import math
import zlib
import json
import logging
import asyncio
import argparse
from pathlib import Path
from typing import AsyncIterator, Optional
from dataclasses import dataclass
from enum import Enum

import httpx

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("arc_chat")

# ── Lane config ───────────────────────────────────────────────────────────────
ARC_URL    = os.environ.get("ARC_INFERENCE_URL",  "http://localhost:8000")
RTX_URL    = os.environ.get("RTX_INFERENCE_URL",  "http://localhost:11434")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Load semantic constant from measurement if available
_CONST_PATH = Path("/data/cache/semantic_constant/semantic_constant.json")
_H_LOW  = 3.2   # default hypothesis
_H_HIGH = 5.8
if _CONST_PATH.exists():
    try:
        with open(_CONST_PATH) as f:
            _c = json.load(f)
        _H_LOW  = _c.get("h_p5",  _H_LOW)
        _H_HIGH = _c.get("h_p95", _H_HIGH)
        log.info(f"Loaded semantic constant: H ∈ [{_H_LOW:.3f}, {_H_HIGH:.3f}] bits/tok")
    except Exception:
        pass


class Lane(str, Enum):
    ARC   = "arc"      # deep reasoning, long context, COMPLEX/SOVEREIGN
    RTX   = "rtx"      # fast inference, TRIVIAL/MODERATE
    BOTH  = "both"     # speculative: RTX drafts, Arc verifies
    CLAUDE = "claude"  # API fallback when neither GPU is up


@dataclass
class RouteDecision:
    lane: Lane
    reason: str
    entropy: float
    complexity: str


# ══════════════════════════════════════════════════════════════════════════════
#  SEMANTIC ROUTER — the antigravity layer
# ══════════════════════════════════════════════════════════════════════════════

def _char_entropy(text: str) -> float:
    """Shannon entropy of character distribution, in bits."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for c in text:
        counts[c] = counts.get(c, 0) + 1
    n = len(text)
    return -sum((v/n) * math.log2(v/n) for v in counts.values())


def _token_entropy(text: str) -> float:
    """Approximate token-level entropy (chars * 4.3 chars/token scaling)."""
    return _char_entropy(text) * 4.3


def _complexity_grade(text: str) -> tuple[str, float]:
    """
    Grade input complexity using the semantic constant.
    Returns (grade, entropy).

    TRIVIAL:   H < H_LOW           → mundane, fast lane sufficient
    MODERATE:  H_LOW ≤ H < 0.75*H_HIGH → standard semantic content
    COMPLEX:   0.75*H_HIGH ≤ H ≤ H_HIGH → rich semantic content
    SOVEREIGN: H > H_HIGH OR long → requires deep lane alone
    """
    h = _token_entropy(text)
    n = len(text)

    if n > 2000:
        return "SOVEREIGN", h
    if h < _H_LOW:
        return "TRIVIAL", h
    if h < 0.75 * _H_HIGH:
        return "MODERATE", h
    if h <= _H_HIGH:
        return "COMPLEX", h
    return "SOVEREIGN", h


def route(message: str) -> RouteDecision:
    """
    Route a message to the right GPU lane based on the semantic constant.
    This is the antigravity layer — lifts the message above modality into
    pure semantic space, then routes by density.
    """
    grade, h = _complexity_grade(message)
    lane_map = {
        "TRIVIAL":   Lane.RTX,
        "MODERATE":  Lane.BOTH,
        "COMPLEX":   Lane.BOTH,
        "SOVEREIGN": Lane.ARC,
    }
    reason_map = {
        "TRIVIAL":   f"H={h:.2f} < {_H_LOW:.2f} (mundane) → fast lane",
        "MODERATE":  f"H={h:.2f} in semantic range → speculative dual-lane",
        "COMPLEX":   f"H={h:.2f} in semantic range → speculative dual-lane",
        "SOVEREIGN": f"H={h:.2f} > {_H_HIGH:.2f} or long → deep lane only",
    }
    return RouteDecision(
        lane=lane_map[grade],
        reason=reason_map[grade],
        entropy=h,
        complexity=grade,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  INFERENCE CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

async def call_arc(message: str, context: list[dict]) -> AsyncIterator[str]:
    """Stream response from Arc B70 inference server (port 8000)."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        payload = {
            "messages": context + [{"role": "user", "content": message}],
            "stream": True,
            "max_tokens": 2048,
        }
        configured_model = os.environ.get("ARC_MODEL")
        if configured_model:
            payload["model"] = configured_model
        try:
            async with client.stream("POST", f"{ARC_URL}/v1/chat/completions",
                                      json=payload) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        chunk = line[6:]
                        if chunk.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                            token = data["choices"][0]["delta"].get("content", "")
                            if token:
                                yield token
                        except Exception:
                            continue
        except (httpx.ConnectError, httpx.TimeoutException):
            yield "[Arc B70 offline — falling back to Claude API]"
            async for tok in call_claude_api(message, context):
                yield tok


async def call_rtx(message: str, context: list[dict]) -> AsyncIterator[str]:
    """Stream response from RTX Ollama server (port 11434)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        payload = {
            "model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
            "messages": context + [{"role": "user", "content": message}],
            "stream": True,
        }
        try:
            async with client.stream("POST", f"{RTX_URL}/api/chat",
                                      json=payload) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        token = data.get("message", {}).get("content", "")
                        if token:
                            yield token
                        if data.get("done"):
                            break
                    except Exception:
                        continue
        except (httpx.ConnectError, httpx.TimeoutException):
            yield "[RTX offline — falling back to Claude API]"
            async for tok in call_claude_api(message, context):
                yield tok


async def call_claude_api(message: str, context: list[dict]) -> AsyncIterator[str]:
    """Stream response from Anthropic API (fallback when GPUs are down)."""
    if not ANTHROPIC_KEY:
        yield "[No ANTHROPIC_API_KEY set — set it to use Claude API fallback]"
        return
    async with httpx.AsyncClient(timeout=120.0) as client:
        headers = {
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2048,
            "stream": True,
            "system": (
                "You are the LOCI Arc Chat — the central intelligence hub of the "
                "LOCI multi-agent system built on Arc B70 + RTX 5060 Ti. "
                "Respond with precision. This is the antigravity layer — "
                "lift every query to its semantic core before answering."
            ),
            "messages": context + [{"role": "user", "content": message}],
        }
        async with client.stream("POST",
                                  "https://api.anthropic.com/v1/messages",
                                  headers=headers, json=payload) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if data.get("type") == "content_block_delta":
                            yield data["delta"].get("text", "")
                    except Exception:
                        continue


async def route_and_respond(
    message: str,
    context: list[dict],
    verbose: bool = False,
) -> AsyncIterator[str]:
    """Route message and stream response from the right lane."""
    decision = route(message)
    if verbose:
        print(f"\n  [route] {decision.complexity} | {decision.reason}")

    if decision.lane == Lane.RTX:
        async for tok in call_rtx(message, context):
            yield tok

    elif decision.lane == Lane.ARC:
        async for tok in call_arc(message, context):
            yield tok

    elif decision.lane == Lane.BOTH:
        # Speculative: try RTX first (fast), if it stutters fall to Arc
        got_tokens = False
        async for tok in call_rtx(message, context):
            if "[RTX offline" in tok:
                async for arc_tok in call_arc(message, context):
                    yield arc_tok
                return
            yield tok
            got_tokens = True
        if not got_tokens:
            async for tok in call_arc(message, context):
                yield tok

    else:  # Claude API
        async for tok in call_claude_api(message, context):
            yield tok


# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI SERVER (for agents / other systems to call)
# ══════════════════════════════════════════════════════════════════════════════

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel

    app = FastAPI(title="Arc Chat Hub", version="0.1.0")

    class ChatRequest(BaseModel):
        message: str
        context: list[dict] = []
        verbose: bool = False

    @app.post("/chat")
    async def chat_endpoint(req: ChatRequest):
        async def generate():
            async for token in route_and_respond(req.message, req.context, req.verbose):
                yield token

        return StreamingResponse(generate(), media_type="text/plain")

    @app.post("/route")
    async def route_endpoint(req: ChatRequest):
        decision = route(req.message)
        return {
            "lane": decision.lane.value,
            "reason": decision.reason,
            "entropy": decision.entropy,
            "complexity": decision.complexity,
            "h_range": [_H_LOW, _H_HIGH],
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "h_low": _H_LOW, "h_high": _H_HIGH}

except ImportError:
    app = None
    log.warning("FastAPI not installed — server mode unavailable. pip install fastapi uvicorn")


# ══════════════════════════════════════════════════════════════════════════════
#  TERMINAL CHAT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_BANNER = """
╔══════════════════════════════════════════════════════╗
║           LOCI Arc Chat — Universal Hub              ║
║                                                      ║
║  Arc B70   (deep)  → COMPLEX / SOVEREIGN queries     ║
║  RTX 5060 (fast)  → TRIVIAL / MODERATE queries       ║
║  Semantic constant H ∈ [{lo:.2f}, {hi:.2f}] bits/tok          ║
║                                                      ║
║  Type 'exit' to quit  |  '/route <msg>' to debug     ║
╚══════════════════════════════════════════════════════╝
""".format(lo=_H_LOW, hi=_H_HIGH)


async def terminal_chat(single_message: Optional[str] = None) -> None:
    print(SYSTEM_BANNER)
    context: list[dict] = []

    async def process(msg: str) -> None:
        decision = route(msg)
        print(f"  → [{decision.complexity}] {decision.lane.value.upper()} "
              f"| H={decision.entropy:.2f} bits/tok", flush=True)
        print()

        full_response = []
        async for token in route_and_respond(msg, context):
            print(token, end="", flush=True)
            full_response.append(token)
        print("\n")

        context.append({"role": "user",    "content": msg})
        context.append({"role": "assistant", "content": "".join(full_response)})
        # Keep context window reasonable
        if len(context) > 20:
            context.pop(0)
            context.pop(0)

    if single_message:
        await process(single_message)
        return

    while True:
        try:
            msg = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not msg:
            continue
        if msg.lower() in ("exit", "quit", "q"):
            print("Bye.")
            break
        if msg.startswith("/route "):
            test = msg[7:]
            d = route(test)
            print(f"  Lane: {d.lane.value}  |  Complexity: {d.complexity}  "
                  f"|  H={d.entropy:.3f}  |  {d.reason}")
            continue
        await process(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Arc Chat — LOCI universal hub")
    parser.add_argument("--message", "-m", default=None,
                        help="Single message (non-interactive)")
    parser.add_argument("--port", type=int, default=9000,
                        help="Port for API server mode")
    parser.add_argument("--serve", action="store_true",
                        help="Run as FastAPI server")
    args = parser.parse_args()

    if args.serve:
        if app is None:
            print("Install fastapi + uvicorn: pip install fastapi uvicorn")
            sys.exit(1)
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=args.port)
    else:
        asyncio.run(terminal_chat(args.message))


if __name__ == "__main__":
    main()
