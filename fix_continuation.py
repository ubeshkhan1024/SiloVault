"""Fix line continuation issues in google_drive.py."""
path = r"c:\Projects\SiloVault\backend\storage\google_drive.py"
with open(path, "rb") as f:
    data = f.read()

# Pattern: files()\r\n            .method( should be files().method(
# The issue is the closing paren on files() followed by newline and then .method(
import re

# Fix all occurrences of files() followed by newline and .method(
# We need to remove the newline between files() and the dot
def fix_method_continuation(content):
    # Pattern: files() followed by newline and optional whitespace and .
    # Replace with files().method(
    pattern = rb'(files\(\))(\r\n\s+)\.('
    def replacer(match):
        return match.group(1).decode('utf-8') + ' .' + match.group(3).decode('utf-8')
    
    # Simpler approach: just join the lines
    # files()\n            .update( -> files().update(
    content = re.sub(rb'files\(\)\r\n\s+\.update\(', rb'files().update(', content)
    content = re.sub(rb'files\(\)\r\n\s+\.delete\(', rb'files().delete(', content)
    content = re.sub(rb'files\(\)\r\n\s+\.get\(', rb'files().get(', content)
    return content

data = fix_method_continuation(data)

with open(path, "wb") as f:
    f.write(data)
print("Fixed method continuation issues")