with open('c:\\Projects\\SiloVault\\backend\\api\\v1\\endpoints\\files.py', 'r', newline='') as f:
    content = f.read()

# Replace the provider check and adapter lookup
old_block = '''    if record.provider != "LOCAL":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Downloads are only supported for LOCAL provider records. "
                f"Provider '{record.provider}' is not eligible."
            ),
        )

    # Get LOCAL adapter from registry and download
    try:
        adapter, _ = lookup("LOCAL")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Storage provider unavailable: {exc}",
        ) from exc'''

new_block = '''    # Get the appropriate adapter from registry based on the file's provider
    provider = record.provider
    try:
        adapter, _ = lookup(provider)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Storage provider unavailable: {exc}",
        ) from exc'''

content = content.replace(old_block, new_block)

# Update docstring to reflect multi-provider support
old_docstring = '''    """Download a locally stored file by its internal record ID.

    Security guarantees:
    - Only records with ``provider='LOCAL'`` can be downloaded.
    - The on-disk filename is taken exclusively from
      ``record.provider_file_id`` (a server-generated UUID); the original
      client-supplied ``name`` is used only as the download Content-Disposition
      header value.
    - The LOCAL adapter validates the path remains strictly inside the
      configured storage root before the file is served.
    - Missing records return 404; missing physical files return 404.
    """'''

new_docstring = '''    """Download a file by its internal record ID.

    Supports both LOCAL and cloud providers (e.g., GOOGLE_DRIVE).
    
    Security guarantees:
    - The provider-specific adapter handles access validation.
    - For LOCAL, the path remains strictly inside the configured storage root.
    - Missing records return 404; missing physical/cloud files return 404.
    """'''

content = content.replace(old_docstring, new_docstring)

with open('c:\\Projects\\SiloVault\\backend\\api\\v1\\endpoints\\files.py', 'w', newline='') as f:
    f.write(content)

print("Updated files.py")