#!/usr/bin/env python3
"""Validate existing images; never builds, pushes, tags or touches production.

Example:
  python3 validate.py --source /private/tmp/.../backend \
    --runtime-image registry.mageek.su/al-torrent-tools:release-... \
    --dev-image altt-release-qa:release-... --build-time ... --git-sha ...

Uses only uniquely named disposable containers and an internal Docker network.
No .env or existing volumes are read. Output remains beside this script.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
LABEL = 'altt.release-validation'
RUN_ID = uuid.uuid4().hex[:12]
PREFIX = f'altt-rv-{RUN_ID}'
COMMANDS = []
CONTAINERS = []
NETWORK_CREATED = False

def command(args, *, check=True, log_name=None, timeout=None):
    started = time.monotonic()
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    COMMANDS.append({'argv': args, 'exit_code': result.returncode, 'elapsed_sec': round(time.monotonic() - started, 3)})
    (ROOT / 'validation-commands.json').write_text(json.dumps(COMMANDS, indent=2))
    if log_name:
        (ROOT / (log_name + '.log')).write_text(result.stdout + result.stderr)
    if check and result.returncode:
        print(result.stdout[-6000:] + result.stderr[-12000:], file=sys.stderr)
        raise RuntimeError(f'Command failed: {args[:4]} (exit {result.returncode})')
    return result

def manifest(source):
    paths = [source / name for name in ('requirements.txt', 'constraints.txt', 'worker.py', 'alembic.ini')]
    paths += [source / 'scripts' / name for name in ('entrypoint.sh', 'migrate_with_lock.py', 'healthcheck_worker.py', 'healthcheck_telegram.py', 'profile_runtime.py', 'repair_encoder_receipts.py')]
    for directory in ('app', 'alembic'):
        paths.extend(p for p in (source / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo'))
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

def docker_run(image, name, platform, script, *, db_name=None, network=None, extra=None):
    cname = PREFIX + '-' + name
    CONTAINERS.append(cname)
    args = ['docker', 'run', '--rm', '--pull=always', '--name', cname, '--label', LABEL + '=' + RUN_ID, '--label', 'altt.release=' + RELEASE, '--platform', platform, '--network', network or 'none', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--mount', f'type=bind,src={ROOT},dst=/validation,readonly', '--workdir', '/app', '-e', 'PYTHONPATH=/app', '-e', 'PYTHONDONTWRITEBYTECODE=1']
    for key, value in BASE_ENV.items():
        args += ['-e', key + '=' + value]
    if db_name:
        args += ['-e', 'DATABASE_URL=postgresql+psycopg://altt_validation:local-validation-only@' + PREFIX + '-postgres:5432/' + db_name]
    if extra:
        args += extra
    args += ['--entrypoint', 'python', image, script]
    result = command(args, log_name=name)
    if name.startswith(('runtime-', 'http-')):
        payload = json.loads(result.stdout)
        assert payload['ok'] is True
        (ROOT / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return result

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, type=Path)
parser.add_argument('--runtime-image')
parser.add_argument('--dev-image')
parser.add_argument('--platforms', default='linux/arm64,linux/amd64')
parser.add_argument('--native-platform', default='linux/arm64')
parser.add_argument('--postgres-image', default='postgres:16-alpine')
parser.add_argument('--build-time', default='')
parser.add_argument('--git-sha', default='')
parser.add_argument('--release', default='release-20261024-validation')
args = parser.parse_args()
RELEASE = args.release
assert args.runtime_image or args.dev_image
source = args.source.resolve()
expected = manifest(source)
(ROOT / 'expected-runtime-manifest.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2))
BASE_ENV = {
    'APP_ENV': 'dev', 'SECRET_KEY': 'isolated-validation-no-production-secrets',
    'ANILIBRIA_BASE_URL': 'http://127.0.0.1:9/api/v1',
    'ANILIBRIA_FALLBACK_BASE_URL': 'http://127.0.0.1:9/api/v1',
    'ANILIBRIA_ADMIN_BASE_URL': '', 'ANILIBRIA_REQUEST_RETRIES': '0',
    'ANILIBRIA_RETRY_DELAY_MS': '0', 'TORRENT_STORAGE_DIR': '/tmp/altt-torrents',
    'ANILIBRIA_MEDIA_ROOT': '/tmp/altt-media',
    'EXPECTED_BUILD_TIME': args.build_time, 'EXPECTED_GIT_SHA': args.git_sha,
}
summary = {'ok': False, 'run_id': RUN_ID, 'source_files': len(expected), 'architectures': {}, 'qa_suite': None}
try:
    if args.runtime_image:
        for platform in args.platforms.split(','):
            arch = platform.rsplit('/', 1)[1]
            docker_run(args.runtime_image, 'runtime-' + arch, platform, '/validation/runtime_probe.py')
            summary['architectures'][arch] = {'runtime': True}
    command(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + RUN_ID, '--label', 'altt.release=' + RELEASE, PREFIX])
    NETWORK_CREATED = True
    pg = PREFIX + '-postgres'
    CONTAINERS.append(pg)
    command(['docker', 'run', '--detach', '--rm', '--name', pg, '--label', LABEL + '=' + RUN_ID, '--label', 'altt.release=' + RELEASE, '--platform', args.native_platform, '--network', PREFIX, '--tmpfs', '/var/lib/postgresql/data', '-e', 'POSTGRES_USER=altt_validation', '-e', 'POSTGRES_PASSWORD=local-validation-only', '-e', 'POSTGRES_DB=altt_test', args.postgres_image], log_name='postgres-start')
    for _ in range(90):
        ready = command(['docker', 'exec', pg, 'pg_isready', '-U', 'altt_validation', '-d', 'altt_test'], check=False)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Isolated Postgres readiness timed out')
    if args.runtime_image:
        for platform in args.platforms.split(','):
            arch = platform.rsplit('/', 1)[1]
            dbname = 'altt_runtime_' + arch
            command(['docker', 'exec', pg, 'createdb', '-U', 'altt_validation', dbname])
            docker_run(args.runtime_image, 'http-' + arch, platform, '/validation/http_probe.py', db_name=dbname, network=PREFIX)
            summary['architectures'][arch]['http'] = True
    if args.dev_image:
        qa_url = 'postgresql+psycopg://altt_validation:local-validation-only@' + pg + ':5432/altt_test'
        cname = PREFIX + '-pytest'
        CONTAINERS.append(cname)
        qa = ['docker', 'run', '--rm', '--name', cname, '--label', LABEL + '=' + RUN_ID, '--label', 'altt.release=' + RELEASE, '--platform', args.native_platform, '--network', PREFIX, '--workdir', '/app', '-e', 'PYTHONPATH=/app', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'ALTT_TEST_POSTGRES_URL=' + qa_url, '-e', 'DATABASE_URL=' + qa_url]
        # Unit tests intentionally assert public configuration defaults. Preserve
        # them; the internal Docker network blocks real services physically.
        for key in ('APP_ENV', 'SECRET_KEY', 'EXPECTED_BUILD_TIME', 'EXPECTED_GIT_SHA'):
            qa += ['-e', key + '=' + BASE_ENV[key]]
        # Bind only our output directory, never source or host credentials.
        qa += ['--mount', f'type=bind,src={ROOT},dst=/validation', '--entrypoint', 'python', args.dev_image, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--junitxml=/validation/pytest-junit.xml']
        command(qa, log_name='pytest-' + args.native_platform.rsplit('/', 1)[1])
        summary['qa_suite'] = 'passed'
    assert manifest(source) == expected, 'Source snapshot changed during validation'
    summary['source_snapshot_unchanged'] = True
    summary['ok'] = True
finally:
    for cname in reversed(CONTAINERS):
        inspected = command(['docker', 'inspect', '--format', '{{json .Config.Labels}}', cname], check=False)
        if inspected.returncode == 0 and json.loads(inspected.stdout).get(LABEL) == RUN_ID:
            command(['docker', 'rm', '--force', cname], check=False)
    if NETWORK_CREATED:
        command(['docker', 'network', 'rm', PREFIX], check=False)
    (ROOT / 'validation-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
