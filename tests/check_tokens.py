with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r", encoding="utf-8") as f:
    content = f.read()

# Check token presence
print(f'Contains old-dead-token: {"old-dead-token" in content}')
print(f'Contains stale-token: {"stale-token" in content}')
print(f'Contains old-null-token: {"old-null-token" in content}')
print(f'Contains gen-token: {"gen-token" in content}')
print(f'File length: {len(content)}')