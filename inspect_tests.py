#!/usr/bin/env python
import sys

with open(r'c:\Projects\SiloVault\tests\test_m52_vision_runtime.py', 'r') as f:
    content = f.read()

# Find all test function definitions
import re
tests = re.findall(r'def (test_\w+)', content)
print('Test functions found:')
for t in tests:
    print(f'  {t}')
print()

# Check for key terms in the test file
key_terms = ['_do_execute', 'model.generate', 'post_process_generation', 'batch_decode', 'BytesIO', 'pil_image', 'task_prompt', 'confidence=None']
for term in key_terms:
    count = content.count(term)
    print(f'Term "{term}" found {count} times in test file')
print()

# Show the new tests that were added
# Find test functions that contain the key terms
for test_name in tests:
    idx = content.find('def ' + test_name)
    if idx >= 0:
        segment = content[idx:idx+500]
        # Check if this test contains any of our key terms
        found_terms = [t for t in key_terms if t in segment]
        if found_terms:
            print(f'Test {test_name} contains: {found_terms}')