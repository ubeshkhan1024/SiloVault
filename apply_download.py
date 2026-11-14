#!/usr/bin/env python
"""Direct editor for google_drive.py download() implementation."""

import re

path = r'c:\Projects\SiloVault\backend\storage\google_drive.py'

with open(path, 'r', encoding='utf-8', newline='') as f:
    content = f.read()

# Add io import if not present
if 'import io\n' not in content and 'import io\r\n' not in content:
    content = content.replace('import logging\n', 'import io\nimport logging\n')

# Replace the download stub
old_download = '''    def download(self, provider_file_id: str) -> bytes:
        raise StorageUnsupportedError("download", provider="GOOGLE_DRIVE")'''

new_download = '''    def download(self, provider_file_id: str) -> bytes:
        """Download a file from Google Drive by its provider-assigned ID.

        Uses :class:`googleapiclient.http.MediaIoBaseDownload` for resumable
        downloads that handle chunked transfers automatically.

        Parameters
        ----------
        provider_file_id : str
            Native Google Drive file identifier.

        Returns
        -------
        bytes
            Raw downloaded file content.

        Raises
        ------
        StorageAuthenticationError
            If not authenticated.
        StorageNotFoundError
            If no file exists under the given identifier.
        StoragePermissionError
            If the provider rejects the request due to insufficient permissions.
        StorageRateLimitError
            On rate-limit responses (429 or 403/userRateLimitExceeded).
        StorageQuotaExceededError
            If the provider reports insufficient remaining capacity.
        StorageUnavailableError
            On transient provider failures (5xx).
        """
        if not isinstance(provider_file_id, str) or not provider_file_id.strip():
            raise StorageNotFoundError(
                provider_file_id or "",
                provider="GOOGLE_DRIVE",
            )

        # Ensure we have an active Drive service
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )

        try:
            from googleapiclient.http import MediaIoBaseDownload
            byte_stream = io.BytesIO()
            request = self._drive_service.files().get_media(fileId=provider_file_id)
            downloader = MediaIoBaseDownload(byte_stream, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return byte_stream.getvalue()
        except ImportError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to import Google Drive download utilities: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc
        except HttpError as exc:
            raise self._map_drive_error(exc)
        except Exception as exc:
            raise StorageUnavailableError(
                detail=f"Failed to download file from Google Drive: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc'''

# Handle both CRLF and LF line endings
old_download_crlf = old_download.replace('\n', '\r\n')

if old_download in content:
    content = content.replace(old_download, new_download)
elif old_download_crlf in content:
    content = content.replace(old_download_crlf, new_download.replace('\n', '\r\n'))
else:
    # Try with escaped quotes from the file
    pattern = r'def download\(self, provider_file_id: str\) -> bytes:\s+raise StorageUnsupportedError\("download", provider="GOOGLE_DRIVE"\)'
    if re.search(pattern, content):
        content = re.sub(pattern, new_download, content)
    else:
        print("Pattern not found!")
        exit(1)

with open(path, 'w', encoding='utf-8', newline='') as f:
    f.write(content)

print("Successfully updated download() method")