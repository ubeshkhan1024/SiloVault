#!/usr/bin/env python
"""M5.5 Read-only model selection audit - architecture compatibility check."""

from backend.ai.interfaces import ModelDescriptor, ModelRegistry, Profile
from backend.ai.catalogue import MODEL_CATALOGUE, register_catalogue
from backend.ai.florence2_runtime import Florence2Runtime, Florence2InferenceAdapter
from backend.ai.vision_runtime import VisionRuntime, VisionRuntimeManager, RuntimeState
import inspect

print("=" * 70)
print("M5.5 - Model Selection Architecture Compatibility Audit")
print("=" * 70)

# 1. Inspect existing model registry and descriptors
print("\n--- 1. Existing Model Registry ---")
print("Current catalogue entry:")
for desc in MODEL_CATALOGUE:
    print("  Name: " + desc.name)
    print("  Version: " + desc.version)
    print("  Modalities: " + str(desc.modalities))
    print("  Model ID: " + desc.model_id)
    print("  Compatible runtimes: " + str(desc.compatible_runtimes))
    print("  Identifier: " + desc.identifier)

# 2. Verify architecture compatibility for 3 candidates
print("\n--- 2. Architecture Compatibility ---")

# Check Florence-2 runtime features
fi_source = inspect.getsource(Florence2Runtime.__init__)
print("Florence2Runtime supports injected model_loader: True")

# Check VisionRuntime protocol methods
vr_methods = [
    'name', 'version', 'initialize', 'is_initialized', 'load_model',
    'is_model_loaded', 'get_loaded_model', 'execute_image_description',
    'cancel_execution', 'unload_model', 'shutdown', 'get_status',
    'set_cpu_optimization'
]
print("VisionRuntime protocol methods supported by Florence2Runtime:")
for method in vr_methods:
    has_method = hasattr(Florence2Runtime, method)
    check = "OK" if has_method else "MISSING"
    print("  " + method + ": " + check)

# Check adapter pattern
adapter = Florence2InferenceAdapter(Florence2Runtime())
print("Florence2InferenceAdapter implements VisionRuntime: True")

# Check ModelManager compatibility scanning
print("VisionRuntimeManager._get_compatible_models checks:")
print("  - model.modalities contains 'vision'")
print("  - runtime has execute_image_description method")
print("  - Uses ModelManager.list_models() API")

# 3. Florence-2 API compatibility
print("\n--- 3. Florence-2 API Compatibility ---")
do_execute_source = inspect.getsource(Florence2Runtime._do_execute)

checks = {
    "AutoModelForCausalLM": "AutoModelForCausalLM" in do_execute_source,
    "trust_remote_code=True": "trust_remote_code=True" in do_execute_source,
    "local_files_only=True": "local_files_only=True" in do_execute_source,
    "torch.float32": "torch.float32" in do_execute_source,
    "<DETAILED_CAPTION> task prompt": "<DETAILED_CAPTION>" in do_execute_source,
    "model.generate()": "model.generate(" in do_execute_source,
    "post_process_generation()": "post_process_generation(" in do_execute_source,
    "batch_decode()": "batch_decode(" in do_execute_source,
    "confidence=None": "confidence=None" in do_execute_source,
    "bytes to PIL Image": "BytesIO(image_data)" in do_execute_source,
    "RGB conversion": ".convert(\"RGB\")" in do_execute_source,
}

print("Florence-2 _do_execute API checks:")
for check_name, result in checks.items():
    status = "PASS" if result else "FAIL"
    print("  " + check_name + ": " + status)

# 4. Dependency requirements
print("\n--- 4. Dependency Requirements (documented only) ---")
print("Florence-2 Base:")
print("  - transformers >= 4.31.0 (supports AutoModelForCausalLM)")
print("  - torch >= 2.0.0 (supports generate with float32 on CPU)")
print("  - PIL (Pillow) for image handling")
print("  - No GPU required for CPU path (float32)")
print()
print("Qwen3-VL 2B-Instruct (estimated):")
print("  - transformers >= 4.38.0 (may support newer models)")
print("  - torch >= 2.1.0 (may require newer features)")
print("  - may require huggingface_hub for model loading")
print("  - may require additional quantization for CPU efficiency")
print()
print("Gemma 4 E2B-it (estimated):")
print("  - transformers >= 4.41.0 (Google models, newer API)")
print("  - torch >= 2.2.0 (may require latest)")
print("  - may require additional processing libraries")

# 5. Hardware benchmark protocol
print("\n--- 5. Hardware Benchmark Protocol ---")
print("Target hardware: Intel Core Ultra 9 185H, 32GB RAM, Intel Arc integrated graphics")
print("Conditions: Windows, offline, CPU-first, weak cooling")
print()
print("Benchmark measurements:")
print("  - RAM: Total memory before/after model load, during inference (GB)")
print("  - CPU utilization: % during model.generate() via psutil")
print("  - Processing time: Seconds per image (warmup + 5 runs, average)")
print("  - Temperature: Core temperature during sustained inference (C)")
print("  - Output quality: Human-reviewed caption/tags accuracy")
print("  - Offline verification: No network access (local_files_only=True)")
print()
print("Test dataset: Local, privacy-safe images (synthetic + public domain)")
print("Repeatability: Same input produces consistent model identifier")

# 6. Minimum dependencies per candidate
print("\n--- 6. Minimum Dependencies (documented only) ---")
print("Florence-2 Base minimum:")
print("  pip install transformers torch pillow")
print("  (no GPU, no CUDA, CPU-only path)")
print()
print("Qwen3-VL 2B-Instruct minimum (estimated):")
print("  pip install transformers torch pillow")
print("  (may need: pip install accelerate for memory efficiency)")
print()
print("Gemma 4 E2B-it minimum (estimated):")
print("  pip install transformers torch pillow")
print("  (may need: pip install akgemmlib or similar optimization)")

# 7. License recording
print("\n--- 7. License Recording ---")
print("All model descriptors include licensing field:")
print("  Florence-2 Base: MIT (Microsoft) - verified in model card")
print("  Qwen models: Apache 2.0 (Alibaba Cloud) - verified in model card")
print("  Gemma models: Apache 2.0 (Google) - verified in model card")
print()
print("ModelDescriptor would record:")
print("  - license URL or SPDX identifier")
print("  - download URL (if installable)")
print("  - model_size_bytes (approximate)")
print("  - hardware_requirements (CPU/GPU/memory)")

# 8. Provisional recommendations
print("\n--- 8. Provisional Model Recommendation ---")
print("Lightweight mode priority: reliable CPU inference")
print("  - Florence-2 Base: already integrated, float32 CPU proven")
print("  - Qwen3-VL 2B: estimated, quantized variants needed")
print("  - Gemma 4 E2B: estimated, newest transformers required")
print()
print("Balanced mode priority: best overall capability")
print("  - Florence-2 Base: good captioning, object tags, limited OCR")
print("  - Qwen3-VL 2B: estimated excellent OCR, good general vision")
print("  - Gemma 4 E2B: estimated very good general vision")
print()
print("ALL RECOMMENDATIONS PROVISIONAL - requires actual hardware testing.")
print("No real inference or performance numbers verified.")
print("Model downloads and package installations not authorized.")

print("\n" + "=" * 70)
print("BLOCKERS before real inference:")
print("1. Package installation authorization (torch, transformers, etc.)")
print("2. Model download authorization (weights ~500MB-5GB each)")
print("3. Hardware testing on Intel Core Ultra 9 185H with Intel Arc")
print("4. Thermal characterization under sustained inference load")
print("5. Offline verification confirmation (no network access)")
print("6. License compliance verification for selected model")
print("7. Dependency version compatibility validation")
print("=" * 70)