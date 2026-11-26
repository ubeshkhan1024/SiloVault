# SiloVault Lightweight AI Model Catalogue

## Implementation Status (26 November 2026)

This document is a model-selection catalogue, not a statement of delivered capability. Current repository state:

- **Florence-2 runtime:** `backend/ai/florence2_runtime.py` implements the runtime and is initialized by `backend/ai/bootstrap.py` during AI bootstrap. It is **not** usable without optional `torch`/`transformers` packages and downloaded model weights; neither is bundled here.
- **Executable capabilities:** an AI capability is executable only when `can_execute_capability(...)` reports true, and `build_task_processors` registers a processor only in that case. With no model installed the mapping is empty and AI tasks remain `PENDING`.
- **End-to-end AI processing:** not functional. No capability has been verified with real weights.
- **OCR:** the OCR runtime and Tesseract adapter exist but are not wired into task processing.
- **Pack installation:** `PackManager` is a read-only foundation; there is no downloader, checksum-verified install, or pack UI.

See the AI model-runtime section of [ARCHITECTURE.md](ARCHITECTURE.md) §7.12 for the per-capability status table.

## Executive Summary
This catalogue provides a modular, offline-capable selection of lightweight AI models for SiloVault. AI is optional; SiloVault remains fully functional without any AI models installed. All processing happens locally on the primary machine (Windows laptop).

**Core Principles:**
- No model weights bundled with the core application.
- Optional AI packs (lazy loading).
- CPU-first execution (float32).
- Capability-specific adapters (no runtime redesign).
- SiloVault remains useful without AI installed.

## Model Comparison

| Model | Params | Weight Size (FP16/Theoretical) | License | Tasks Supported |
|---|---|---|---|---|
| **Florence-2 Base** | 232M (232,844,395) | ~0.4 GB (FP32: ~931.4 MB / 888 MiB) | MIT | `<DETAILED_CAPTION>` |
| **Florence-2 Base-FT** | ~230M | ~0.4 GB | MIT | `<CAPTION>`, `<OD>`, `<OCR>` |
| **Florence-2 Large** | 771M | ~1.4 GB | MIT | `<CAPTION>`, `<OD>` |
| **Florence-2 Large-FT** | ~770M | ~1.4 GB | MIT | `<CAPTION>`, `<OD>`, `<OCR>` |
| **SmolVLM-256M** | 256M | ~0.5 GB | Apache 2.0 | Captioning, VQA |
| **SmolVLM2-500M** | 500M | ~0.6 GB | Apache 2.0 | Captioning, VQA |
| **Moondream 2** | 1.6B | ~1.2 GB | Apache 2.0 | Captioning, VQA |
| **MobileCLIP** | Base: ~150M; Large: ~300M | Base: ~0.2 GB; Large: ~0.5 GB | Code: MIT; Models: Apple ML Research Model Terms | Similarity, Embedding |
| **MobileNetV3 Small** | 2,542,856 | ~9.8 MB | Apache 2.0 | Classification |
| **MobileNetV3 Large** | 5,483,032 | ~21.1 MB | Apache 2.0 | Classification |

*Note: FP32 size for Florence-2 Base is derived from the exact parameter count (232,844,395 × 4 bytes = 931,377,580 bytes). FP16 weight-file sizes are theoretical estimates. Actual download sizes and runtime RAM usage are unknown until benchmarked.*

## Capability & Runtime
- **Florence2Runtime**: Currently hardcodes `<DETAILED_CAPTION>`. OCR and object detection are model-supported but not yet exposed through the SiloVault runtime.
- **Adapters**: New models require new adapter classes implementing the `VisionRuntime` protocol, not runtime redesign.

## Profiles
- **Lightweight**: Florence-2 Base (proven, zero-risk).
- **Balanced**: Florence-2 Base-FT (OOB OCR + OD).
- **Advanced**: Florence-2 Large-FT (segmentation + grounding).

## Known Limitations & Validation
- **Test status:** the AI vision, OCR and pack-manager suites are mocked integration-contract tests; they exercise code paths without real model weights and do not validate real inference quality, latency, or RAM usage. The "41/41" figure quoted in the AI-M5.x checkpoint is **historical**; later test modules have since been added, including the canonical `tests/test_m53_florence2_inference.py` (8 tests). The latest full-suite baseline is 1792 passed, 3 skipped, 1 warning (26 November 2026).
- `local_files_only=True` restricts HF `from_pretrained()` only; it does not guarantee application-wide offline operation.
- Inference speed, thermal behavior, and RAM usage require benchmarking on target hardware (Intel Core Ultra 9 185H).