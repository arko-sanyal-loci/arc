# Arc Mobile + Web App Architecture

**Version:** 0.1 — 2026-11-18  
**Status:** Planning  
**Scope:** Web SPA + iOS (Siri/ANE) + Android — client layer over arc-runtime backend

---

## Stack Decision

| Layer | Choice | Rationale |
|---|---|---|
| Cross-platform mobile | React Native 0.76+ | Single codebase, native bridges for Swift/Kotlin, New Architecture JSI for low-latency |
| iOS native bridge | Swift 6 + AppIntents | Siri integration requires AppIntents; CoreML for ANE embeddings |
| Android native bridge | Kotlin + WorkManager | Background inference scheduling |
| Web SPA | Vite + React 18 | Shared component library with RN Web |
| Backend | arc-runtime (FastAPI) | Already built; Provider.LOCAL → Ollama |
| Realtime | WebSocket via FastAPI | Streaming tokens to all clients |

---

## iOS: Siri + Apple Neural Engine

### Siri Integration
```
User: "Hey Siri, ask Arc about [topic]"
  → AppIntent fires: ArcQueryIntent(query: String)
  → Passes to ArcBridge.swift
  → HTTP POST http://localhost:arc-runtime/v1/chat  (on-device or LAN)
  → Returns INSpeakableString back to Siri
```

**AppIntents manifest** (`ArcIntents.swift`):
```swift
struct ArcQueryIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Arc"
    static var description = IntentDescription("Query the Arc AI engine")

    @Parameter(title: "Query") var query: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let response = try await ArcClient.shared.query(query)
        return .result(dialog: IntentDialog(stringLiteral: response))
    }
}
```

### Nomic Embed on Apple Neural Engine (CoreML)

**Goal:** Run `nomic-embed-text-v1.5` locally on the ANE at ~10ms/token vs ~100ms on CPU.

**Pipeline:**
1. Export `nomic-ai/nomic-embed-text-v1.5` from HuggingFace → ONNX
2. Convert ONNX → CoreML via `coremltools 7+`
3. Set `compute_units=ComputeUnit.ALL` → ANE picks up the transformer attention blocks
4. Bundle as `NomicEmbed.mlpackage` in the iOS app target
5. At runtime: `MLModel.prediction()` → 768-dim float32 embedding

**Conversion script** (`scripts/convert_nomic_coreml.py`):
```python
import coremltools as ct
import torch
from transformers import AutoModel, AutoTokenizer

model_id = "nomic-ai/nomic-embed-text-v1.5"
model    = AutoModel.from_pretrained(model_id, trust_remote_code=True)
tokenizer= AutoTokenizer.from_pretrained(model_id)

# Trace with dummy input (seq_len=128)
example_input = tokenizer("arc runtime test", return_tensors="pt", max_length=128, padding="max_length")

traced = torch.jit.trace(model, (example_input["input_ids"], example_input["attention_mask"]))

# Convert → CoreML, target ANE
ml_model = ct.convert(
    traced,
    inputs=[
        ct.TensorType(name="input_ids",      shape=(1, 128), dtype=int),
        ct.TensorType(name="attention_mask", shape=(1, 128), dtype=int),
    ],
    compute_units=ct.ComputeUnit.ALL,  # GPU + ANE
    minimum_deployment_target=ct.target.iOS17,
)
ml_model.save("NomicEmbed.mlpackage")
```

**Expected performance on iPhone 15 Pro (A17 Pro ANE @ 35 TOPS):**
- ~8-12ms per 128-token embedding
- vs ~90ms on CPU-only
- 768-dim output identical to server-side Nomic

---

## Multi-GPU Inference Routing (arc-runtime side)

Already implemented in `core/api_provision.py`:

```
Provider.LOCAL  → Ollama @ localhost:11434  (Arc B70 + RTX 5060 Ti)
Provider.NEXOS  → nexos.ai API
Provider.XAI    → Grok failsafe
```

**New: task-based GPU routing**

| Task type | Model | GPU |
|---|---|---|
| Embedding | nomic-embed-text | Ollama (auto) |
| Fast tool use / chat | llama3.1:8b-q8_0 | RTX 5060 Ti (CUDA) |
| Deep reasoning / coding | llama3.1:70b-q4_K_M | Arc B70 + RTX 5060 Ti (VRAM split) |
| Cloud fallback | grok-2 / claude-3-5 | NEXOS / XAI |

Routing logic → `core/api_provision.py` `_select_provider()` extension (Phase 2).

---

## Android

Same React Native codebase. Native bridge handles:
- WorkManager for background embedding jobs
- Room DB as local calendar_memory mirror
- No Siri equivalent — custom wake word or notification-based trigger

---

## Web SPA

- Vite + React 18, TailwindCSS
- WebSocket connection to arc-runtime for streaming
- IndexedDB mirrors `calendar_memory` entries for offline mode
- Shared with React Native Web for desktop browser fallback

---

## Next Steps (Priority Order)

1. **P1:** `scripts/convert_nomic_coreml.py` — CoreML pipeline + `.mlpackage` bundle
2. **P1:** `mobile/ios/ArcIntents.swift` — AppIntents for Siri
3. **P2:** `mobile/` — React Native scaffold (`npx react-native init ArcApp`)
4. **P2:** `app/ws.py` — WebSocket streaming endpoint in arc-runtime
5. **P3:** `mobile/android/` — Kotlin WorkManager bridge
6. **P3:** Web SPA deploy to `billo.tech/arc`

---

## Model Wrapper: CONFIRMED — Ollama

`Provider.LOCAL` in `core/api_provision.py` points to `http://localhost:11434/v1`.  
Decision: Ollama. No custom wrapper needed.

Rationale:
- Already baked into arc-runtime
- Supports CUDA (RTX) + experimental Intel SYCL (Arc B70)
- OpenAI-compatible API → zero friction with existing `api_provision.py`
- 48 GB aggregate VRAM fits llama3.1:70b Q4_K_M (40.4 GB)
- Mobile clients hit arc-runtime over LAN; they don't run Ollama directly
