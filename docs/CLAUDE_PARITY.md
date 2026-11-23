# Desktop parity boundary

Arc targets the useful, user-visible properties of a modern desktop AI
workspace: a persistent local runtime, conversation memory, bounded coding
tools, MCP interoperability, a Chromium browser workspace, multimodal
extension points, and a single packaged launcher.

This is a behavior-level design reference, not a reconstruction of Claude
Desktop. Arc uses its own UI, runtime, prompts, storage, and orchestration.
The browser is an Electron/Chromium surface with explicit navigation and
permission boundaries. The Arc runtime remains loopback-first and keeps
provider credentials outside the package.

The main parity gaps are tracked honestly: native music tooling, true backend
KV paging, native macOS/Windows builds, and authorized external quality
benchmarks are not declared complete until validated.
