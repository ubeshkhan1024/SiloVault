"""Repair remaining corrupted line in google_drive.py."""
path = r"c:\Projects\SiloVault\backend\storage\google_drive.py"
with open(path, "rb") as f:
    data = f.read()

# Fix the move() method's get call that got corrupted
old = b'self._drive_service.files()                .\r\n                    fileId=source_provider_file_id,\r\n                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",'

new = b'self._drive_service.files()\r\n                .get(\r\n                    fileId=source_provider_file_id,\r\n                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",'

if old in data:
    data = data.replace(old, new, 1)
    print("Fixed move() get call")
else:
    print("Pattern not found for move() get call")

with open(path, "wb") as f:
    f.write(data)
print("Repair attempt complete")