"""Run inside the unmodified runtime candidate with network disabled."""
import decimal
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig

import psycopg
import pymediainfo
from pymediainfo import MediaInfo

expected = json.loads(Path('/validation/expected-runtime-manifest.json').read_text())
actual = {
    str(p.relative_to('/app')): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in Path('/app').rglob('*')
    if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')
}
assert actual == expected, {
    'missing': sorted(set(expected) - set(actual)),
    'unexpected': sorted(set(actual) - set(expected)),
    'changed': sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k]),
}
assert sys.version_info[:2] == (3, 15), sys.version
assert not sysconfig.get_config_var('Py_GIL_DISABLED')
assert sys._is_gil_enabled()
assert not sys._jit.is_available()
assert str(decimal.Decimal('1.25') * 4) == '5.00'
assert psycopg.pq.__impl__ == 'binary', psycopg.pq.__impl__
_, _, mediainfo_version, mediainfo_tuple = MediaInfo._get_library()
assert mediainfo_tuple and mediainfo_tuple[0] >= 26, mediainfo_version
assert not list(Path(pymediainfo.__file__).resolve().parent.glob('libmediainfo*'))
assert util.find_spec('pytest') is None
assert util.find_spec('watchfiles') is None
assert not any(shutil.which(name) for name in ('cc', 'gcc', 'clang', 'g++', 'make'))
assert not Path('/app/tests').exists()
assert not any(p.name == '.env' or p.name.startswith('.env.') for p in Path('/app').rglob('*'))
pip = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True)
assert pip.returncode == 0, pip.stdout + pip.stderr
result = {
    'ok': True, 'architecture': platform.machine(), 'python': sys.version,
    'gil_enabled': sys._is_gil_enabled(),
    'free_threaded_build': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
    'jit_available': sys._jit.is_available(),
    'psycopg_impl': psycopg.pq.__impl__, 'psycopg_version': psycopg.__version__,
    'mediainfo': mediainfo_version, 'mediainfo_bundled_removed': True,
    'source_count': len(actual), 'source_files': actual,
    'pip_check': pip.stdout.strip(), 'pytest_absent': True,
    'watchfiles_absent': True, 'compilers_absent': True, 'dotenv_absent': True,
    'build_time': Path('/etc/altt_build_time').read_text().strip(),
    'git_sha': Path('/etc/altt_git_sha').read_text().strip(),
    'packages': {d.metadata['Name']: d.version for d in metadata.distributions()},
}
if os.environ.get('EXPECTED_BUILD_TIME'):
    assert result['build_time'] == os.environ['EXPECTED_BUILD_TIME']
if os.environ.get('EXPECTED_GIT_SHA'):
    assert result['git_sha'] == os.environ['EXPECTED_GIT_SHA']
print(json.dumps(result, ensure_ascii=False, indent=2))
