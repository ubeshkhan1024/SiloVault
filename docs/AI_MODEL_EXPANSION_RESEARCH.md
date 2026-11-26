# docs/AI_MODEL_EXPANSION_RESEARCH.md

# SiloVault Lightweight AI Model Expansion Research (M5.7)

## Executive Summary
This report evaluates lightweight, offline-capable AI models and engines to reduce SiloVault's total installation footprint while maintaining modularity. The research focuses on models that provide useful file-management capabilities (captioning, similarity, classification, OCR, and detection) with minimal dependency overhead.

**Key finding:** SiloVault’s architecture already supports modularity via `ModelDescriptor` and the adapter pattern. The transition to a lightweight expansion requires moving from a monolithic dependencies model to an "optional dependency pack" architecture, where heavy ML libraries (PyTorch/Transformers) are only installed if an AI model is selected.

**Offline scope note:** The runtime's `local_files_only=True` flag only restricts the Hugging Face `from_pretrained()` loading operation. It does not guarantee application-wide offline behavior; any other network-capable component must be audited separately.

**Current status note (26 November 2026):** this is a research report. The "AI Model Pack" system it proposes is **not implemented**. The repository contains only a read-only `PackManager` foundation (capability discovery, hardware-compatibility checks, recommendations and in-memory approvals) plus a design document, `M5.8.1_AI_PACK_MANAGER_ARCHITECTURE.md`. There is no pack download, no checksum-verified installation, no persistent approval store and no pack-management UI. Florence-2 real inference still requires optional packages and model weights that are not bundled.

---

## 1. Research Candidates

### Image Understanding & Captioning
| Model | Params | Weight Size (FP16) | License | CPU Support | Offline | Framework |
|---|---|---|---|---|---|---|
| **Florence-2 Base** | 232M (232,844,395) | ~0.4 GB (FP16); FP32 = 931,377,580 bytes (~931.4 MB decimal / 888 MiB) | MIT | ✅ | ✅ | PyTorch/Transformers |
| **SmolVLM-256M** | 256M | ~0.5 GB | Apache 2.0 | ✅ | ✅ | PyTorch/Transformers |
| **SmolVLM2-500M** | 500M | ~0.6 GB | Apache 2.0 | ✅ | ✅ | PyTorch/Transformers |
| **Moondream 2** | 1.6B | ~1.2 GB | Apache 2.0 | ✅ | ✅ | PyTorch/Transformers |

*Note: Florence-2 Base FP32 size is derived from the exact parameter count (232,844,395 × 4 bytes = 931,377,580 bytes). FP16 weight-file size (~0.4 GB) is a theoretical estimate; actual download size and runtime RAM usage are unknown until benchmarked.*

### Image Similarity & Embeddings
| Model | Params | Weight Size | License | CPU Support | Offline | Framework |
|---|---|---|---|---|---|---|
| **MobileCLIP** | Base: ~150M; Large: ~300M | Base: ~0.2 GB; Large: ~0.5 GB | Code: MIT; Models: Apple ML Research Model Terms | ✅ | ✅ | PyTorch/Transformers |

### Image Classification
| Model | Params | Weight Size | License | CPU Support | Offline | Framework |
|---|---|---|---|---|---|---|
| **MobileNetV3 Small** | 2,542,856 | ~9.8 MB | Apache 2.0 | ✅ | ✅ | PyTorch/Torchvision |
| **MobileNetV3 Large** | 5,483,032 | ~21.1 MB | Apache 2.0 | ✅ | ✅ | PyTorch/Torchvision |

*Note: Parameter counts and sizes from official TorchVision documentation.*

### OCR
| Engine | License | CPU Support | Offline | Framework |
|---|---|---|---|---|
| **Tesseract** | Apache 2.0 | ✅ | ✅ | C++ / Tesseract API |
| **PaddleOCR (Light)** | Apache 2.0 | ✅ | ✅ | PaddlePaddle |

*Note: Tesseract engine installation is separate from language data. English `tessdata_fast` data is ~3.92 MB. Language data is not always bundled with the engine installation.*

---

## 2. Core Optimization Investigation (Dependency Footprint)

Comparison of AI execution engines for SiloVault:

| Engine | Installation Footprint | Complexity | Optimization |
|---|---|---|---|
| **PyTorch/Transformers** | High (~1GB+) | Low | Standard; easiest to integrate |
| **ONNX Runtime** | Medium (~200MB) | Medium | Excellent for CPU; requires model conversion |
| **OpenVINO** | Medium (~300MB) | High | Best for Intel hardware (CPU/Arc/NPU) |

**Recommendation:**
- Maintain **PyTorch/Transformers** for initial Florence-2 integration (already tested via mocked integration-contract tests; real model inference not yet validated).
- Explore **ONNX Runtime** as an optional, lightweight alternative for inference-only deployment (requires exporting models to ONNX via `torch.onnx.export()`; not yet validated with the existing Florence-2 adapter).
- Explore **OpenVINO** specifically for Intel-based hardware acceleration when requested (requires IR conversion; not yet validated).

*Note: Installation footprints above are approximate estimates from package metadata; actual installed sizes are unknown until measured on the target platform.*

---

## 3. Lightweight Architecture Proposal

To minimize footprint, SiloVault should implement an "AI Model Pack" system:

1. **Core SiloVault:** Zero AI dependencies.
2. **Optional AI Packs:**
   - `pip install silovault-ai-vision` (Florence-2, PyTorch, Transformers)
   - `pip install silovault-ai-ocr` (Tesseract, PaddleOCR)
   - `pip install silovault-ai-similarity` (MobileCLIP)

**Registry implementation:**
- `ModelDescriptor` entries for each model.
- Registry checks for required packages before allowing model installation.
- Lazy load AI modules only when needed; unload when idle.

---

## 4. Recommended Lineup

| Capability | Recommended Model | Rationale |
|---|---|---|
| **Image Description** | Florence-2 Base | Proven, lightweight, MIT license; validated via mocked integration-contract tests (real inference not yet benchmarked) |
| **Similarity/Embeddings** | MobileCLIP | Extremely compact, specialized for similarity |
| **Classification** | MobileNetV3 | Industry standard for lightweight classification |
| **OCR** | Tesseract | Mature, highly efficient, offline-first |

---

## 5. Unknowns and Next Steps

**Unknowns:**
- Actual inference latency on target hardware (Intel Core Ultra 9 185H).
- Thermal behavior under sustained inference load.
- Complexity of exporting Florence-2 to ONNX or OpenVINO format.
- Real-world accuracy of MobileCLIP for SiloVault's specific file-management use cases.
- Runtime RAM usage during model load and inference (unmeasured).
- Whether the existing Florence-2 adapter exposes tasks beyond `<DETAILED_CAPTION>` (currently hardcoded).

**Test status:** the AI vision/OCR/pack tests are mocked integration-contract tests. They exercise code paths without real model weights and do not validate real inference quality, latency, or RAM usage. The "41/41" figure cited at the AI-M5.7 checkpoint is **historical**; modules have been added since (for example the canonical `tests/test_m53_florence2_inference.py`, 8 tests), and the latest full-suite baseline is 1792 passed, 3 skipped, 1 warning (26 November 2026).

**Next Step:**
1. Authorize testing of Florence-2 Base (already integrated).
2. Benchmark RAM, CPU, and thermal performance.
3. Define the "AI Model Pack" installation structure.
4. Authorize testing of OCR and similarity models.

---
*Disclaimer: All analysis is based on documentation and official model cards. No real inference or hardware benchmarking has been performed.*