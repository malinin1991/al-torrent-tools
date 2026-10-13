"""Fresh-process GIL fallback audit; never forces gil=0."""
import argparse
import importlib.metadata
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--application-root',default='/app')
args=parser.parse_args()
module_names={'_decimal','_ssl','_hashlib','_sqlite3','_lzma','_bz2','_ctypes',
              'sqlalchemy','psycopg','pydantic_core','blake3','uvloop','httptools',
              'yaml','markupsafe','charset_normalizer','watchfiles','websockets',
              'pymediainfo','app.services.hevc_pairing','app.services.encoder_history',
              'app.main','worker','app.telegram_bot.handlers'}
for dist in importlib.metadata.distributions():
 for file in dist.files or ():
  path=Path(str(file))
  for suffix in sorted(importlib.machinery.EXTENSION_SUFFIXES,key=len,reverse=True):
   if path.name.endswith(suffix):
    name=path.name[:-len(suffix)]
    if name and not any(part.startswith('..') for part in path.parts):
     module_names.add('.'.join((*path.parts[:-1],name)))
    break
child=r'''
import importlib,json,sys,sysconfig,warnings
before=sys._is_gil_enabled()
record={'module':sys.argv[1], 'gil_before':before,'free_threaded_build':sysconfig.get_config_var('Py_GIL_DISABLED'),'forced_gil_argument':sys._xoptions.get('gil')}
with warnings.catch_warnings(record=True) as seen:
 warnings.simplefilter('always')
 try:
  module=importlib.import_module(sys.argv[1])
  record['import_ok']=True
  record['module_file']=getattr(module,'__file__',None)
  if sys.argv[1]=='psycopg':
   record['psycopg_backend']=module.pq.__impl__
   record['libpq_version']=module.pq.version()
  if sys.argv[1]=='pymediainfo':
   lib,handle,version,version_tuple=module.MediaInfo._get_library()
   record['mediainfo_version']=version
  if sys.argv[1]=='yaml':
   record['libyaml_enabled']=module.__with_libyaml__
 except Exception as exc:
  record['import_ok']=False
  record['error']=f'{type(exc).__name__}: {exc}'
 record['warnings']=[{'category':type(w.message).__name__,'message':str(w.message)} for w in seen]
record['gil_after']=sys._is_gil_enabled()
record['automatic_gil_enable']=not before and record['gil_after']
print(json.dumps(record,sort_keys=True))
'''
env=dict(os.environ)
env.pop('PYTHON_GIL',None)
env['PYTHONPATH']=args.application_root
env['PYTHONDONTWRITEBYTECODE']='1'
records=[]
for name in sorted(module_names):
 result=subprocess.run([sys.executable,'-c',child,name],env=env,text=True,capture_output=True,timeout=45)
 try:
  row=json.loads(result.stdout.splitlines()[-1])
 except (ValueError,IndexError):
  row={'module':name,'import_ok':False,'error':'No JSON result','stdout':result.stdout[-1000:]}
 row['returncode']=result.returncode
 if result.stderr:
  row['stderr']=result.stderr[-2000:]
 records.append(row)
combined=r'''
import importlib,json,sys,sysconfig,warnings
modules=['app.main','worker','app.telegram_bot.handlers','uvloop','httptools','yaml','watchfiles','blake3','pymediainfo']
result={'gil_before':sys._is_gil_enabled(),'free_threaded_build':sysconfig.get_config_var('Py_GIL_DISABLED'),'forced_gil_argument':sys._xoptions.get('gil'),'imports':[]}
with warnings.catch_warnings(record=True) as seen:
 warnings.simplefilter('always')
 for name in modules:
  importlib.import_module(name)
  result['imports'].append({'module':name,'gil_enabled':sys._is_gil_enabled()})
 result['warnings']=[{'category':type(w.message).__name__,'message':str(w.message)} for w in seen]
result['gil_after']=sys._is_gil_enabled()
print(json.dumps(result,sort_keys=True))
'''
result=subprocess.run([sys.executable,'-c',combined],env=env,text=True,capture_output=True,timeout=60)
try:
 application=json.loads(result.stdout.splitlines()[-1])
except (ValueError,IndexError):
 application={'import_ok':False,'stdout':result.stdout[-1000:],'stderr':result.stderr[-2000:],'returncode':result.returncode}
report={'python':sys.version,'executable':sys.executable,'probe_forces_gil_off':False,
        'native_and_package_imports':records,'combined_application_import':application,
        'installed_versions':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}
args.output.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
print(json.dumps({'probed':len(records),'import_failures':[r['module'] for r in records if not r.get('import_ok')],
 'automatic_gil_enable':[r['module'] for r in records if r.get('automatic_gil_enable')],
 'combined_gil_after':application.get('gil_after')},sort_keys=True))
