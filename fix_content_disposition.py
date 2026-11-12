with open(r'c:\Projects\SiloVault\backend\api\v1\endpoints\files.py', 'r') as f:
    content = f.read()

old = '''    # Return bytes directly instead of using FileResponse to avoid filesystem path exposure
    from fastapi.responses import Response
    return Response(
        content=file_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename*=utf-8\\\\\\'\\\\\\'{safe_filename}',
        },
    )'''

new = '''    # Return bytes directly instead of using FileResponse to avoid filesystem path exposure
    from fastapi.responses import Response
    from urllib.parse import quote
    # RFC 5987 encoding: filename*=utf-8''<url-encoded-filename>
    encoded_filename = quote(safe_filename, encoding='utf-8')
    return Response(
        content=file_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=utf-8''{encoded_filename}",
        },
    )'''

if old in content:
    content = content.replace(old, new)
    with open(r'c:\Projects\SiloVault\backend\api\v1\endpoints\files.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Old text not found')
    # Print what we're looking for
    idx = content.find('Content-Disposition')
    if idx >= 0:
        print(repr(content[idx:idx+200]))