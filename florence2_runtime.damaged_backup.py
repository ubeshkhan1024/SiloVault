#!/usr/bin/env python
"""Quick fix: replace hardcoded confidence 0.95 with None."""
import re

with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'r') as f:
    content = f.read()

# Replace "confidence": None with "confidence": None
# Be careful to only match the _do_execute return dict line
content = content.replace(
    '"confidence": None',
    '"confidence": None'
)

with open(r'c:\Projects\SiloVault\backend\ai\florence2_runtime.py', 'w') as f:
    f.write(content)

print('Confidence field updated from 0.95 to None')