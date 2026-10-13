import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig

modules = ('_decimal', '_ssl', '_hashlib', '_sqlite3', '_lzma', '_bz2', 'yaml._yaml',
           'pydantic_core', 'blake3', 'psycopg', 'psycopg_binary', 'uvloop', 'httptools',
           'watchfiles', 'websockets', 'pymediainfo', 'app.main', 'worker',
           'app.telegram_bot.handlers', 'app.telegram_bot.__main__')
imports = []
for name in modules:
    before = sys._is_gil_enabled()
    module = importlib.import_module(name)
    after = sys._is_gil_enabled()
    assert before and after, name
    imports.append({'name': name, 'gil_before': before, 'gil_after': after,
                    'file': getattr(module, '__file__', None)})

import _decimal
import decimal
import psycopg
import yaml
from pymediainfo import MediaInfo
assert decimal.Decimal is _decimal.Decimal
assert str(decimal.Decimal('1.25') * decimal.Decimal('4')) == '5.00'
assert yaml.__with_libyaml__
assert psycopg.pq.__impl__ == 'binary'
assert not sysconfig.get_config_var('Py_GIL_DISABLED')
assert sys._is_gil_enabled()
assert not sys._jit.is_available()
library, handle, version, version_tuple = MediaInfo._get_library()
assert version_tuple[0] >= 26
import pymediainfo
bundled = list(Path(pymediainfo.__file__).parent.glob('libmediainfo*'))
assert not bundled, bundled
assert not shutil.which('gcc') and not shutil.which('make')
assert not Path('/tmp/ensure_native_decimal.py').exists()
assert not list(Path('/tmp').glob('altt-decimal-*'))

def canonical(value):
    return re.sub('[-_.]+', '-', value).lower()

expected = {}
for line in Path('/app/constraints.txt').read_text().splitlines():
    if line and not line.startswith('#'):
        name, value = line.split('==')
        expected[canonical(name)] = value
installed = {canonical(d.metadata['Name']): d.version for d in metadata.distributions()}
assert len(expected) == 46
assert {name: installed.get(name) for name in expected} == expected
assert set(installed) - set(expected) == {'pip'}, set(installed) - set(expected)
dpkg = subprocess.run(['dpkg-query', '-W', '-f=${Package} ${Status}\n',
                       'gcc', 'libc6-dev', 'make', 'libyaml-dev', 'libyaml-0-2'],
                      capture_output=True, text=True)
assert 'libyaml-0-2 install ok installed' in dpkg.stdout
for line in dpkg.stdout.splitlines():
    if not line.startswith('libyaml-0-2 '):
        assert 'install ok installed' not in line, line

print(json.dumps({'python': sys.version, 'architecture': platform.machine(),
                  'platform': platform.platform(), 'gil_enabled': sys._is_gil_enabled(),
                  'jit_available': sys._jit.is_available(),
                  'config': {key: sysconfig.get_config_var(key) for key in
                             ('SOABI', 'EXT_SUFFIX', 'Py_GIL_DISABLED', 'Py_DEBUG', 'Py_TRACE_REFS', 'CONFIG_ARGS')},
                  'libmpdec': _decimal.__libmpdec_version__, 'libyaml': yaml._yaml.get_version_string(),
                  'mediainfo': version, 'psycopg_backend': psycopg.pq.__impl__,
                  'psycopg_libpq': psycopg.pq.version(), 'distributions': installed,
                  'native_imports': imports, 'build_dependency_status': dpkg.stdout,
                  'compiler_removed': True, 'bundled_mediainfo_removed': True},
                 indent=2, sort_keys=True))
