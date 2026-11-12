with open(r'c:\Projects\SiloVault\backend\api\v1\endpoints\files.py', 'r') as f:
    lines = f.readlines()

# Find and replace line 182 (0-indexed: 181)
for i, line in enumerate(lines):
    if 'Content-Disposition' in line and 'filename' in line and 'header' not in line:
        print(f'Found at line {i+1}: {repr(line)}')
        lines[i] = '            "Content-Disposition": f"attachment; filename*=utf-8\'\'{encoded_filename}",\n'
        break

# Also need to add the import and encoding before the return
# Find the "from fastapi.responses import Response" line
for i, line in enumerate(lines):
    if 'from fastapi.responses import Response' in line and 'urllib' not in lines[i-1]:
        lines[i] = '    from fastapi.responses import Response\n    from urllib.parse import quote\n    # RFC 5987 encoding: filename*=utf-8\'\'<url-encoded-filename>\n    encoded_filename = quote(safe_filename, encoding=\'utf-8\')\n'
        break

with open(r'c:\Projects\SiloVault\backend\api\v1\endpoints\files.py', 'w') as f:
    f.writelines(lines)
print('Fixed successfully')