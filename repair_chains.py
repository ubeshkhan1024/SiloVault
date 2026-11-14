"""Comprehensive repair of google_drive.py method chaining."""
path = r"c:\Projects\SiloVault\backend\storage\google_drive.py"
with open(path, "rb") as f:
    data = f.read()

# The pattern we need to fix is:
# files().method(
#     args...
# )
# .execute()
# 
# Should become:
# files().method(
#     args...
# ).execute()

# Or more simply, we need to join lines where:
# Line ends with ) and next line starts with .execute() or .other_method()

# Let's use a line-by-line approach
lines = data.split(b'\r\n')
result = []
i = 0
while i < len(lines):
    line = lines[i]
    result.append(line)
    
    # Check if line ends with just ) and next line starts with .execute() or .update() or .delete()
    if i + 1 < len(lines):
        next_line = lines[i + 1]
        stripped_line = line.rstrip()
        if (stripped_line.endswith(b')') and 
           next_line.lstrip().startswith(b'.execute()') or
           next_line.lstrip().startswith(b'.update(') or
           next_line.lstrip().startswith(b'.delete(') or
           next_line.lstrip().startswith(b'.get(')):
            # This is a continuation - don't add newline, just merge
            result.pop()  # Remove the last added line
            merged = line + next_line
            result.append(merged)
            i += 1  # Skip the next line since we merged it
    
    i += 1

data = b'\r\n'.join(result)

with open(path, "wb") as f:
    f.write(data)
print("Repair complete")