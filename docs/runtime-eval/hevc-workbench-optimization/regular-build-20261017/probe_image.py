"""Verify an ordinary CPython image without DB/network/lifespan activity."""
import decimal
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

assert sys.version_info[:3] == (3, 15, 0), sys.version
assert not sysconfig.get_config_var('Py_GIL_DISABLED')
assert sys._is_gil_enabled() is True
assert not sys._jit.is_available()
assert str(decimal.Decimal('1.25') * 4) == '5.00'
for name in ('blake3', 'httptools', 'uvloop', 'pydantic_core', 'markupsafe', 'sqlalchemy', 'psycopg', 'pymediainfo'):
    importlib.import_module(name)
    assert sys._is_gil_enabled() is True
import psycopg
from psycopg import pq
from pymediainfo import MediaInfo
assert pq.__impl__ == 'binary'
assert psycopg.__version__ == importlib.metadata.version('psycopg-binary') == '3.3.6'
assert importlib.util.find_spec('psycopg_c') is None
_, _, mediainfo_version, mediainfo_tuple = MediaInfo._get_library()
assert mediainfo_tuple and mediainfo_tuple[0] >= 26
import pymediainfo
assert not list(Path(pymediainfo.__file__).resolve().parent.glob('libmediainfo*'))
assert not Path('/app/.env').exists()
assert not Path('/app/app/core/runtime_contract.py').exists()
assert not Path('/app/scripts/check_runtime.py').exists()
service = Path('/app/app/services/hevc_workbench.py')
service_sha = hashlib.sha256(service.read_bytes()).hexdigest()
assert service_sha == 'e5838d5cef83af43407130d12e9d6044773e5a459186b2b4f70944054af51ef4'
assert Path('/etc/altt_build_time').read_text() == '2026-10-17T16:11:51Z'
assert Path('/etc/altt_git_sha').read_text() == '4e78fcfb10de16abd8bebf13232196ce84c1d5d0'
pip_check = subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True,check=True)
print(json.dumps({
    'ok':True, 'python':sys.version, 'python_executable':sys.executable,
    'free_threaded_build':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
    'gil_enabled':sys._is_gil_enabled(), 'jit_available':sys._jit.is_available(),
    'psycopg_impl':pq.__impl__, 'psycopg_version':psycopg.__version__, 'libpq_version':pq.version(),
    'mediainfo_version':mediainfo_version, 'bundled_mediainfo_removed':True,
    'decimal_module':decimal.Decimal.__module__, 'decimal_native_available':importlib.util.find_spec('_decimal') is not None,
    'workbench_sha256':service_sha, 'fake_315t_guards_absent':True,
    'build_time':Path('/etc/altt_build_time').read_text(), 'git_sha':Path('/etc/altt_git_sha').read_text(),
    'pip_check':pip_check.stdout.strip(), 'pytest_installed':importlib.util.find_spec('pytest') is not None,
    'watchfiles_installed':importlib.util.find_spec('watchfiles') is not None,
    'packages':{dist.metadata['Name']:dist.version for dist in importlib.metadata.distributions()},
},ensure_ascii=False,indent=2))
