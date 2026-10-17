import decimal,hashlib,importlib,importlib.metadata,importlib.util,json,os,platform,shutil,subprocess,sys,sysconfig
from pathlib import Path
assert sys.version_info[:3]==(3,15,0)
assert not sysconfig.get_config_var('Py_GIL_DISABLED') and sys._is_gil_enabled() is True
assert not sys._jit.is_available()
for name in ('blake3','httptools','uvloop','pydantic_core','markupsafe','sqlalchemy','psycopg','pymediainfo','jinja2'):
 importlib.import_module(name)
 assert sys._is_gil_enabled() is True
import psycopg
from psycopg import pq
from pymediainfo import MediaInfo
assert pq.__impl__=='binary' and psycopg.__version__==importlib.metadata.version('psycopg-binary')=='3.3.6'
assert importlib.metadata.version('Jinja2')=='3.1.6'
assert str(decimal.Decimal('1.25')*4)=='5.00'
_,_,media_ver,media_tuple=MediaInfo._get_library();assert media_tuple and media_tuple[0]>=26
import pymediainfo
assert not list(Path(pymediainfo.__file__).parent.glob('libmediainfo*'))
assert importlib.util.find_spec('pytest') is None and importlib.util.find_spec('watchfiles') is None
assert all(shutil.which(p) is None for p in ('gcc','g++','cc','make','cargo','rustc'))
assert not Path('/app/.env').exists() and not Path('/app/app/core/runtime_contract.py').exists()
assert Path('/etc/altt_build_time').read_text()=='2026-10-17T16:24:49Z'
assert Path('/etc/altt_git_sha').read_text()=='4e78fcfb10de16abd8bebf13232196ce84c1d5d0'
expected=json.loads(Path('/validation/expected-runtime.json').read_text())
actual={str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}
assert actual==expected,{'extra':sorted(set(actual)-set(expected)),'missing':sorted(set(expected)-set(actual)),'changed':[p for p in expected if actual.get(p)!=expected[p]]}
assert len(actual)==163
pip=subprocess.check_output([sys.executable,'-m','pip','check'],text=True).strip()
print(json.dumps({'ok':True,'architecture':platform.machine(),'python':sys.version,'gil_enabled':sys._is_gil_enabled(),'free_threaded_build':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),'jit_available':sys._jit.is_available(),'psycopg_impl':pq.__impl__,'psycopg_version':psycopg.__version__,'libpq_version':pq.version(),'jinja2':importlib.metadata.version('Jinja2'),'mediainfo':media_ver,'mediainfo_bundled_removed':True,'decimal_arithmetic':'5.00','decimal_module':decimal.Decimal.__module__,'native_decimal_available':importlib.util.find_spec('_decimal') is not None,'source_files':actual,'source_count':len(actual),'pip_check':pip,'pytest_absent':True,'compilers_absent':True,'dotenv_absent':True,'build_time':Path('/etc/altt_build_time').read_text(),'git_sha':Path('/etc/altt_git_sha').read_text(),'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}},indent=2))
