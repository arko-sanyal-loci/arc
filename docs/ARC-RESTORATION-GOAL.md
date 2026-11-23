# Arc restoration goal

Status: staged implementation with decision gates
Updated: 2026-11-23

## Target

Restore Arc to a Claude-level or better assistant experience while preserving
LOCI's local-first memory, provenance, heat routing, GPU lanes, and bounded
tool execution.

“Better” is evaluated by measurable behavior rather than feature count:

- useful answers remain available when outbound services are unavailable;
- memory recall is provenance-aware, inspectable, and does not silently invent
  facts;
- tools require bounded scopes and approval for consequential side effects;
- local compute is selected by task complexity and measured health;
- voice is responsive, expressive, interruptible, and consent-gated;
- work can continue across sessions without exposing private state;
- the VPS remains useful on low resources without pretending to provide GPU
  inference.

## Milestones and stop gates

### 1. Evidence inventory

Map Arc, LOCI, shader, voice, GPU, VPS, GitHub, and currently exposed MCP
assets. Stop if an artifact's provenance, permissions, or ownership is unclear.

### 2. Capability parity

Maintain the Claude comparison matrix for chat, projects, artifacts,
connectors, research, delegated work, memory, voice, media, and approvals.
Stop before implementing a feature whose safety or permission model is
ambiguous.

### 3. Arc-native implementation

Implement in small slices:

1. project context and versioned workbench artifacts;
2. one shared approval/provenance gate;
3. durable multi-agent task graph;
4. measured RTX/Intel routing and startup;
5. expressive local voice with interruption and consent;
6. image/video capability adapters;
7. low-tier VPS continuity and relay support.

Each slice must have a focused test and a rollback-safe checkpoint.

### 4. Validation

Run the full Arc test suite, targeted UI and service checks, GPU lane
benchmarks, voice latency/consent checks, and VPS resource checks. Stop and
request a decision when measured behavior conflicts with a product or safety
tradeoff.

## Parallel-work rule

While a milestone is waiting for a decision, continue only independent,
reversible work such as evidence indexing, test coverage, documentation,
benchmark harnesses, and capability detection. Do not silently choose a
product, privacy, access, or safety policy on the user's behalf.

## Current constraints

- Drive and Gmail MCP connectors are not exposed in the current session.
- The low-tier Hostinger VPS is a continuity/relay target, not a GPU target.
- The local GPU service topology includes raw Ollama, an Intel inference
  service, and an optional gateway/tunnel; endpoint alignment must be measured
  before changing routing.
