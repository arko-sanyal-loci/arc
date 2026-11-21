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

The VPS runs Ollama bound to `127.0.0.1:11434` with `nomic-embed-text`. Agents
should use `/api/embed` with an explicit task prefix, persist vectors with model
and source-version metadata, and retrieve source references before making a
claim. Keep source IDs, timestamps, consent scope, and content hashes alongside
each vector. Rebuild the index when the source text or embedding model changes.
Never put raw private media in the public Git repository.

The VPS has CPU-only inference (two vCPU and 7.7 GiB RAM); it hosts a compact
embedding endpoint but does not provide GPU token offload. Workstation RTX/Arc
execution remains the candidate for GPU-heavy embedding batches, audio/video
feature extraction, or model KV/token offload. Route only through a private,
authenticated tunnel. Do not bind Ollama to a public interface.

## Stream/TRS status

TRS currently describes a proposed stream/relevance architecture. It is not a
deployed cross-GPU stream runtime. The current Arc memory search route uses SQL
substring matching, not Nomic vector search. The existing browser A/V workbench
shows a user-started local preview; it does not extract semantic features or
send camera/microphone content to the model. No automatic device recording is
enabled. Evidence intake processes only files the user places in
`~/Downloads/Evidence-Intake`.

To extend this safely, implement a versioned stream record containing source,
time range, consent scope, content hash, derived text/features, embedding model
and vector reference. Add a retrieval API that returns exact source/time/frame
references, then let the agent request only relevant stream segments. Keep
indexing and GPU work observable and cancellable. Never infer identity,
emotion, or a personal profile from voice/video unless the user has explicitly
enabled a narrowly scoped, documented feature.

## Validation and launch

After deployment, verify the panel by submitting a test entry from the
workstation UI and checking that it appears after refresh. Verify the VPS model
with `curl http://127.0.0.1:11434/api/embed` from a VPS shell or authenticated
SSH session. The VPS endpoint is intentionally inaccessible from the public
internet.
