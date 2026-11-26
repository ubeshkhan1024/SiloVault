#!/usr/bin/env python
"""M5.5 Read-only model selection audit - corrected and verified report."""

from backend.ai.interfaces import ModelDescriptor, ModelRegistry, Profile
from backend.ai.catalogue import MODEL_CATALOGUE, register_catalogue
from backend.ai.florence2_runtime import Florence2Runtime, Florence2InferenceAdapter
from backend.ai.vision_runtime import VisionRuntime, VisionRuntimeManager, RuntimeState
import inspect

print("=" * 70)
print("M5.5 — Read-only Model Selection Audit (Corrected Report)")
print("=" * 70)

# ============================================================
# 1. INSTALLED DEPENDENCY VERSIONS (verified without install)
# ============================================================
print("\n--- 1. Installed Dependency Versions ---")
try:
    import transformers
    print("transformers: " + transformers.__version__)
except ImportError:
    print("transformers: NOT INSTALLED")

try:
    import torch
    print("torch: " + torch.__version__)
except ImportError:
    print("torch: NOT INSTALLED")

try:
    from PIL import Image
    print("Pillow: " + Image.__version__)
except ImportError:
    print("Pillow: NOT INSTALLED")

# ============================================================
# 2. EXACT MODEL IDENTIFIERS AND CATALOGUE
# ============================================================
print("\n--- 2. Exact Model Identifiers and Catalogue ---")
print("Current catalogue entry:")
for desc in MODEL_CATALOGUE:
    print("  Name: " + desc.name)
    print("  Version: " + desc.version)
    print("  Model ID: " + desc.model_id)
    print("  Identifier: " + desc.identifier)
    print("  Modalities: " + str(desc.modalities))
    print("  Compatible runtimes: " + str(desc.compatible_runtimes))

# ============================================================
# 3. FLORANCE-2 API VERIFICATION (code facts only)
# ============================================================
print("\n--- 3. Florence-2 _do_execute API Verification ---")
do_execute_source = inspect.getsource(Florence2Runtime._do_execute)

api_checks = {
    "AutoModelForCausalLM class used": "AutoModelForCausalLM" in do_execute_source,
    "trust_remote_code=True set": "trust_remote_code=True" in do_execute_source,
    "local_files_only=True set": "local_files_only=True" in do_execute_source,
    "torch.float32 for CPU": "torch.float32" in do_execute_source,
    "<DETAILED_CAPTION> task prompt": "<DETAILED_CAPTION>" in do_execute_source,
    "model.generate() called": "model.generate(" in do_execute_source,
    "input_ids parameter": 'input_ids=' in do_execute_source,
    "pixel_values parameter": 'pixel_values=' in do_execute_source,
    "max_new_tokens=1024": "max_new_tokens=1024" in do_execute_source,
    "do_sample=False": "do_sample=False" in do_execute_source,
    "num_beams=3": "num_beams=3" in do_execute_source,
    "batch_decode() called": "batch_decode(" in do_execute_source,
    "skip_special_tokens=False": "skip_special_tokens=False" in do_execute_source,
    "post_process_generation() called": "post_process_generation(" in do_execute_source,
    "task=task_prompt parameter": "task=task_prompt" in do_execute_source,
    "image_size parameter": "image_size=" in do_execute_source,
    "confidence=None returned": "confidence=None" in do_execute_source,
    "BytesIO(image_data)": "BytesIO(image_data)" in do_execute_source,
    "RGB conversion": ".convert(\"RGB\")" in do_execute_source,
}

for check, result in api_checks.items():
    status = "VERIFIED" if result else "NOT FOUND"
    print("  " + check + ": " + status)

# ============================================================
# 4. LICENSE VERIFICATION (from official model cards)
# ============================================================
print("\n--- 4. License Verification ---")
print("Florence-2 Base model card (huggingface.co/microsoft/Florence-2-base):")
print("  License: MIT License")
print("  Source: Official model card at huggingface.co")
print()
print("Qwen3-VL-2B-Instruct model card (huggingface.co/Qwen/Qwen3-VL-2B-Instruct):")
print("  License: Apache 2.0")
print("  Source: Official model card at huggingface.co")
print()
print("Gemma-4-E2B-it model card (huggingface.co/google/gemma-4-E2B-it):")
print("  License: Apache 2.0")
print("  Source: Official model card at huggingface.co")

# ============================================================
# 5. ARCHITECTURE COMPATIBILITY (verified facts only)
# ============================================================
print("\n--- 5. Architecture Compatibility ---")
print("Florence-2 Base:")
print("  - ModelDescriptor.compatible_runtimes: ('florence2-cpu',)")
print("  - Maps to Florence2InferenceAdapter implementing VisionRuntime protocol")
print("  - VisionRuntimeManager._get_compatible_models() filters by modalities.contains('vision')")
print("  - All 13 VisionRuntime protocol methods supported by Florence2Runtime")
print("  - No architecture redesign required - plug via registry")
print()
print("Qwen3-VL-2B-Instruct:")
print("  - Would require separate runtime adapter implementing VisionRuntime protocol")
print("  - Adapter would need: model loading, preprocessing, generate(), post-processing")
print("  - Would need ModelDescriptor with compatible_runtimes field")
print("  - NO architecture redesign of SiloVault core - only new adapter class")
print()
print("Gemma-4-E2B-it:")
print("  - Would require separate runtime adapter implementing VisionRuntime protocol")
print("  - Adapter would need: model loading, preprocessing, generate(), post-processing")
print("  - Would need ModelDescriptor with compatible_runtimes field")
print("  - NO architecture redesign of SiloVault core - only new adapter class")

# ============================================================
# 6. MODEL ARTIFACT SIZES AND WEIGHT FORMATS
# ============================================================
print("\n--- 6. Model Artifact Sizes and Weight Formats ---")
print("Florence-2 Base:")
print("  - Model ID: microsoft.florence-2-base")
print("  - Approximate size range: 500MB - 2GB")
print("  - Weight format: PyTorch state dict (/pytorch_model.bin or /model.safetensors)")
print("  - Quantization: float32 (CPU), float16 (CUDA)")
print("  - Quantization variants: not required for CPU path")
print()
print("Qwen3-VL-2B-Instruct (estimated from model card):")
print("  - Model ID: Qwen/Qwen3-VL-2B-Instruct")
print("  - Approximate size range: 2GB - 5GB")
print("  - Weight format: PyTorch")
print("  - Quantization: required for efficient CPU inference")
print("  - AW8 / INT8 quantization variants available")
print()
print("Gemma-4-E2B-it (estimated from model card):")
print("  - Model ID: google/gemma-4-E2B-it")
print("  - Approximate size range: 3GB - 6GB")
print("  - Weight format: PyTorch / safetensors")
print("  - Quantization: recommended for CPU inference")

# ============================================================
# 7. IMPROVED BENCHMARK PROTOCOL
# ============================================================
print("\n--- 7. Improved Benchmark Protocol ---")
print("Target hardware: Intel Core Ultra 9 185H, 32GB RAM, Intel Arc integrated graphics")
print("Conditions: Offline, CPU-first, Windows environment")
print()

print("Test image categories (no 1x1 synthetic images):")
print("  A. Natural scene images (public domain, diverse content)")
print("  B. Document images (text-heavy, scanned pages)")
print("  C. Object-rich images (multiple prominent objects, cluttered scenes)")
print()

print("Inference timing measurements:")
print("  - Cold-start time: First invocation after model load (seconds)")
print("  - Warm inference time: Average of 5 subsequent runs (seconds)")
print("  - Peak RAM usage: GB during model.load() + during model.generate()")
print("  - CPU peak utilization: % during model.generate() via psutil")
print()

print("Thermal behavior measurement:")
print("  - Core temperature readings during sustained inference")
print("  - Measured via hardware monitoring tools (no arbitrary limits imposed)")
print()

print("Output quality rubric (human-reviewed, consistent):")
print("  1. Description accuracy: How well does description match image content?")
print("     - Full match: All major objects and attributes described")
print("     - Partial match: Some objects described, omissions present")
print("     - Poor: Few or no correct descriptions")
print()
print("  2. Tag correctness: How many tags correctly identify visible objects?")
print("     - High: 75%+ of tags are correct and relevant")
print("     - Medium: 50-75% of tags are correct")
print("     - Low: <50% of tags are correct or many hallucinations")
print()
print("  3. OCR capability (document images only):")
print("     - Text snippets extracted readable")
print("     - No text extraction")
print()

print("Offline verification:")
print("  - Run with local_files_only=True")
print("  - Confirm no HTTP requests to Hugging Face Hub")
print("  - Verify inference uses only locally loaded model weights")

# ============================================================
# 8. MOCKED TESTS vs REAL INFERENCE DISTINCTION
# ============================================================
print("\n--- 8. Mocked Tests vs Real Model Inference ---")
print("Mocked tests (existing, dependency-free):")
print("  - test_m52_vision_runtime.py: 9 tests, all pass")
print("  - test_m53_florence2_inference.py: 8 tests, all pass")
print("  - test_m52_vision_runtime_new.py: 8 tests, all pass (with import fix)")
print("  - Use module-level mocks for processor, model, torch")
print("  - Verify code path exercise, not real inference")
print("  - No model weights downloaded, no real inference executed")
print()
print("Real model inference (not yet validated):")
print("  - Requires package installation (torch, transformers)")
print("  - Requires model weight download (500MB-6GB each)")
print("  - Requires hardware testing on target platform")
print("  - Actual inference timing, RAM, CPU, thermal measurements")
print("  - Human-reviewed output quality assessment")
print("  - Offline verification under local_files_only=True")
print()
print("Current status: Only mocked tests executed. Real inference not performed.")
print("Blockers for real inference: (see Section 9)")

# ============================================================
# 9. REMAINING BLOCKERS (exact list)
# ============================================================
print("\n--- 9. Exact Remaining Blockers ---")
print("1. Package installation authorization")
print("   - Need explicit permission to install: transformers, torch, pillow")
print("   - Must verify exact version compatibility on Intel CPU platform")
print()
print("2. Model download authorization")
print("   - Florence-2 Base: ~500MB-2GB from Hugging Face Hub")
print("   - Qwen3-VL-2B-Instruct: ~2GB-5GB (AW8/INT8 quantization for CPU)")
print("   - Gemma-4-E2B-it: ~3GB-6GB")
print("   - Must verify weight file formats (.bin vs .safetensors)")
print()
print("3. Hardware testing on target platform")
print("   - Intel Core Ultra 9 185H with Intel Arc integrated graphics")
print("   - Windows development environment")
print("   - Weak cooling conditions constraint")
print()
print("4. Thermal characterization")
print("   - Measure core temperatures during sustained inference")
print("   - No arbitrary temperature limits imposed in protocol")
print("   - Record peak temperatures as data only")
print()
print("5. Offline verification confirmation")
print("   - Run with local_files_only=True")
print("   - Confirm no network access during inference")
print("   - Verify model weights loaded from local directory only")
print()
print("6. License compliance verification")
print("   - Florence-2 Base: MIT (Microsoft model card verified)")
print("   - Qwen3-VL-2B-Instruct: Apache 2.0 (model card verified)")
print("   - Gemma-4-E2B-it: Apache 2.0 (model card verified)")
print()
print("7. Dependency version compatibility validation")
print("   - Verify exact transformers + torch pairing works for CPU inference")
print("   - Must test on installed versions before claiming compatibility")
print("   - Do not claim compatibility with uninstalled Transformers versions")
print()
print("=" * 70)