import hashlib
import json
from pathlib import Path

root = Path('/app')
files = {}
for folder in ('app', 'tests', 'alembic', 'scripts'):
    for path in sorted((root / folder).rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py', '.html', '.js', '.css'):
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
for name in ('worker.py', 'requirements.txt', 'constraints.txt', 'Dockerfile', 'alembic.ini', '.dockerignore'):
    files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
serialized = json.dumps(files, sort_keys=True, separators=(',', ':')).encode()
print(json.dumps({'sha256': hashlib.sha256(serialized).hexdigest(), 'files': files}, indent=2, sort_keys=True))
