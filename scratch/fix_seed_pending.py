import pathlib
import re

path = pathlib.Path('tests/test_recovery.py')
content = path.read_text(encoding='utf-8')

def add_cleanup(m):
    call = m.group(0)
    if 'operation_type=' in call:
        return call
    if call.endswith(')'):
        call = call[:-1] + ", operation_type='CLEANUP')"
    return call

pattern = re.compile(r'_seed_pending\([^)]+\)')
new_content = pattern.sub(add_cleanup, content)
path.write_text(new_content, encoding='utf-8')
print('Done')
