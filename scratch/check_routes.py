import re

endpoint_files = [
    'backend/api/v1/endpoints/health.py',
    'backend/api/v1/endpoints/files.py',
    'backend/api/v1/endpoints/uploads.py',
    'backend/api/v1/endpoints/sync.py',
    'backend/api/v1/endpoints/processing.py',
    'backend/api/v1/endpoints/review.py',
    'backend/api/v1/endpoints/activity.py',
]

for f in endpoint_files:
    with open(f) as fp:
        content = fp.read()
    # Find router prefix
    prefix_match = re.search(r'prefix="([^"]+)"', content)
    prefix = prefix_match.group(1) if prefix_match else ''
    # Find routes
    routes = re.findall(r'@(\w+)_router\.(get|post|put|delete|patch)\("([^"]*)"\)', content)
    for router_name, method, path in routes:
        full_path = f'/api/v1{prefix}{path}'
        print(f'{method.upper()} {full_path}')