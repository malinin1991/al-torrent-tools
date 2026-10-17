#!/usr/bin/env python3
"""Create only this task's disposable, internal, tmpfs PostgreSQL fixture."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
NETWORK = 'altt-release-162449-tests'
CONTAINER = 'altt-release-162449-pg'
DATABASE = 'altt_release_test'
LABEL = 'altt.release=release-20261017-162449'

def run(argv):
    start = time.monotonic()
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    record = {'utc': datetime.now(timezone.utc).isoformat(), 'argv': argv,
              'exit_code': result.returncode, 'wall_sec': time.monotonic() - start,
              'stdout': result.stdout, 'stderr': result.stderr}
    with (ROOT/'setup-commands.jsonl').open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    if result.returncode:
        raise RuntimeError('Fixture setup failed: ' + json.dumps(record))
    return result.stdout.strip()

if (ROOT/'postgres-fixture.json').exists():
    raise SystemExit('Refusing to replace an existing fixture record')
image = run(['docker','image','inspect','--format','{{.Id}}','postgres:16'])
network_id = run(['docker','network','create','--internal','--label',LABEL,NETWORK])
container_id = run(['docker','run','-d','--name',CONTAINER,'--network',NETWORK,'--label',LABEL,
    '--tmpfs','/var/lib/postgresql/data:rw,size=536870912','--shm-size','128m',
    '--env','POSTGRES_HOST_AUTH_METHOD=trust','--env','POSTGRES_USER=postgres',
    '--env','POSTGRES_DB='+DATABASE,image,'-c','fsync=on','-c','full_page_writes=on',
    '-c','synchronous_commit=on'])
ready = False
for attempt in range(30):
    check = subprocess.run(['docker','exec',CONTAINER,'pg_isready','-h','127.0.0.1','-U','postgres','-d',DATABASE],
                           capture_output=True,text=True,check=False)
    if check.returncode == 0:
        ready = True
        break
    time.sleep(.5)
if not ready:
    raise RuntimeError('PostgreSQL did not become ready; preserve container for diagnosis')
configuration = run(['docker','exec',CONTAINER,'psql','-h','127.0.0.1','-U','postgres','-d',DATABASE,'-At','-c',
    "SELECT current_database(), version(), current_setting('fsync'), current_setting('full_page_writes'), current_setting('synchronous_commit')"])
metadata = {'network':NETWORK,'network_id':network_id,'container':CONTAINER,'container_id':container_id,
    'image_id':image,'database_name':DATABASE,'database_url':f'postgresql+psycopg://postgres@{CONTAINER}:5432/{DATABASE}',
    'host_ports':[],'data_storage':'container-only tmpfs 512MiB','label':LABEL,'configuration':configuration,
    'scope':'Disposable test DB; no production mount, dotenv or host port. Keep for root lifecycle smoke.'}
(ROOT/'postgres-fixture.json').write_text(json.dumps(metadata,indent=2)+'\n')
print(json.dumps(metadata,indent=2))
