#!/usr/bin/env python3
"""Explicitly invoked after build+GO; never builds or installs packages."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--image-id',required=True)
args=parser.parse_args()
if not args.image_id.startswith('sha256:') or len(args.image_id)!=71:
    parser.error('Pass the resolved immutable image ID, not a tag')
fixture=json.loads((ROOT/'postgres-fixture.json').read_text())
if (ROOT/'test-invocation.json').exists():
    parser.error('Refusing to rerun/overwrite test evidence in this directory')
metadata={'image_id':args.image_id,'fixture':fixture,'host_wrapper_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'suite_wrapper_sha256':hashlib.sha256((ROOT/'run_suite.py').read_bytes()).hexdigest()}
(ROOT/'test-invocation.json').write_text(json.dumps(metadata,indent=2)+'\n')
base=['docker','run','--rm','--network',fixture['network'],'--label','altt.regular-workbench=20261017-161151',
      '--tmpfs','/tmp:rw,size=1073741824','--mount',f'type=bind,source={ROOT},target=/evidence',
      '--mount',f'type=bind,source={ROOT},target=/runner,readonly','--workdir','/app',
      '--env','PSYCOPG_IMPL=binary','--env','PYTHONDONTWRITEBYTECODE=1','--env','PYTHONPATH=/app',
      '--env','APP_ENV=dev','--env','SECRET_KEY=isolated-runtime-tests',
      '--env','DATABASE_URL='+fixture['database_url'],'--env','ALTT_TEST_POSTGRES_URL='+fixture['database_url'],
      '--env','ANILIBRIA_BASE_URL=http://invalid.invalid','--env','ANILIBRIA_FALLBACK_BASE_URL=http://invalid.invalid',
      '--env','ANILIBRIA_ADMIN_BASE_URL=','--env','ANILIBRIA_MEDIA_ROOT=/tmp/altt-test-media',
      '--env','TORRENT_STORAGE_DIR=/tmp/altt-test-torrents','--entrypoint','/usr/local/bin/python',args.image_id]
commands=[('pip-check',['-m','pip','check']),
          ('compileall',['-X','pycache_prefix=/tmp/altt-compile-cache','-m','compileall','-q','app','tests','worker.py']),
          ('pytest',['/runner/run_suite.py'])]
for name,arguments in commands:
    argv=base+arguments
    path=ROOT/(name+'.log')
    print('START '+name,flush=True)
    start_wall,start_mono=time.time(),time.monotonic()
    with path.open('x') as stream:
        result=subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT,check=False)
    record={'stage':name,'argv':argv,'image_id':args.image_id,'exit_code':result.returncode,
            'start_utc':datetime.fromtimestamp(start_wall,timezone.utc).isoformat(),
            'wall_sec':time.monotonic()-start_mono,'log':path.name,
            'log_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    with (ROOT/'test-commands.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n')
    print('DONE '+name+' exit='+str(result.returncode),flush=True)
    if result.returncode:
        print(path.read_text()[-8000:],flush=True)
        raise SystemExit(result.returncode)
