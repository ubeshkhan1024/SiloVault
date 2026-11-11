import os, sys
sys.path.insert(0, r'C:\Projects\SiloVault')
os.chdir(r'C:\Projects\SiloVault')
from backend.main import app

def walk_routes(router, prefix=''):
    for route in router.routes:
        path = getattr(route, 'path', None) or ''
        full_path = (prefix + path).rstrip('/')
        methods = sorted(getattr(route, 'methods', {'*'}))
        if full_path:
            print(f"{' '.join(methods):7} {full_path}")
        # recurse into included routers
        if hasattr(route, 'routes'):
            new_prefix = (prefix + path).rstrip('/')
            if new_prefix:
                new_prefix += '/'
            walk_routes(route, new_prefix)

walk_routes(app)
