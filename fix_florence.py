#!/usr/bin/env python3
"""Fix Florence2Runtime inference to use official Florence-2 workflow."""

import re

# Read the file
with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'r') as f:
    content = f.read()

# --- Fix 1: Replace _try_lazy_import_load import ---
# AutoModelForVision2Seq -> AutoModelForCausalLM in the lazy import
content = content.replace(
    "from transformers import AutoProcessor, AutoModelForVision2Seq  # type: ignore[import]",
    "from transformers import AutoProcessor, AutoModelForCausalLM  # type: ignore[import]"
)

# --- Fix 2: Replace load_model lazy-import path ---
old_load_model = '''            # Lazy-import path (packages available)\n            try:\n                from transformers import AutoProcessor, AutoModelForCausalLM  # type: ignore[import]\n\n                # Resolve model subdirectory (Florence-2 stores model files under a\n                # possibly versioned subdirectory or the root itself)\n                model_subdir = self._find_model_subdirectory(model_dir, manifest)\n                if model_subdir is None:\n                    model_subdir = model_dir\n\n                processor = AutoProcessor.from_pretrained(str(model_subdir))\n                model = AutoModelForVision2Seq.from_pretrained(\n                    str(model_subdir), torch_dtype=torch.float16\n                )'''

new_load_model = '''            # Lazy-import path (packages available)\n            try:\n                from transformers import AutoProcessor, AutoModelForCausalLM  # type: ignore[import]\n\n                # Resolve model subdirectory (Florence-2 stores model files under a\n                # possibly versioned subdirectory or the root itself)\n                model_subdir = self._find_model_subdirectory(model_dir, manifest)\n                if model_subdir is None:\n                    model_subdir = model_dir\n\n                processor = AutoProcessor.from_pretrained(str(model_subdir), trust_remote_code=True, local_files_only=True)\n                model = AutoModelForCausalLM.from_pretrained(\n                    str(model_subdir), trust_remote_code=True, local_files_only=True, torch_dtype=torch.float32\n                )'''

if old_load_model in content:
    content = content.replace(old_load_model, new_load_model)
    print("Fix 2: load_model lazy-import path updated")
else:
    print("Fix 2: target not found, searching...")
    # Check what's there
    if "AutoModelForCausalLM" in content:
        print("  AutoModelForCausalLM already present")
    if "AutoModelForVision2Seq" in content:
        print("  AutoModelForVision2Seq still present")

# --- Fix 3: Replace _do_execute inference ---
old_do_execute = '''    def _do_execute(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        \"\"\"Core inference logic — injected executor or default transformers path.\"\"\"
        # Check cancellation before starting heavy work
        if self._cancellation_token.is_set():
            raise CancellationError(\"Inference cancelled before start\")

        # Use injected executor if provided
        if self._executor is not None:
            return self._executor(image_data, mime_type=mime_type, **kwargs)

        # Default transformers-based execution
        # (this path is reached when packages are available at runtime)
        try:
            from transformers import AutoProcessor, AutoModelForCausalLM  # type: ignore[import]

            model_info = self._loaded_model
            processor: Any = model_info["processor"]
            model: Any = model_info["model"]

            # Process the image
            inputs = processor(images=image_data, return_tensors="pt")

            # Move to CPU and disable gradient computation
            inputs = {k: v.to("cpu") for k, v in inputs.items()}
            with torch.no_grad():
                outputs = model(**inputs)

            # Decode the generated tokens into text
            generated_tokens = outputs.logits.argmax(-1)
            description = processor.decode(generated_tokens[0], skip_special_tokens=True)

            # Derive simple tags from the description
            tags = tuple(
                w.strip(".,!;:")
                for w in description.split()
                if w.isalpha() and len(w) > 2
            )

            return {
                "description": description,
                "tags": tags,
                "model": self._get_loaded_model_identifier(),
                "confidence": 0.95,
            }
        except Exception as exc:  # pylint: disable=broad-except
            raise ExecutionError(f"Inference execution error: {exc}") from exc'''

new_do_execute = '''    def _do_execute(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        \"\"\"Core inference logic — injected executor or default transformers path.\"\"\"
        # Check cancellation before starting heavy work
        if self._cancellation_token.is_set():
            raise CancellationError("Inference cancelled before start")

        # Use injected executor if provided
        if self._executor is not None:
            return self._executor(image_data, mime_type=mime_type, **kwargs)

        # Default transformers-based execution
        # (this path is reached when packages are available at runtime)
        try:
            from transformers import AutoProcessor, AutoModelForCausalLM  # type: ignore[import]

            model_info = self._loaded_model
            processor: Any = model_info["processor"]
            model: Any = model_info["model"]

            # Convert encoded image bytes to validated PIL Image
            # Preserve original image dimensions for post-processing
            pil_image = Image.open(BytesIO(image_data)).convert("RGB")
            orig_width, orig_height = pil_image.size

            # Define task prompt for detailed captioning
            task_prompt = "<DETAILED_CAPTION>"

            # Process the image with task prompt
            inputs = processor(text=task_prompt, images=pil_image, return_tensors="pt")

            # Move to CPU and disable gradient computation
            inputs = {k: v.to("cpu") for k, v in inputs.items()}

            with torch.no_grad():
                # Generate using the official Florence-2 generation path
                generated_ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=1024,
                    do_sample=False,
                    num_beams=3,
                )

            # Decode generated tokens using processor's batch_decode
            generated_text = processor.batch_decode(
                generated_ids, skip_special_tokens=False
            )[0]

            # Post-process the generation using the task prompt and image dimensions
            structured_result = processor.post_process_generation(
                generated_text, task=task_prompt, image_size=(orig_width, orig_height)
            )

            # Extract description and tags from structured result
            # Preserve SiloVault's output contract: description, tags, model, confidence
            description = structured_result.get("<DETAILED_CAPTION>", structured_result.get("caption", ""))
            if not description:
                description = ""

            # Derive tags from description words (fallback if post-processing
            # doesn't provide them)
            tags = tuple(
                w.strip(".,!;:")
                for w in description.split()
                if w.isalpha() and len(w) > 2
            )

            return {
                "description": description,
                "tags": tags,
                "model": self._get_loaded_model_identifier(),
                "confidence": 0.95,
            }
        except Exception as exc:  # pylint: disable=broad-except
            raise ExecutionError(f"Inference execution error: {exc}") from exc'''

if old_do_execute in content:
    content = content.replace(old_do_execute, new_do_execute)
    print("Fix 3: _do_execute updated")
else:
    print("Fix 3: target not found, searching for _do_execute...")
    # Find _do_execute
    if "def _do_execute" in content:
        print("  _do_execute method found")
    else:
        print("  _do_execute method NOT found")

# --- Fix 4: Add imports at top ---
# Add BytesIO and Image imports if not present
if "from io import BytesIO" not in content:
    # Find the import section and add BytesIO
    import_line = "import json\nimport logging\nimport os\nimport threading"
    new_imports = "import json\nimport logging\nimport os\nimport threading\nfrom io import BytesIO"
    content = content.replace(import_line, new_imports)
    print("Fix 4: Added BytesIO import")

if "from PIL import Image" not in content:
    import_line = "import json\nimport logging\nimport os\nimport threading\nfrom io import BytesIO"
    new_imports2 = "import json\nimport logging\nimport os\nimport threading\nfrom io import BytesIO\nfrom PIL import Image"
    content = content.replace(import_line, new_imports2)
    print("Fix 4b: Added PIL Image import")

# Write the file back
with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'w') as f:
    f.write(content)

print("\nAll fixes attempted. Please verify the file.")