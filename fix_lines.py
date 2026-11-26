#!/usr/bin/env python3
"""Fix Florence2Runtime _do_execute inference method - line-by-line replacement."""

with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'r') as f:
    lines = f.readlines()

# The _do_execute method starts at line 426 (0-indexed: 425)
# We need to replace lines 433-457 (0-indexed: 432-456) with new code

# New code lines (will be inserted)
new_inference = '''            # Convert encoded image bytes to validated PIL Image\n            # Preserve original image dimensions for post-processing\n            pil_image = Image.open(BytesIO(image_data)).convert("RGB")\n            orig_width, orig_height = pil_image.size\n\n            # Define task prompt for detailed captioning\n            task_prompt = "<DETAILED_CAPTION>"\n\n            # Process the image with task prompt\n            inputs = processor(text=task_prompt, images=pil_image, return_tensors="pt")\n\n            # Move to CPU and disable gradient computation\n            inputs = {k: v.to("cpu") for k, v in inputs.items()}\n\n            with torch.no_grad():\n                # Generate using the official Florence-2 generation path\n                generated_ids = model.generate(\n                    input_ids=inputs["input_ids"],\n                    pixel_values=inputs["pixel_values"],\n                    max_new_tokens=1024,\n                    do_sample=False,\n                    num_beams=3,\n                )\n\n            # Decode generated tokens using processor's batch_decode\n            generated_text = processor.batch_decode(\n                generated_ids, skip_special_tokens=False\n            )[0]\n\n            # Post-process the generation using the task prompt and image dimensions\n            structured_result = processor.post_process_generation(\n                generated_text, task=task_prompt, image_size=(orig_width, orig_height)\n            )\n\n            # Extract description and tags from structured result\n            # Preserve SiloVault's output contract: description, tags, model, confidence\n            description = structured_result.get("<DETAILED_CAPTION>", structured_result.get("caption", ""))\n            if not description:\n                description = ""\n\n            # Derive tags from description words (fallback if post-processing\n            # doesn't provide them)\n            tags = tuple(\n                w.strip(".,!;:")\n                for w in description.split()\n                if w.isalpha() and len(w) > 2\n            )\n\n            return {\n                "description": description,\n                "tags": tags,\n                "model": self._get_loaded_model_identifier(),\n                "confidence": 0.95,\n            }'''

# Find the range to replace: from "            # Process the image" to the return statement
# Line 433 (1-indexed) = index 432 in 0-indexed list
# Through line 457 (1-indexed) = index 456 in 0-indexed list (the return line)

# Actually, let me find the exact range
# Start: line 433 is "            # Process the image"
# End: line 457 is the return statement

# Let me just replace from line 433 to line 457 (inclusive)
# 0-indexed: 432 to 456

# Check if the lines match what we expect
if lines[432].strip() == "# Process the image":
    print(f"Line 433 matches: {lines[432].strip()}")
    # Replace lines 433-457 (0-indexed 432-456 inclusive)
    # That's 25 lines (433 to 457 inclusive = 25 lines)
    
    # New code has multiple lines - let me count
    new_lines = new_inference.split('\n')
    print(f"New inference code has {len(new_lines)} lines")
    
    # Replace the range
    # We'll replace from index 432 to index 456 (inclusive) = 25 lines
    # with the new code lines
    
    # But the new code might have a different number of lines
    # Let me just replace from "# Process the image" through the return
    
    # Find "            return {" line
    return_start = None
    for i in range(432, min(470, len(lines))):
        if '("description"' in lines[i]:
            return_start = i
            break
    
    if return_start:
        print(f"Return statement found at line {return_start + 1} (1-indexed)")
        # Replace from line 433 (0-indexed 432) to return_start (inclusive)
        # Actually, let me replace from "# Process the image" through end of file? No, just the section.
        
        # Let me try a different approach: replace the entire _do_execute method
        # Find where _do_execute starts and ends
        do_execute_start = 425  # 0-indexed
        # Find the next method definition
        do_execute_end = None
        for i in range(do_execute_start + 1, len(lines)):
            if lines[i].startswith("    def ") and i > do_execute_start:
                do_execute_end = i
                break
        
        if do_execute_end:
            print(f"_do_execute method spans lines {do_execute_start + 1} to {do_execute_end} (1-indexed)")
            # Replace the entire method body
            # Keep the method signature and docstring, replace the body
            method_sig = lines[do_execute_start]
            # The docstring is on the next line(s)
            
            # Actually, let me just do a string replacement on the whole file
            # using a unique marker
            pass
    
    # Simpler approach: replace lines 433-457 with the new code
    # Lines 433-457 in 1-indexed = indices 432-456 in 0-indexed
    # That's 25 lines
    
    # Let me just count the old lines from 433 to the return
    old_section = ''.join(lines[432:457])  # indices 432 to 456 inclusive
    print(f"Old section length: {len(old_section)} chars")
    
    # Now replace
    new_section = new_inference + '\n'
    
    # Build new content
    new_lines = lines[:432] + [new_inference + '\n'] + lines[457:]
    new_content = ''.join(new_lines)
    
    with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'w') as f:
        f.write(new_content)
    print("Success: _do_execute method updated")
else:
    print("Line 433 does not match expected '# Process the image'")
    print(f"Actual line 433: {lines[432].strip()}")