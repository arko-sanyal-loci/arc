# Mr Calendar recall and provenance audit — 2026-11-20

## Scope

This audit exercised the local Mr Calendar recall path against the 10-section
`WORKSPACE-MAP.md` source. The raw ChatGPT conversation export was not present,
so this is not a claim that every original message or attachment was tested.
The machine-readable results are in `results/mr-calendar-recall-2026-11-20.json`.

## Verified

- All four recall categories returned the expected depth: information extraction
  5, knowledge update 3, temporal reasoning 7, and multi-session reasoning 10.
- Retrieved evidence matched the source SHA-256. Broad recall returned all 10
  source sections. Re-indexing remained idempotent at 10 rows.
- Representative compression estimates were 764→330, 468→329, 1018→329,
  and 1367→331 tokens. These are estimates from this implementation, not
  independently measured model-token counts.
- Scene-neighbor retrieval returned all 4 entries in the small local fixture.
- Embedding service discovery uses the live Arc port 8000 then RTX port 11435;
  the old 11434 assumption failed because no service was listening there.
- Mr Calendar's CPU fallback now targets the running local Ollama endpoint on
  11435 with `num_gpu=0`; GPU-first requests use the adaptive gateway on 11437.
- Arc monitor service is enabled and active at `http://127.0.0.1:11500/`.
  Its API sees both model lanes, two camera device nodes, and three suspended
  audio inputs. No capture process or active audio stream was detected.
- The camera was previously tested with one bounded frame on `/dev/video0`;
  `/dev/video1` did not act as a capture source. The current HUD is a status
  monitor and preview, not an Arc video/audio stream.
- Provenance tests and five selected LOCI engine suites passed: 55 tests, with
  17 upstream asyncio deprecation warnings. Mr Calendar recall tests: 10 passed.
  Arc monitor tests: 3 passed.

## Provenance conclusion

Audio, video, timing, and interaction factors can improve an opt-in session
profile and retrieval ranking, but pattern matching them is not a reliable
identity credential. Use a public-key fingerprint as the authenticated actor
identifier; keep chosen display names separate; make sensory profiles
consented, short-lived, and non-authenticating. The design and limits are in
`PROVENANCE-PROFILE-DESIGN.md`. Hashes provide content integrity checks, not
proof that a sensor observed a real-world event. Actor signatures and an
append-only event chain are not implemented yet.

## Remaining gaps

- Obtain a user-provided raw transcript export before claiming full-conversation
  recall coverage.
- Build a signed event envelope and key rotation chain before claiming
  cryptographic provenance.
- The running HUD does not start capture, show a remote peer, or verify that an
  agent is watching/listening. Mic capture remains off.
- The independent Arc repository contains this design and results; the live
  Mr Calendar implementation remains in the workspace tree. Its source will be
  preserved separately in the dated local/VPS backup bundle.
