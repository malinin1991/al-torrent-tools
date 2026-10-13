import importlib
import importlib.metadata as metadata
import json
import platform
import ssl
import sys
import sysconfig
from pathlib import Path

stdlib = {}
for name in ('_decimal', '_ssl', '_lzma', '_bz2', '_sqlite3', '_zstd', '_ctypes', '_gdbm', '_tkinter'):
    try:
        module = importlib.import_module(name)
        stdlib[name] = {'available': True, 'version': getattr(module, '__libmpdec_version__', None)}
    except ImportError as exc:
        stdlib[name] = {'available': False, 'error': str(exc)}

from blake3 import blake3
assert blake3(b'').hexdigest() == 'af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262'
import psycopg
import uvloop
import httptools
import pydantic_core
import yaml
import sqlalchemy
with sqlalchemy.create_engine('sqlite:///:memory:').connect() as conn:
    assert conn.execute(sqlalchemy.text('SELECT 1')).scalar_one() == 1
loop = uvloop.new_event_loop()
assert loop.run_until_complete(__import__('asyncio').sleep(0, result=7)) == 7
loop.close()
try:
    from pymediainfo import MediaInfo
    lib, handle, version, version_tuple = MediaInfo._get_library()
    media = {'available': True, 'path': str(lib._name), 'version': version, 'meets_production_minimum_26': bool(version_tuple and version_tuple[0] >= 26)}
except Exception as exc:
    media = {'available': False, 'error': str(exc)}

result = {
    'version': sys.version, 'executable': sys.executable,
    'platform': platform.platform(), 'machine': platform.machine(),
    'config_args': sysconfig.get_config_var('CONFIG_ARGS'),
    'gil_enabled': sys._is_gil_enabled(), 'gil_disabled_build': sysconfig.get_config_var('Py_GIL_DISABLED'),
    'jit_available': sys._jit.is_available(), 'jit_enabled': sys._jit.is_enabled(),
    'utf8_mode': sys.flags.utf8_mode,
    'thread_inherit_context': getattr(sys.flags, 'thread_inherit_context', None),
    'context_aware_warnings': getattr(sys.flags, 'context_aware_warnings', None),
    'abiflags': sys.abiflags, 'xoptions': sys._xoptions,
    'with_pymalloc': sysconfig.get_config_var('WITH_PYMALLOC'),
    'with_mimalloc': sysconfig.get_config_var('WITH_MIMALLOC'),
    'psycopg_backend': psycopg.pq.__impl__,
    'openssl': ssl.OPENSSL_VERSION,
    'libpq': psycopg.pq.version(),
    'pyyaml_libyaml': yaml.__with_libyaml__,
    'stdlib': stdlib, 'mediainfo': media,
    'distributions': {d.metadata['Name']: d.version for d in metadata.distributions()},
}
print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
