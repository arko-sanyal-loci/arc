# Arc and Claude capability matrix

Updated: 2026-11-23

This is a product-surface comparison, not a claim that Arc reproduces
Claude's private implementation. Claude's public Artifacts documentation
describes a side-by-side surface for documents, code, dashboards, interactive
tools, uploads, and MCP-connected applications:

<https://support.claude.com/en/articles/17153992-what-are-artifacts-and-how-do-i-use-them>

## Current comparison

| Capability | Claude surface | Arc status | Gap |
|---|---|---|---|
| Chat | Text chat with streaming and model selection | Present: REST and WebSocket chat | Improve conversation UX and error states |
| Persistent memory | Memory across conversations and project context | Present: LOCI/Glassbox bridge and session continuity | Add user-visible memory controls and provenance views |
| Projects | Durable project files and instructions | Partial: workspace and project-oriented backend surfaces | Add project creation, instructions, file scope, and switching UI |
| Artifacts/workbench | Dedicated editable output surface beside chat | Partial: existing workbench/details area | Add versioned artifact documents, previews, and export/share |
| MCP/connectors | Permissioned MCP and app connections | Partial: MCP endpoint and bounded local tools | Add connector registry, scopes, approval inbox, and audit display |
| Computer/desktop work | Delegated multi-step work with human control | Partial: bounded workspace/browser capabilities | Add task plans, progress graph, and explicit side-effect approvals |
| Research | Multi-source web and connected-app research | Partial: provider fallback and local tooling | Add a first-class research run with citations and source status |
| Voice | Conversation voice input/output | Partial: VPS push-to-record and browser speech synthesis | Add workstation voice session UX and consent state |
| Image/video generation | Provider-dependent media generation | Contract and media panel exist, no native generator wired | Add local/provider adapters and result cards |
| Multi-agent execution | Delegated parallel tasks and subtasks | Status lanes exist; orchestration is not yet a full task graph | Add durable task graph, lane assignment, and cancellation |
| Safety/approvals | Permission prompts and workspace controls | Bounded tools and sensitive-action prompts exist | Enforce one shared gate across every side-effect route |

## Recommended implementation order

1. Projects plus artifact/workbench versions.
2. A shared permission and approval inbox for workspace, browser, MCP, and
   media actions.
3. Durable multi-agent task graph with fast, deep, research, and outbound
   lanes.
4. Media provider adapters and local capability detection.
5. Voice session UX and workstation notifications.

## VPS fit

Hostinger's current entry KVM VPS listing advertises 1 vCPU, 4 GB RAM, 50 GB
NVMe, and weekly backups. It is suitable for Arc's continuity chat, encrypted
journal, Caddy/TLS, health/status endpoints, and a small relay or API gateway.
It is not a suitable host for Arc's RTX/Intel GPU engines: the ordinary KVM
plan has no attached GPU and the CPU/RAM budget is too small for the
workstation models.

Keep GPU inference on the workstation or a separate GPU provider. The VPS can
support Arc by:

- serving the low-resource continuity chat;
- retaining encrypted session and dispatch records;
- exposing authenticated health and routing status;
- relaying requests to a deliberately configured outbound GPU endpoint;
- refusing local model startup when no suitable backend is present.

Do not install the full workstation dependency set, CUDA, Intel XPU, vLLM,
SGLang, or video-generation stacks on the low-tier VPS. Those would consume
the available disk/RAM without providing usable acceleration.
