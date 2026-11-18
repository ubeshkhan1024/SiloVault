"""Add uuid import to test_recovery.py."""

path = r'c:\Projects\SiloVault\tests\test_recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

old = "import asyncio\nfrom datetime import datetime, timedelta, timezone\nfrom unittest.mock import MagicMock, patch\n"
new = "import asyncio\nimport uuid\nfrom datetime import datetime, timedelta, timezone\nfrom unittest.mock import MagicMock, patch\n"

if old not in content:
    print("ERROR: import block not found")
    exit(1)

if "import uuid" in content:
    print("uuid already imported")
    exit(0)

content = content.replace(old, new, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("uuid import added")
