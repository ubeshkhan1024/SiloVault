#!/usr/bin/env python3
"""Fix Florence2Runtime _do_execute inference method."""

with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'r') as f:
    content = f.read()

# Replace the _do_execute method's inference section
old_do_execute_section = '''    def _do_execute(
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

new_do_execute_section = '''    def _do_execute(
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

if old_do_execute_section in content:
    content = content.replace(old_do_execute_section, new_do_execute_section)
    with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'w') as f:
        f.write(content)
    print("Success: _do_execute method updated")
else:
    print("Target section not found - searching for _do_execute...")
    # Try to find it with a simpler search
    if "def _do_execute" in content:
        print("  _do_execute method exists in file")
        # Print lines around it
        import re
        match = re.search(r'def _do_execute.*?from exc\s*\n', content, re.DOTALL)
        if match:
            print("  Found _do_execute, showing first 600 chars:")
            print(match.group()[:600])
    else:
        print("  _do_execute method NOT found in file")