# Agent feedback and semantic streams

## Feedback panel

The Arc workbench exposes an **Agent feedback** panel in the existing chat UI.
Agents can submit an observation, question, suggestion, or issue with a project
label. Entries are stored append-only in `data/agent_feedback.sqlite3` on the
workstation. The `/api/feedback` write route accepts loopback clients only; do
not expose it through a public reverse proxy without adding authentication,
CSRF protection, and abuse controls. Feedback is operational context and is not
automatically treated as a verified fact or ingested into long-term memory.

## Semantic stream index

The VPS runs Ollama bound to `127.0.0.1:11434` with `nomic-embed-text`. From a
VPS shell, agents can POST to `/api/embed` with
`{"model":"nomic-embed-text","input":"search_query: ..."}` for retrieval
queries and `search_document: ...` when indexing documents. Arc provides
`POST /api/streams/index` and `GET /api/streams/search` over this service. Each
index request must name a `stream_id`, `source_id`, `source_label`, and one of
`user_supplied`, `explicit_opt_in`, or `derived_metadata` for `consent_scope`.
It may include `source_sha256`, `start_ms`, `end_ms`, and `frame_references`.
Each submission appends a version; SQLite triggers prevent updates and deletes.
Search embeds the query with `search_query`, scores the latest version of each
stream by cosine similarity, and returns excerpts plus exact source/time/frame
references. Retrieve the source segment before relying on a result. Re-index
when source text or model changes. Never put raw private media in the public
Git repository.

Search returns matches at or above cosine similarity `0.2` by default. Agents
may pass `min_similarity` to tune precision versus recall and should inspect the
returned source references before using a match.

Arc exposes the same operations to connected local MCP clients as
`arc_stream_index` and `arc_stream_search`. Ordinary indexing remains an
explicit agent action. The separately user-started A/V capture flow locally
transcribes short microphone chunks, samples camera frames, runs Garena motion
and detail-tile selection, and asks the workstation Qwen-VL model to describe
the selected image regions. Raw media stays transient. On Stop, derived
transcripts, scene summaries, motion coordinates, and timestamps are appended
to this journal with `explicit_opt_in` consent, then included in the encrypted
memory sync. The A/V preview remains off until the user starts it.

On the workstation, Arc uses local Ollama at `http://127.0.0.1:11435/api/embed`
first. The local Nomic model reports 768 dimensions and about 336 MB resident in
VRAM. If the local endpoint is unavailable, Arc falls back to the authenticated
SSH tunnel at `http://127.0.0.1:11438/api/embed`. The persistent tunnel binds
only to workstation loopback; its VPS-side SSH key is restricted to forwarding
only to `127.0.0.1:11434`. The Arc service exports
`ARC_NOMIC_EMBED_BASE_URL` and `ARC_NOMIC_FALLBACK_URL`. The VPS itself is not
exposed publicly.

The VPS has CPU-only inference (two vCPU and 7.7 GiB RAM); it hosts a compact
embedding endpoint but does not provide GPU token offload. The workstation
uses its GPU-resident local Nomic model for embedding and Qwen-VL for local
frame analysis. The A/V panel uses local faster-whisper on CPU for speech
transcription. Model KV/token offload and the proposed TRS cross-GPU scheduler
remain separate work. Do not bind either Ollama endpoint to a public interface.

## Stream/TRS status

TRS still describes the proposed cross-GPU stream/relevance runtime; that part
is not deployed. Arc has a Nomic semantic index for explicitly submitted text
segments and metadata, plus a separate opt-in local A/V path that extracts
transcripts, vision descriptions, and motion features. A/V-derived summaries
enter the semantic journal only when the user stops the capture session. No
automatic device recording is enabled. Evidence intake processes only files
the user places in `~/Downloads/Evidence-Intake`.

The semantic journal is included in the existing private memory sync as a
SQLite-consistent snapshot, encrypted with age before it is versioned in the
private `arc-private-memory` repository. The source system is not yet subscribed
to the coordination bus: agents explicitly call `arc_stream_index` for a
consented transcript/metadata segment or task delta. See the [Director delta,
heartbeat, and clock-safety design](https://github.com/arko-sanyal-loci/LOCI-MEMORY-ENGINE/blob/main/docs/DIRECTOR-DELTA-AND-TIME.md).

The local A/V path is wired to this versioned vector index through a
user-started session's derived text and metadata. No TRS cross-GPU scheduler or
multi-agent live-delta bus is deployed. Keep indexing and GPU work observable
and cancellable. Never infer identity, emotion, or a personal profile from
voice/video unless the user has explicitly enabled a narrowly scoped,
documented feature.

## Validation and launch

Verify the agent feedback panel and semantic search from the workstation UI.
To index a stream from an agent, POST JSON to `/api/streams/index`; to retrieve,
GET `/api/streams/search?q=...`. Verify the VPS model with
`curl http://127.0.0.1:11434/api/embed` from a VPS shell or with
`curl http://127.0.0.1:11435/api/embed` on the workstation, or use
`http://127.0.0.1:11438/api/embed` as the secured VPS fallback. Both endpoints
are intentionally inaccessible from the public internet.
