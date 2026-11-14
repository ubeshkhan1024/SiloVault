"""Repair the 6 lines corrupted by the bad regex in google_drive.py."""
path = r"c:\Projects\SiloVault\backend\storage\google_drive.py"
with open(path, "rb") as f:
    data = f.read()

fixes = [
    # 1. rename() get call
    (
        b'self._drive_service.files()                .fileId=provider_file_id, fields="size,mimeType,modifiedTime,md5Checksum")',
        b'self._drive_service.files()\r\n                .get(fileId=provider_file_id, fields="size,mimeType,modifiedTime,md5Checksum")',
    ),
    # 2. rename() update call
    (
        b'self._drive_service.files()                .\r\n                    fileId=provider_file_id,',
        b'self._drive_service.files()\r\n                .update(\r\n                    fileId=provider_file_id,',
    ),
    # 3. quarantine() update call
    (
        b'self._drive_service.files()            .\r\n                fileId=provider_file_id,\r\n                body={"trashed": True},',
        b'self._drive_service.files()\r\n            .update(\r\n                fileId=provider_file_id,\r\n                body={"trashed": True},',
    ),
    # 4. restore() update call
    (
        b'self._drive_service.files()            .\r\n                fileId=quarantine_id,\r\n                body={"trashed": False},',
        b'self._drive_service.files()\r\n            .update(\r\n                fileId=quarantine_id,\r\n                body={"trashed": False},',
    ),
    # 5. restore() get call
    (
        b'self._drive_service.files()                .\r\n                    fileId=quarantine_id,\r\n                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",',
        b'self._drive_service.files()\r\n                .get(\r\n                    fileId=quarantine_id,\r\n                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",',
    ),
    # 6. cleanup() delete call
    (
        b'self._drive_service.files()            .fileId=quarantine_id)',
        b'self._drive_service.files()\r\n            .delete(fileId=quarantine_id)',
    ),
]

for i, (old, new) in enumerate(fixes, 1):
    if old not in data:
        print(f"FIX {i}: pattern NOT FOUND — skipping")
        continue
    data = data.replace(old, new, 1)
    print(f"FIX {i}: applied")

with open(path, "wb") as f:
    f.write(data)
print("Repair complete")
